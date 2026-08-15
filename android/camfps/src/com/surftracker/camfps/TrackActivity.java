package com.surftracker.camfps;

import android.app.Activity;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothSocket;
import android.graphics.ImageFormat;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.util.Size;
import android.util.SizeF;
import android.view.Surface;

import org.tensorflow.lite.Interpreter;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.OutputStreamWriter;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.FloatBuffer;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * ЭТАП 4: замкнутый контур зрение → мотор.
 *
 * Камера → кроп → модель → угловая ошибка → уставка скорости → Bluetooth →
 * ESP32 → STM32 → вал. Телеметрия обратно тем же путём.
 *
 * Что здесь НЕ делается и почему:
 *
 *   - НЕТ фильтра Калмана и предсказания цели. Первый прогон должен показать
 *     сырое поведение: сколько живёт детекция, какая задержка, куда уезжает
 *     контур. Фильтр поверх непонятого поведения скрыл бы ровно то, что надо
 *     увидеть.
 *   - ω̇ шлётся НУЛЁМ. Поле есть в протоколе, и соблазн заполнить его
 *     разностью велик, но разность шумной детекции по шумному времени
 *     телефона дала бы приёмнику мусор ровно в том поле, которым он
 *     экстраполирует при пропаже кадров. Лучше не экстраполировать вовсе,
 *     чем экстраполировать по шуму.
 *   - Регулятор — чистое П по углу. Уставка ω = K · (угловая ошибка). Рампа,
 *     пределы и сторож уже стоят в приёмнике, дублировать их здесь незачем:
 *     приёмник обязан быть безопасен при ЛЮБОМ входе, включая сломанный
 *     телефон.
 *
 * ЗНАК. Куда крутить при цели справа — зависит от того, как закреплена плата,
 * и по данным неизвестно. Поэтому знак вынесен в параметр `sign`, а первый
 * прогон служит его определением: если камера уезжает ОТ цели, знак обратный.
 * Измерено на стенде: положительная уставка → вал против часовой при взгляде
 * сверху (docs/ПРОТОКОЛ_ТЕЛЕФОН_STM32.md).
 *
 * Модель выбирается параметром; форма выхода спрашивается у интерпретатора.
 * У сёрфовой это [1,5,8400], у COCO [1,84,8400], но строка 4 в обоих случаях
 * означает одно и то же — единственный класс либо person (класс 0 COCO).
 *
 *   am start -n com.surftracker.camfps/.TrackActivity \
 *       --es mac 38:18:2B:30:7D:86 --es model person_w8a32.tflite \
 *       --ei seconds 60 --ef k 1.2 --ei sign 1 [--es tag t1] [--ei side 1280]
 */
public class TrackActivity extends Activity {
    static final String TAG = "track";

    // ГОЛОСОВОЙ СУФЛЁР.
    //
    // Прогоны с участием человека нельзя строить на инструкции «в такую-то
    // секунду сделайте то-то»: держать отсчёт в голове невозможно, и Hero
    // прямо сказал, что не сможет. Экран телефона смотрит ОТ наблюдателя
    // (камера-то направлена на него), поэтому единственный доступный канал —
    // звук.
    //
    // Тон-сигналы отвергнуты: они требуют помнить код, то есть переносят ту же
    // нагрузку в другое место. Речь не требует ничего.
    android.speech.tts.TextToSpeech tts;
    volatile boolean ttsReady = false;
    android.media.ToneGenerator tone;

    // Экран показывает ТЕКУЩУЮ реплику. Звук может не пройти (движок синтеза
    // не выбран, громкость снята, наушники), и тогда экран остаётся
    // единственным каналом. Дублирование здесь не избыточность: канал, у
    // которого нет запасного, отказывает молча.
    volatile android.widget.TextView statusView;
    // Защёлка старта: прогон не начинается, пока наблюдатель не нажал кнопку.
    // Так согласование момента уходит из переписки в приложение — Hero жмёт,
    // когда встал, а не когда прочитал сообщение.
    final CountDownLatch startGate = new CountDownLatch(1);

    void say(String phrase) {
        Log.i(TAG, "СУФЛЁР: " + phrase);
        final android.widget.TextView sv = statusView;
        if (sv != null) runOnUiThread(() -> sv.setText(phrase));
        boolean spoken = false;
        try {
            if (ttsReady && tts != null) {
                tts.speak(phrase, android.speech.tts.TextToSpeech.QUEUE_FLUSH, null, "p");
                spoken = true;
            }
        } catch (Throwable t) { Log.e(TAG, "речь: " + t); }
        // Запасной канал: если речь недоступна, хотя бы отбить внимание.
        // Молчаливый суфлёр хуже отсутствующего — наблюдатель будет ждать.
        if (!spoken) {
            try {
                if (tone == null)
                    tone = new android.media.ToneGenerator(
                            android.media.AudioManager.STREAM_MUSIC, 100);
                tone.startTone(android.media.ToneGenerator.TONE_PROP_BEEP2, 400);
            } catch (Throwable t) { Log.e(TAG, "сигнал: " + t); }
        }
    }

    // Последняя вычисленная уставка. Пишет поток зрения, читает поток
    // отправки. volatile достаточно: одно значение, атомарная запись float,
    // и терять промежуточные значения не страшно — свежее всегда лучше.
    volatile float wCmd = 0.0f;
    // Момент последней ЗАПИСИ уставки. Без него развязка отправки от зрения
    // создаёт худший отказ, чем чинит: если цикл зрения умрёт МОЛЧА (камеру
    // отнял звонок, приложение ушло в фон, слушатель кадров проглотил
    // исключение), поток отправки продолжит слать последнюю команду 10 раз в
    // секунду. Сторож приёмника её не поймает — кадры свежие, seq растёт, —
    // и вал уедет вслепую до конца сеанса.
    //
    // Асимметрия: ШУМНЫЙ отказ безопасен (исключение уводит в finally, сокет
    // закрывается, сторож роняет вал за 300 мс), опасен ровно тот путь, где
    // никто ничего не бросает.
    volatile long wCmdNs = 0;

    /**
     * Ровно ОДИН прогон на процесс одновременно.
     *
     * Найдено ухом: каждая реплика суфлёра прозвучала дважды, и в логе видно
     * два разных потока. Активность создаётся повторно (пробуждение экрана,
     * снятие блокировки, смена конфигурации), и каждый экземпляр запускает
     * свой run(): свою камеру, свой сокет, свой поток отправки уставок.
     *
     * На сухом прогоне это эхо. В боевом — ДВА независимых отправителя,
     * гоняющих мотор наперегонки, каждый со своим представлением о цели.
     * Ни один тест этого не показал бы: оба прогона пишут в один файл, и
     * последний закрывшийся затирает первого.
     */
    static final java.util.concurrent.atomic.AtomicBoolean RUNNING_ONE =
            new java.util.concurrent.atomic.AtomicBoolean(false);
    volatile boolean running = true;
    volatile int lastStatus = 0;
    volatile float lastTheta = 0, lastWRamp = 0;
    volatile int telCount = 0;
    volatile int bWd = 0, bCap = 0, bRamp = 0, bEnc = 0, bClamp = 0, bSlip = 0;
    volatile int staleZeros = 0;   // сколько раз слали ноль из-за протухания
    static final UUID SPP = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB");
    static final int NET = 640;
    static final float CONF_MIN = 0.35f;

    ImageReader reader;      // сильная ссылка: иначе финализатор закроет поток

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        // ЭКРАН ОБЯЗАН ГОРЕТЬ, и это не косметика.
        //
        // При погашенном экране Андроид считает приложение фоновым и переселяет
        // его на малые ядра. Замер: инференс 545 мс в дозе против 183 мс в
        // прежнем рабочем прогоне на этом же телефоне и той же модели. Причём
        // в дозе время НЕ ЗАВИСИТ ни от числа потоков, ни от XNNPACK, ни от
        // размера модели — сколько потоков ни дай, все они на малых ядрах.
        // Именно это безразличие и выдало причину: постоянное время при любых
        // настройках вычислений означает, что упираемся не в вычисления.
        //
        // Флаги окна FLAG_TURN_SCREEN_ON и FLAG_SHOW_WHEN_LOCKED на Android 12+
        // уже не действуют — нужны методы активности, и звать их надо ДО того,
        // как окно создано.
        try {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
            android.app.KeyguardManager km = getSystemService(android.app.KeyguardManager.class);
            if (km != null) km.requestDismissKeyguard(this, null);
        } catch (Throwable t) {
            Log.e(TAG, "разбудить экран не вышло: " + t);
        }
        final boolean auto = getIntent().getBooleanExtra("auto", false);
        runOnUiThread(() -> {
            android.widget.LinearLayout root = new android.widget.LinearLayout(this);
            root.setOrientation(android.widget.LinearLayout.VERTICAL);
            root.setPadding(40, 80, 40, 40);

            android.widget.TextView tv = new android.widget.TextView(this);
            tv.setTextSize(26);
            tv.setText(auto ? "слежение" : "нажмите СТАРТ, когда встанете в кадр");
            statusView = tv;

            android.widget.Button go = new android.widget.Button(this);
            go.setTextSize(34);
            go.setText("СТАРТ");
            go.setOnClickListener(v -> {
                go.setEnabled(false);
                go.setText("...");
                // Отсчёт вслух: наблюдателю нужно время отойти от телефона и
                // встать в кадр. Без него первая реплика застаёт его у экрана.
                new Thread(() -> {
                    try {
                        // Восемь секунд, а не три: наблюдателю надо отойти от
                        // телефона и ВСТАТЬ В КАДР. При трёх он ещё в движении
                        // у края кадра, ошибка наведения на старте выходит под
                        // тридцать градусов, и контур начинает с рывка.
                        say("Отойдите и встаньте в кадр"); Thread.sleep(4000);
                        say("Пять"); Thread.sleep(1000);
                        say("Четыре"); Thread.sleep(1000);
                        say("Три"); Thread.sleep(1000);
                        say("Два"); Thread.sleep(1000);
                        say("Один"); Thread.sleep(1000);
                    } catch (Throwable ignored) {}
                    startGate.countDown();
                }).start();
            });
            root.addView(go, new android.widget.LinearLayout.LayoutParams(
                    android.widget.LinearLayout.LayoutParams.MATCH_PARENT, 320));
            root.addView(tv);
            setContentView(root);
            getWindow().addFlags(
                    android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                    | android.view.WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                    | android.view.WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED);
        });
        try {
            tts = new android.speech.tts.TextToSpeech(this, st -> {
                if (st == android.speech.tts.TextToSpeech.SUCCESS) {
                    try {
                        tts.setLanguage(new java.util.Locale("ru", "RU"));
                        tts.setSpeechRate(0.95f);
                        ttsReady = true;
                    } catch (Throwable t) { Log.e(TAG, "язык: " + t); }
                }
            });
        } catch (Throwable t) { Log.e(TAG, "tts: " + t); }

        String[] need = {"android.permission.CAMERA",
                          "android.permission.BLUETOOTH_CONNECT",
                          "android.permission.BLUETOOTH_SCAN"};
        boolean ok = true;
        for (String p : need)
            if (checkSelfPermission(p) != android.content.pm.PackageManager.PERMISSION_GRANTED)
                ok = false;
        if (!ok) requestPermissions(need, 9);
        else new Thread(this::run).start();
    }

    @Override public void onRequestPermissionsResult(int c, String[] p, int[] g) {
        new Thread(this::run).start();
    }

    /**
     * Уход в фон ОСТАНАВЛИВАЕТ прогон.
     *
     * Звонок, разблокировка по лицу или чужое приложение камеры отбирают поток
     * кадров, но не трогают ни поток зрения, ни поток отправки: слушатель
     * ImageReader просто перестаёт вызываться, цикл видит null и спит дальше.
     * Без этой остановки уставка протухнет (см. wCmdNs) и вал встанет — но
     * лучше остановиться явно и записать причину, чем полагаться на страховку.
     */
    @Override protected void onPause() {
        super.onPause();
        if (running) {
            running = false;
            wCmd = 0.0f; wCmdNs = System.nanoTime();
            Log.i(TAG, "уход в фон: прогон остановлен");
        }
    }

    void run() {
        if (!RUNNING_ONE.compareAndSet(false, true)) {
            Log.i(TAG, "прогон уже идёт — этот экземпляр завершается");
            finish();
            return;
        }
        // Частичный wake lock: без него телефон уходит в дозу и поток
        // замирает молча, не оставив в логе даже ошибки.
        android.os.PowerManager.WakeLock wl = null;
        try {
            android.os.PowerManager pm =
                (android.os.PowerManager) getSystemService(POWER_SERVICE);
            wl = pm.newWakeLock(android.os.PowerManager.PARTIAL_WAKE_LOCK, "surftracker:track");
            wl.acquire(30 * 60 * 1000L);
        } catch (Throwable t) { Log.e(TAG, "wake lock: " + t); }

        String tag = getIntent().getStringExtra("tag");
        if (tag == null) tag = "track";
        String mac = getIntent().getStringExtra("mac");
        String mn = getIntent().getStringExtra("model");
        if (mn == null) mn = "person_w8a32.tflite";
        int seconds = getIntent().getIntExtra("seconds", 60);
        int side = getIntent().getIntExtra("side", 1280);
        float K = getIntent().getFloatExtra("k", 1.2f);
        // ЗНАК ПО УМОЛЧАНИЮ -1, и это исправление ошибки.
        //
        // Прогон 15 августа: ошибка -22.3 град, команда -0.466, вал пошёл в
        // минус — и ошибка стала -22.4, -23.2, -27.2, -31.7. Она РОСЛА, пока
        // камера крутилась; через секунду цель ушла из кадра. Это
        // положительная обратная связь.
        //
        // Почему не поймали раньше: знак «подтвердили» счётом 43 против 28
        // (шестьдесят процентов против сорока — на грани случайности), и при
        // K=0.6 со старым масштабом расхождение шло медленно, а наблюдатель
        // своим движением возвращался в кадр сам. Рост коэффициента вдвое и
        // масштаба на четверть сделал дефект явным.
        int sign = getIntent().getIntExtra("sign", -1);
        boolean dry = getIntent().getBooleanExtra("dry", false);

        // РЕЖИМ «ПОТОК»: камера как НЕЗАВИСИМЫЙ измеритель угла.
        //
        // Модель не запускается вовсе. Вместо неё между соседними кадрами
        // считается горизонтальный сдвиг картинки, и он же есть истинный угол
        // поворота: цена деления 0.038 град на пиксель против кванта энкодера
        // 0.393 — вчетверо мельче.
        //
        // Зачем. Утверждение «врёт датчик, а вал идёт ровно» нельзя проверить
        // самим датчиком. Нужен второй прибор для той же величины, и камера им
        // является по построению — она физически сидит на том же валу.
        // Разность «камера минус энкодер» и есть ошибка энкодера, измеренная
        // напрямую, а не выведенная из реакции на напряжение.
        // ВЫБЕГ ПРИ ПОТЕРЕ ЦЕЛИ. Мгновенный ноль правилен для комнаты и
        // сомнителен для поля: сёрфер уходит за волну, в брызги, за край
        // кадра — и возвращается. Останавливаться на каждое такое пропадание
        // значит терять цель окончательно там, где она пропала на полсекунды.
        //
        // Держим последнюю команду coast секунд, затем линейно гасим за столько
        // же. Держать ДОЛЬШЕ нельзя: слепое вращение уезжает от цели тем
        // дальше, чем дольше её нет, и это ровно тот отказ, который делает
        // возврат невозможным.
        float coast = getIntent().getFloatExtra("coast", 0.4f);

        // РАСШИРЕНИЕ ОКНА ПРИ ДОЛГОЙ ПОТЕРЕ. Окно слежения сужает поле зрения
        // ради разрешения; когда цели нет давно, разрешение уже не нужно, нужен
        // охват. Возвращаем узкое окно сразу после захвата.
        float relost = getIntent().getFloatExtra("relost", 1.5f);

        // ПРЕДЕЛ ДЛИТЕЛЬНОЙ СКОРОСТИ. Развёртка для глаза показала две полосы
        // раскачки: 0.20-0.30 и 0.45-0.50 рад/с, где гармоники привода
        // пересекают резонанс. Транзит через них безопасен, ЗАДЕРЖКА — нет
        // (гистерезис: заход снизу возбуждает, заход сверху нет).
        //
        // Поэтому ограничивается не мгновенная команда, а СГЛАЖЕННАЯ: короткие
        // броски проходят, длительное сидение в полосе — нет.
        float dwell = getIntent().getFloatExtra("dwell", 0.18f);

        // ВОЗВРАТ В ИСХОДНОЕ. После сеанса камера остаётся там, куда доехала,
        // и следующий сеанс начинается с наведения в пустоту. На стенде это
        // уже стоило одного потерянного прогона.
        boolean home = getIntent().getBooleanExtra("home", true);

        // СЦЕНАРИЙ СУФЛЁРА. Один прогон — один механизм: так и выполнимо для
        // наблюдателя, и при отказе видно, ЧТО именно отказало.
        String scen = getIntent().getStringExtra("scen");
        if (scen == null) scen = "";

        boolean flow = getIntent().getBooleanExtra("flow", false);
        float spinW = getIntent().getFloatExtra("spin", 0.15f);

        File dir = new File(getExternalFilesDir(null), "track");
        dir.mkdirs();
        File base = new File(dir, tag);
        StringBuilder j = new StringBuilder("{");
        StringBuilder csv = new StringBuilder(
            "i,t_ms,есть_цель,conf,cx_сенсор,ошибка_град,ω_уставка,"
            + "θ_enc,ω_ramp,статус,watchdog,потолок,рампа,энкодер,кламп,срыв,"
            // Четыре диагностические колонки. Без них прогон не отличает
            // «механизм сработал» от «механизм — заглушка», а это ровно тот
            // класс отказа, которым проект уже болел. Значения писались в
            // строки и раньше, но имён в заголовке не было — разборщик молча
            // выбрасывал их, то есть данные существовали и были нечитаемы.
            + "инференс_мс,такт_мс,Sc,winCx,ω_сглаж,ужатие\n");

        CameraDevice dev = null;
        BluetoothSocket sock = null;
        HandlerThread ht = new HandlerThread("cam");
        ht.start();
        Handler h = new Handler(ht.getLooper());
        Interpreter interp = null;

        // Ждём кнопку. Камера и мотор не трогаются до неё вовсе: прежняя
        // схема стартовала сразу после команды с ноутбука, и наблюдателю
        // приходилось успевать встать в кадр по сообщению в переписке.
        if (!getIntent().getBooleanExtra("auto", false)) {
            try {
                if (!startGate.await(180, TimeUnit.SECONDS)) {
                    Log.i(TAG, "старт не нажат за три минуты — выходим");
                    RUNNING_ONE.set(false);
                    finish();
                    return;
                }
            } catch (Throwable ignored) {}
        }

        try {
            // Режим БЕЗ КАМЕРЫ: интерпретатор в тесном цикле на нулевом буфере.
            // Нужен ровно для одного — отделить цену инференса от цены
            // конвейера камеры. Если здесь быстро, а с камерой медленно, значит
            // виноват конвейер, и спорить больше не о чем.
            if (getIntent().getBooleanExtra("nocam", false)) {
                File m2 = new File(getExternalFilesDir(null), mn);
                Interpreter.Options o2 = new Interpreter.Options();
                o2.setNumThreads(getIntent().getIntExtra("threads", 1));
                if (getIntent().getBooleanExtra("xnn", false))
                    try { Interpreter.Options.class.getMethod("setUseXNNPACK", boolean.class)
                            .invoke(o2, true); } catch (Throwable ignored) {}
                Interpreter it2 = new Interpreter(m2, o2);
                int[] sh2 = it2.getOutputTensor(0).shape();
                float[][][] ob = new float[1][sh2[1]][sh2[2]];
                ByteBuffer nb = ByteBuffer.allocateDirect(4 * 3 * NET * NET)
                        .order(ByteOrder.nativeOrder());
                List<Double> ts = new ArrayList<>();
                long tEnd = System.nanoTime() + (long) seconds * 1_000_000_000L;
                while (System.nanoTime() < tEnd) {
                    nb.rewind();
                    long a = System.nanoTime();
                    it2.run(nb, ob);
                    ts.add((System.nanoTime() - a) / 1e6);
                }
                it2.close();
                java.util.Collections.sort(ts);
                j.append(",\"без_камеры\":true,\"прогонов\":").append(ts.size())
                 .append(",\"инференс_p50\":").append(fmt(ts.get(ts.size() / 2)))
                 .append(",\"инференс_мин\":").append(fmt(ts.get(0)))
                 .append(",\"ok\":true");
                // Ранний возврат минует блок, который пишет первые поля,
                // поэтому JSON начинался бы с запятой и не разбирался. Ставим
                // заглушку вместо пропущенных полей.
                j.insert(1, "\"режим\":\"без_камеры\"");
                return;
            }

            // ---------- камера ----------
            CameraManager cm = getSystemService(CameraManager.class);
            String id = "0";
            CameraCharacteristics ch = cm.getCameraCharacteristics(id);
            StreamConfigurationMap map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            // Разрешение ПОТОКА выбирается, а не берётся максимальное.
            //
            // Полный сенсор 4080x3060 — это 12 мегапикселей на каждый кадр
            // непрерывного потока, и он душит те же ядра, на которых считается
            // модель. Замер: при полном разрешении инференс 590 мс и такт
            // 1.4 к/с, причём ОДИНАКОВО для сёрфовой модели с пятью строками
            // выхода и для COCO с восемьюдесятью четырьмя — то есть время
            // тратится не в модели.
            //
            // Стендовая активность этого не показывала: она делала один снимок
            // и останавливала поток ДО инференса.
            int wantW = getIntent().getIntExtra("cam_w", 1920);
            Size pick = null;
            for (Size sz : map.getOutputSizes(ImageFormat.YUV_420_888)) {
                if (pick == null
                    || Math.abs(sz.getWidth() - wantW) < Math.abs(pick.getWidth() - wantW))
                    pick = sz;
            }
            final int W = pick.getWidth(), H = pick.getHeight();

            // Угловой масштаб. Без него «ошибка в пикселях» осталась бы
            // числом без физического смысла, а коэффициент K пришлось бы
            // подбирать заново при любой смене разрешения или объектива.
            float hfovDeg = 60.0f;
            try {
                SizeF ps = ch.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE);
                float[] fl = ch.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS);
                if (ps != null && fl != null && fl.length > 0)
                    hfovDeg = (float) (2 * Math.toDegrees(Math.atan(ps.getWidth() / (2 * fl[0]))));
            } catch (Throwable t) {
                Log.e(TAG, "поле зрения не прочиталось, беру 60°: " + t);
            }
            // ФОКУС В ПИКСЕЛЯХ, а не «поле зрения делить на ширину».
            //
            // Для прямолинейного объектива x = f*tg(theta), связь угла и
            // пикселя НЕ линейна: в центре кадра градус на пиксель меньше
            // среднего по кадру, а к краям больше. Прежняя формула
            // hfov/W давала среднее и занижала центральный масштаб на 16%
            // (0.0380 против 0.0441 град/пикс при поле 72.9 и ширине 1920).
            //
            // Найдено сравнением с энкодером: камера «проходила» 1271 град там,
            // где энкодер 1439. После поправки 1476 против 1439 — расхождение
            // падает с 11.6% до 2.6%.
            //
            // Цена ошибки была не в отчёте, а в контуре: коэффициент петли
            // слежения считался вокруг заниженного на 16% масштаба.
            final double fPx = (W / 2.0) / Math.tan(Math.toRadians(hfovDeg / 2.0));
            final double degPerPx = Math.toDegrees(1.0 / fPx);   // в центре кадра

            j.append("\"tag\":\"").append(tag).append("\",\"модель\":\"").append(mn)
             .append("\",\"сенсор\":\"").append(W).append("x").append(H)
             .append("\",\"поле_зрения_град\":").append(fmt(hfovDeg))
             .append(",\"град_на_пиксель\":").append(String.format(java.util.Locale.US, "%.5f", degPerPx))
             .append(",\"K\":").append(fmt(K)).append(",\"знак\":").append(sign)
             .append(",\"окно\":").append(side).append(",\"секунд\":").append(seconds)
             .append(",\"выбег\":").append(fmt(coast))
             .append(",\"расширение\":").append(fmt(relost))
             .append(",\"предел_задержки\":").append(fmt(dwell))
             .append(",\"возврат\":").append(home)
             .append(",\"сухой_прогон\":").append(dry);

            // Очередь на 4 кадра, а не на 2. При двух возникает гонка: один
            // кадр держит слушатель в latest, второй — цикл, пока режет кроп;
            // третий приходит, acquireLatestImage падает с «maxImages (2) has
            // already been acquired», и падает на потоке камеры, унося процесс
            // целиком. Симптом со стороны — приложение исчезает без следа,
            // даже без записи в лог, потому что finally не выполняется.
            reader = ImageReader.newInstance(W, H, ImageFormat.YUV_420_888, 4);
            final AtomicReference<Image> latest = new AtomicReference<>();
            reader.setOnImageAvailableListener(r -> {
                // Слушатель обязан быть неубиваемым. Любое исключение здесь
                // прилетает на поток камеры, а не на наш, и обычного try в
                // run() не касается вовсе.
                try {
                    Image im = r.acquireLatestImage();
                    if (im == null) return;
                    Image prev = latest.getAndSet(im);
                    if (prev != null) prev.close();
                } catch (Throwable t) {
                    Log.e(TAG, "слушатель кадров: " + t);
                }
            }, h);

            dev = open(cm, id);
            List<Surface> targets = new ArrayList<>();
            targets.add(reader.getSurface());
            final android.hardware.camera2.CameraCaptureSession[] box =
                new android.hardware.camera2.CameraCaptureSession[1];
            CountDownLatch cfg = new CountDownLatch(1);
            dev.createCaptureSession(targets,
                new android.hardware.camera2.CameraCaptureSession.StateCallback() {
                    public void onConfigured(android.hardware.camera2.CameraCaptureSession s) {
                        box[0] = s; cfg.countDown();
                    }
                    public void onConfigureFailed(android.hardware.camera2.CameraCaptureSession s) {
                        cfg.countDown();
                    }
                }, h);
            if (!cfg.await(8, TimeUnit.SECONDS) || box[0] == null)
                throw new RuntimeException("сессия не собралась");
            CaptureRequest.Builder rq = dev.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
            rq.addTarget(reader.getSurface());
            box[0].setRepeatingRequest(rq.build(), null, h);

            // ---------- модель ----------
            File model = new File(getExternalFilesDir(null), mn);
            if (!flow && !model.exists()) throw new RuntimeException("нет файла модели " + mn);
            if (flow) { j.append(",\"режим\":\"поток\",\"spin\":").append(fmt(spinW)); }
            // Число потоков и XNNPACK — ПАРАМЕТРЫ, а не константы. Четыре
            // потока с XNNPACK дали 550 мс на кадр, тогда как прежний рабочий
            // замер на этом же телефоне давал 183 мс на ОДНОМ потоке и без
            // XNNPACK, да ещё под записью 4K. Больше потоков здесь оказалось
            // хуже, и подбирать это надо перебором, а не рассуждением.
            int threads = getIntent().getIntExtra("threads", 1);
            boolean wantXnn = getIntent().getBooleanExtra("xnn", false);
            Interpreter.Options o = new Interpreter.Options();
            o.setNumThreads(threads);
            if (flow) { /* интерпретатор не поднимаем: в потоке он не нужен */ }
            j.append(",\"потоков\":").append(threads);
            // XNNPACK ОБЯЗАТЕЛЕН. Без него w8a32 считается на медленных ядрах
            // по умолчанию: первый прогон дал 571 мс на кадр вместо ожидаемых
            // десятков, и весь такт слежения провалился до 1.4 к/с. Метод
            // зовётся отражением: в части сборок LiteRT его нет, и прямой
            // вызов не собрался бы вовсе.
            boolean xnn = false;
            if (wantXnn) {
                try {
                    Interpreter.Options.class.getMethod("setUseXNNPACK", boolean.class)
                            .invoke(o, true);
                    xnn = true;
                } catch (Throwable t) {
                    Log.e(TAG, "XNNPACK недоступен: " + t);
                }
            }
            j.append(",\"xnnpack\":").append(xnn);
            int[] osh = new int[]{1, 5, 8400};
            if (!flow) {
                interp = new Interpreter(model, o);
                osh = interp.getOutputTensor(0).shape();
            }
            // Форма СПРАШИВАЕТСЯ, а не берётся константой: у сёрфовой модели
            // 5 строк, у COCO — 84, и захардкоженная пятёрка дала бы не
            // исключение, а тихо неверный разбор.
            float[][][] out = new float[1][osh[1]][osh[2]];
            // Длина полосы, а НЕ полного кадра. Первая редакция оставляла
            // массив длиной NET с нулями по краям, и корреляция при сдвиге
            // совпадала нулями с нулями, залипая на нулевом лаге: камера
            // «прошла» 79 град там, где энкодер 1439. Оконный срез обязан
            // менять ДЛИНУ массива, а не обнулять его часть.
            final int STRIP = NET / 3;
            float[] colPrev = null, colCur = new float[STRIP];

            // КАДРЫ МОМЕНТА ПОТЕРИ.
            //
            // Лог говорит, ЧТО произошло (уверенность упала, скорость была
            // такая-то), но не говорит ПОЧЕМУ: смазало движением, вышел за
            // край, заслонило, попал в контровый свет. Ответ на «почему» есть
            // только в самом кадре.
            //
            // Пишем не видео, а кольцо из последних кадров: при переходе
            // «цель есть» -> «цели нет» выгружаются три кадра до и три после.
            // Полная запись потребовала бы отсматривать десятки секунд ради
            // полусекунды события, а кодек ещё и отобрал бы такт у модели.
            final int RING_N = 3;
            byte[][] ring = new byte[RING_N][];
            int ringAt = 0, dumpLeft = 0, lossIdx = 0;
            boolean prevHit = false;
            double flowAccPx = 0;
            j.append(",\"выход\":\"").append(osh[0]).append("x").append(osh[1])
             .append("x").append(osh[2]).append("\"");

            // ---------- канал ----------
            if (!dry) {
                BluetoothAdapter ad = BluetoothAdapter.getDefaultAdapter();
                if (ad == null || !ad.isEnabled()) throw new RuntimeException("Bluetooth выключен");
                BluetoothDevice bt = (mac != null) ? ad.getRemoteDevice(mac) : null;
                if (bt == null) throw new RuntimeException("не задан mac");
                sock = bt.createInsecureRfcommSocketToServiceRecord(SPP);
                ad.cancelDiscovery();
                sock.connect();
            }
            OutputStream os = dry ? null : sock.getOutputStream();
            InputStream is = dry ? null : sock.getInputStream();

            // Прогрев ISP: первые кадры идут с неустоявшейся экспозицией, и
            // детекций по ним не будет независимо от модели.
            Thread.sleep(1200);

            // ---------- цикл ----------
            ByteBuffer bin = ByteBuffer.allocateDirect(4 * 3 * NET * NET).order(ByteOrder.nativeOrder());
            byte[] rx = new byte[4096];
            long[] sendNs = new long[128];
            int seq = 0, frames = 0, hits = 0, misses = 0;
            // Окно слежения: центр — последняя уверенная детекция. При потере
            // НЕ расширяется: расширение прячет потерю и мешает увидеть, как
            // часто она случается. Для первого прогона важнее честность.
            int winCx = W / 2, winCy = H / 2;
            final int ScNarrow = Math.min(side, Math.min(W, H));
            final int ScWide = Math.min(W, H);
            int Sc = ScNarrow;
            int scanPos = 0;               // фаза пилы обзора при долгой потере
            double lastGoodW = 0;          // последняя команда при живой цели
            long lastGoodNs = 0;           // когда цель видели последний раз
            double wSmooth = 0;            // сглаженная команда для предела задержки
            float homeTheta = Float.NaN;   // угол вала на старте сеанса
            // Реплики: {секунда, фраза}. Проговариваются один раз, когда
            // прогон доходит до указанной секунды.
            String[][] script;
            if (scen.equals("проводка")) script = new String[][]{
                {"0","Стойте на месте"},
                // Быстрым шагом и БЛИЖЕ к камере: спокойный проход через
                // комнату дал максимум 0.157 рад/с при пороге 0.18, то есть
                // пределу нечего было ограничивать. Угловая скорость растёт с
                // приближением к оси, а не с линейной скоростью ног.
                {"8","Идите быстрым шагом близко к камере"},
                {"20","Идите обратно так же быстро"},
                {"32","Стойте. Прогон закончен"}};
            else if (scen.equals("выбег")) script = new String[][]{
                {"0","Стойте на месте"},
                {"5","Идите поперёк кадра"},
                {"6","Спрячьтесь на две секунды и выйдите там же"},
                {"5","Идите дальше"},
                {"5","Стойте на месте"},
                {"4","Спрячьтесь на две секунды и выйдите там же"},
                {"5","Стойте. Прогон закончен"}};
            else if (scen.equals("окно")) script = new String[][]{
                {"0","Стойте в центре"},
                {"6","Уйдите из кадра и не показывайтесь"},
                {"12","Не спеша выйдите в другом конце комнаты"},
                {"10","Стойте. Прогон закончен"}};
            else if (scen.equals("знак")) script = new String[][]{
                // РЕШАЮЩИЙ ТЕСТ ЗНАКА. Цель неподвижна и смещена от центра:
                // верный знак обязан свести ошибку к нулю монотонно, неверный
                // — увести её в рост. Никакой статистики по долям не нужно,
                // ответ виден за две секунды.
                //
                // Ровно этого теста не было в первый раз, и вместо него знак
                // «подтверждали» счётом 43 против 28.
                {"0","Встаньте сбоку от центра и не двигайтесь"},
                {"18","Готово, можно расслабиться"}};
            else script = new String[0][];
            int scriptAt = 0;
            StringBuilder cues = new StringBuilder();
            // Время в сценарии считается ОТ КОНЦА предыдущей реплики, а не от
            // начала прогона.
            //
            // Реплика звучит две-три секунды, и всё это время наблюдатель ещё
            // слушает, а не действует. При отсчёте от начала прогона у него на
            // действие остаётся на столько же меньше, и он не успевает —
            // ровно это и произошло в сценарии «окно», где Hero не успел
            // переползти в другой конец комнаты.
            //
            // Длительность речи оценивается по длине фразы: точного сигнала
            // окончания у TextToSpeech без слушателя нет, а слушатель здесь
            // не окупается.
            double cueBase = 0;

            long t0 = System.nanoTime();
            long lastLoop = t0;


            List<Double> lat = new ArrayList<>();

            // ---------- отправка на РОВНЫХ 10 Гц, отдельным потоком ----------
            //
            // Зрение идёт 5 Гц, и первая редакция слала уставку прямо из его
            // цикла. Замер показал бит потолка экстраполяции в 202 кадрах из
            // 203: приёмник получал уставку раз в 200 мс при потолке 150 и
            // стороже 300. Практического вреда не было только потому, что ω̇
            // нулевое, но бит перестал что-либо означать, а до срабатывания
            // сторожа оставалось 100 мс — один пропущенный кадр останавливал
            // вал. Сторож и сработал один раз за сорок секунд.
            //
            // Развязка: частота КАНАЛА не обязана совпадать с частотой ЗРЕНИЯ.
            // Между кадрами повторяется последняя уставка — приёмник видит
            // ровный поток, запас до сторожа втрое, а потолок снова означает
            // настоящую задержку, а не расписание.
            final OutputStream fos = os;
            final InputStream fis = is;
            final boolean fdry = dry;
            final List<Double> flat = lat;
            Thread sender = new Thread(() -> {
                byte[] sreq = new byte[ProtoV2.REQ_LEN];
                byte[] buf = new byte[4096];
                int bn = 0;
                int sq = 0;
                long next = System.nanoTime();
                while (running) {
                    try {
                        if (!fdry) {
                            int q = sq & 0x7F;
                            ProtoV2.buildReq(sreq, q, wCmd, 0.0f);
                            sendNs[q] = System.nanoTime();
                            fos.write(sreq); fos.flush();
                            sq++;
                            int av = fis.available();
                            if (av > 0) {
                                int g = fis.read(buf, bn, Math.min(av, buf.length - bn));
                                if (g > 0) bn += g;
                                int p = 0;
                                while (bn - p >= ProtoV2.TEL_LEN) {
                                    ProtoV2.Tel t = ProtoV2.parseTel(buf, p);
                                    if (t == null) { p++; continue; }
                                    long snt = sendNs[t.seq];
                                    if (snt != 0) {
                                        synchronized (flat) { flat.add((System.nanoTime() - snt) / 1e6); }
                                        sendNs[t.seq] = 0;
                                    }
                                    lastStatus = t.status; lastTheta = t.theta; lastWRamp = t.wRamp;
                                    telCount++;
                                    int stt = t.status;
                                    if ((stt & ProtoV2.ST_WATCHDOG) != 0) bWd++;
                                    if ((stt & ProtoV2.ST_EXTRAP_CAP) != 0) bCap++;
                                    if ((stt & ProtoV2.ST_RAMP_SAT) != 0) bRamp++;
                                    if ((stt & ProtoV2.ST_ENC_OK) != 0) bEnc++;
                                    if ((stt & ProtoV2.ST_CLAMP) != 0) bClamp++;
                                    if ((stt & ProtoV2.ST_SLIP) != 0) bSlip++;
                                    p += ProtoV2.TEL_LEN;
                                }
                                if (p > 0) { System.arraycopy(buf, p, buf, 0, bn - p); bn -= p; }
                            }
                        }
                        next += 100_000_000L;
                        long sl = next - System.nanoTime();
                        if (sl > 0) Thread.sleep(sl / 1_000_000L, (int) (sl % 1_000_000L));
                        else next = System.nanoTime();
                    } catch (Throwable t) {
                        Log.e(TAG, "поток отправки: " + t);
                        break;
                    }
                }
            });
            sender.start();


            while ((System.nanoTime() - t0) / 1e9 < seconds) {
                double tsec = (System.nanoTime() - t0) / 1e9;
                while (scriptAt < script.length
                        && tsec >= cueBase + Double.parseDouble(script[scriptAt][0])) {
                    say(script[scriptAt][1]);
                    // Копим в отдельный буфер и дописываем ОДИН раз в конце:
                    // прежняя редакция открывала массив в JSON и закрывала его
                    // только на последней реплике, поэтому прогон, кончившийся
                    // раньше сценария, оставлял массив незакрытым и весь лог
                    // переставал разбираться.
                    if (cues.length() > 0) cues.append(",");
                    cues.append("{\"t\":").append(fmt(tsec)).append(",\"текст\":\"")
                        .append(script[scriptAt][1]).append("\"}");
                    // Следующая реплика отсчитывается от конца этой.
                    cueBase = tsec + 0.06 * script[scriptAt][1].length() + 0.6;
                    scriptAt++;
                }
                Image im = latest.getAndSet(null);
                if (im == null) { Thread.sleep(3); continue; }
                long tf = System.nanoTime();

                int cropX = clamp(winCx - Sc / 2, 0, W - Sc);
                int cropY = clamp(winCy - Sc / 2, 0, H - Sc);
                Yuv.crop(im, bin, cropX, cropY, Sc, NET, 0);
                im.close();

                bin.rewind();
                long ti = System.nanoTime();
                double shiftPx = 0;
                if (flow) {
                    // Проекция кадра на горизонталь: суммируем по столбцам.
                    // Для чистой панорамы этого достаточно, а двумерная
                    // корреляция стоила бы в 640 раз дороже без выигрыша.
                    // Берём только ЦЕНТРАЛЬНУЮ треть по горизонтали. Дисторсия
                    // объектива меняет цену деления к краям, и корреляция по
                    // всей ширине смешала бы разные масштабы в одно число.
                    // В центральной трети (±12 град) отличие от центрального
                    // масштаба меньше 2%.
                    java.util.Arrays.fill(colCur, 0f);
                    FloatBuffer fb = bin.asFloatBuffer();
                    final int x0 = NET / 3;
                    for (int c = 0; c < 3; c++)
                        for (int y = NET / 4; y < 3 * NET / 4; y++) {
                            int row0 = (c * NET + y) * NET;
                            for (int x = 0; x < STRIP; x++) colCur[x] += fb.get(row0 + x0 + x);
                        }
                    if (colPrev != null) shiftPx = xcorr(colPrev, colCur, 40);
                    float[] tmp = colPrev; colPrev = colCur;
                    colCur = (tmp != null) ? tmp : new float[STRIP];
                    flowAccPx += shiftPx;
                } else {
                    interp.run(bin, out);
                }
                double infMs = (System.nanoTime() - ti) / 1e6;

                // Лучшая детекция по строке 4. Для COCO это класс 0 = person;
                // строки 5..83 не читаются вовсе, поэтому фильтр по классу
                // достаётся бесплатно.
                float bestC = 0; float bcx = 0, bcy = 0, bw = 0, bh = 0;
                float[][] o0 = out[0];
                for (int a = 0; a < o0[0].length; a++) {
                    float c = o0[4][a];
                    if (c > bestC) {
                        bestC = c; bcx = o0[0][a]; bcy = o0[1][a];
                        bw = o0[2][a]; bh = o0[3][a];   // строки 2 и 3 — ширина и высота
                    }
                }
                if (flow) { bestC = 0; }
                boolean hit = flow ? true : (bestC >= CONF_MIN);
                double errDeg = 0; double w = 0;
                double cxSensor = winCx;
                if (flow) {
                    // Уставка постоянна: меряем ВРАЩЕНИЕ, а не слежение.
                    w = spinW;
                    // «ошибка» в этом режиме — накопленный угол по КАМЕРЕ.
                    errDeg = flowAccPx * degPerPx * (Sc / (double) NET);
                    cxSensor = shiftPx;
                    hits++;
                } else if (hit) {
                    // Координаты выхода НОРМИРОВАНЫ: умножать на сторону сети,
                    // потом на масштаб кропа. Забыть об этом — значит собрать
                    // все рамки в левом верхнем углу.
                    cxSensor = cropX + (bcx * NET) * (Sc / (double) NET);
                    double cySensor = cropY + (bcy * NET) * (Sc / (double) NET);
                    // Через арктангенс, а не умножением: на краю кадра
                    // (±36 град) линейное приближение врёт на четверть.
                    errDeg = Math.toDegrees(Math.atan((cxSensor - W / 2.0) / fPx));
                    w = sign * K * Math.toRadians(errDeg);
                    winCx = (int) cxSensor; winCy = (int) cySensor;

                    // ОКНО СЛЕДУЕТ ЗА РАЗМЕРОМ ЦЕЛИ.
                    //
                    // Фиксированное окно ломается вблизи: кадр потери показал
                    // торс во весь экран — ни головы, ни силуэта. Детектор
                    // людей ищет человеческую фигуру и фрагмент туловища не
                    // узнаёт, причём совершенно правильно.
                    //
                    // Множитель 2.5 к большей стороне рамки: цель занимает
                    // около 40% окна, вокруг остаётся контекст, по которому
                    // фигура и опознаётся. Меньше — теряем силуэт вблизи,
                    // больше — вдали цель схлопывается в несколько пикселей
                    // после уменьшения до 640.
                    double boxSensor = Math.max(bw, bh) * NET * (Sc / (double) NET);
                    if (boxSensor > 1) {
                        int want = (int) Math.round(2.5 * boxSensor);
                        want = Math.max(ScNarrow, Math.min(ScWide, want));
                        // Плавно, а не скачком: резкая смена окна меняет и
                        // масштаб, и содержимое сразу, и следующая детекция
                        // приходит в другой системе координат.
                        Sc = (int) Math.round(0.7 * Sc + 0.3 * want);
                    }
                    hits++;
                    lastGoodW = w; lastGoodNs = System.nanoTime();
                    Sc = ScNarrow;
                } else {
                    misses++;
                    double lost = (lastGoodNs == 0) ? 1e9
                                  : (System.nanoTime() - lastGoodNs) / 1e9;
                    if (lost <= coast) {
                        w = lastGoodW;                       // выбег: держим
                    } else if (lost <= 2 * coast) {
                        w = lastGoodW * (1.0 - (lost - coast) / coast);  // гасим
                    } else {
                        w = 0;
                    }
                    // Долгая потеря — окно обязано ХОДИТЬ, а не просто
                    // расширяться.
                    //
                    // Расширение само по себе оказалось тождественной
                    // операцией: при потоке 1920x1080 min(side=1280, 1080) и
                    // min(W,H)=1080 — одно и то же число. Но даже когда оно
                    // работает (4:3), квадратное окно накрывает максимум
                    // min(W,H)/W = 56% ширины, и цель, ушедшая вбок, остаётся
                    // снаружи навсегда: winCx в ветке промаха не менялся, и
                    // потеря становилась ПОГЛОЩАЮЩИМ состоянием.
                    //
                    // Тот же дефект уже описан в проекте для питоновского
                    // трекера (reports/КРИТИЧЕСКИЙ_ДЕФЕКТ_ОКНО.md), и здесь он
                    // был повторён заново.
                    if (lost > relost) {
                        Sc = ScWide;
                        int span = Math.max(1, W - Sc);
                        scanPos = (scanPos + Sc / 2) % (2 * span);
                        winCx = Sc / 2 + (scanPos <= span ? scanPos : 2 * span - scanPos);
                        winCy = H / 2;
                    }
                }

                // Уставка только ОБНОВЛЯЕТСЯ. Отправкой занят отдельный поток на
                // ровных 10 Гц — см. ниже, зачем.
                // Предел ДЛИТЕЛЬНОЙ скорости. Три дефекта первой редакции,
                // каждый из которых по отдельности отключал механизм:
                //
                // 1. Сглаживание шло ПО КАДРАМ (коэффициент 0.1 на такт), а не
                //    по времени. При такте 200 мс это постоянная около 2 с
                //    вместо заявленных 0.5, и она плыла с загрузкой телефона.
                //    Теперь по dt.
                // 2. Сглаживалась команда СО ЗНАКОМ. Проводка туда-обратно
                //    давала среднее около нуля, и при качании ±0.38 сглаженная
                //    не поднималась выше 0.174 — предел не срабатывал НИ РАЗУ.
                //    Теперь сглаживается модуль.
                // 3. Ужатие было ПРОПОРЦИОНАЛЬНЫМ: выход получался всегда не
                //    меньше dwell, и вал вели сверху вниз ЧЕРЕЗ всю запретную
                //    полосу 0.20-0.30. Теперь клип.
                //
                // И снятие предела при большой ошибке: при 0.18 рад/с догнать
                // цель, требующую 0.25-0.40, невозможно по построению, так что
                // на большой ошибке плавность уступает захвату.
                double dtLoop = (System.nanoTime() - lastLoop) / 1e9;
                if (dtLoop <= 0 || dtLoop > 1.0) dtLoop = 0.2;
                double aSm = Math.min(1.0, dtLoop / 0.5);
                wSmooth += (Math.abs(w) - wSmooth) * aSm;
                double shrink = 1.0;
                // ТОЛЬКО ПРИ ЖИВОЙ ЦЕЛИ. При потере errDeg обнуляется, предел
                // считает ошибку малой, включается и обрезает выбег: в прогоне
                // 15 августа команда после потери держалась -0.180 вместо
                // последней -0.663. Два механизма мешали друг другу, и выбег
                // делал вчетверо меньше задуманного.
                if (hit && dwell > 0 && wSmooth > dwell && Math.abs(w) > dwell
                        && Math.abs(errDeg) < 12.0) {
                    double lim = Math.copySign(dwell, w);
                    shrink = Math.abs(lim) / Math.abs(w);
                    w = lim;
                }
                wCmd = (float) w; wCmdNs = System.nanoTime();
                if (Float.isNaN(homeTheta) && telCount > 0) homeTheta = lastTheta;
                int st = lastStatus; float th = lastTheta, wr = lastWRamp;
                boolean gotTel = telCount > 0;

                // Кольцо кадров: держим последние RING_N в сыром виде тензора.
                if (!flow) {
                    if (ring[ringAt] == null) ring[ringAt] = new byte[NET * NET * 3 * 4];
                    bin.rewind();
                    bin.get(ring[ringAt]);
                    bin.rewind();
                    ringAt = (ringAt + 1) % RING_N;
                }
                if (prevHit && !hit) {          // МОМЕНТ ПОТЕРИ
                    lossIdx++;
                    for (int q = 0; q < RING_N; q++) {
                        int idx = (ringAt + q) % RING_N;
                        if (ring[idx] == null) continue;
                        dumpTensor(ring[idx], new File(dir,
                                tag + "_потеря" + lossIdx + "_до" + (RING_N - q) + ".png"));
                    }
                    dumpLeft = RING_N;
                } else if (dumpLeft > 0 && !flow) {
                    bin.rewind();
                    byte[] cur = new byte[NET * NET * 3 * 4];
                    bin.get(cur); bin.rewind();
                    dumpTensor(cur, new File(dir,
                            tag + "_потеря" + lossIdx + "_после" + (RING_N - dumpLeft + 1) + ".png"));
                    dumpLeft--;
                }
                prevHit = hit;

                long now = System.nanoTime();
                double loopMs = (now - lastLoop) / 1e6;
                lastLoop = now;
                csv.append(frames).append(',').append((int) ((now - t0) / 1e6)).append(',')
                   .append(hit ? 1 : 0).append(',').append(fmt(bestC)).append(',')
                   .append((int) cxSensor).append(',').append(fmt(errDeg)).append(',')
                   .append(fmt(w)).append(',')
                   .append(gotTel ? fmt(th) : "").append(',')
                   .append(gotTel ? fmt(wr) : "").append(',')
                   .append(gotTel ? String.valueOf(st) : "").append(',')
                   .append(bit(st, ProtoV2.ST_WATCHDOG)).append(',')
                   .append(bit(st, ProtoV2.ST_EXTRAP_CAP)).append(',')
                   .append(bit(st, ProtoV2.ST_RAMP_SAT)).append(',')
                   .append(bit(st, ProtoV2.ST_ENC_OK)).append(',')
                   .append(bit(st, ProtoV2.ST_CLAMP)).append(',')
                   .append(bit(st, ProtoV2.ST_SLIP)).append(',')
                   .append(fmt(infMs)).append(',').append(fmt(loopMs)).append(',')
                   .append(Sc).append(',').append(winCx).append(',')
                   .append(fmt(wSmooth)).append(',').append(fmt(shrink)).append('\n');
                frames++;
            }

            // ВОЗВРАТ В ИСХОДНОЕ. Простой П-регулятор по углу: ошибка берётся
            // из телеметрии, команда ограничена как обычная уставка. Идём
            // медленно (0.12 рад/с) — ниже полосы раскачки 0.20-0.30.
            if (home && !dry && !Float.isNaN(homeTheta)) {
                // Пауза перед возвратом. Главный цикл кончился, команда упала в
                // ноль, но вал ещё катится по инерции — и детектор срыва видит
                // расхождение угла с интегралом команды. Это штатный
                // переходный процесс, а в прогоне «окно» он сорвал возврат,
                // потому что ворота приняли его за аварию.
                //
                // Ждём дольше окна детектора (1 с), держа нулевую команду.
                wCmd = 0.0f; wCmdNs = System.nanoTime();
                Thread.sleep(1400);
                // ВОРОТА. Возврат ведёт вал вслепую по телеметрии, поэтому он
                // обязан прерываться, как только телеметрии верить нельзя.
                // Отдельно — БЮДЖЕТ ПУТИ: он единственный закрывает перезапуск
                // STM32, где все биты чистые, а точка отсчёта уже другая.
                long th0 = System.nanoTime();
                double err = 0, travel = 0, err0 = homeTheta - lastTheta;
                String hstat = "ок";
                int badEnc = 0, badSlip = 0;
                long prevNs = System.nanoTime();
                int telAt = telCount;
                long telSeenNs = System.nanoTime();
                while ((System.nanoTime() - th0) / 1e9 < 25.0) {
                    if (telCount != telAt) { telAt = telCount; telSeenNs = System.nanoTime(); }
                    if ((System.nanoTime() - telSeenNs) > 300_000_000L) { hstat = "нет_телеметрии"; break; }
                    // Ворота срабатывают по ТРЁМ подряд плохим кадрам, а не по
                    // одному. В прогоне 15 августа возврат прервался по
                    // «энкодер молчит», хотя энкодер был жив в 469 кадрах из
                    // 473: одного случайного кадра хватило, чтобы убить возврат.
                    if ((lastStatus & ProtoV2.ST_ENC_OK) == 0) badEnc++; else badEnc = 0;
                    if ((lastStatus & ProtoV2.ST_SLIP) != 0)   badSlip++; else badSlip = 0;
                    if (badEnc >= 3)  { hstat = "энкодер_молчит"; break; }
                    // Срыв — по пяти подряд: возврат идёт на 0.12 рад/с, и
                    // единичные срабатывания там ничего не значат.
                    if (badSlip >= 5) { hstat = "срыв"; break; }
                    err = homeTheta - lastTheta;
                    // Порог 0.05 рад, а не 0.02: собственный шум theta около
                    // 0.08 рад, и 0.02 лежит НИЖЕ него — цикл крутился бы до
                    // таймаута, гоняя вал по шуму.
                    if (Math.abs(err) < 0.05) break;
                    double wh = Math.max(-0.12, Math.min(0.12, 0.5 * err));
                    long now2 = System.nanoTime();
                    travel += Math.abs(wh) * (now2 - prevNs) / 1e9;
                    prevNs = now2;
                    if (travel > Math.abs(err0) + 0.17) { hstat = "бюджет_пути"; break; }
                    wCmd = (float) wh; wCmdNs = now2;
                    Thread.sleep(20);
                }
                if (hstat.equals("ок") && (System.nanoTime() - th0) / 1e9 >= 25.0) hstat = "таймаут";
                wCmd = 0.0f; wCmdNs = System.nanoTime();
                Thread.sleep(300);
                j.append(",\"возврат_статус\":\"").append(hstat).append("\"")
                 .append(",\"возврат_путь_рад\":").append(fmt(travel))
                 .append(",\"возврат_ошибка_град\":")
                 .append(hstat.equals("ок") ? fmt(Math.toDegrees(err)) : "null");
            }

            running = false;
            try { sender.join(500); } catch (Throwable ignored) {}
            // Остановить вал ЯВНО. Полагаться на сторож нельзя: он сработает,
            // но через 300 мс и с поднятым битом, то есть штатный выход
            // выглядел бы как отказ связи.
            if (!dry) {
                byte[] stopReq = new byte[ProtoV2.REQ_LEN];
                for (int i = 0; i < 5; i++) {
                    ProtoV2.buildReq(stopReq, i & 0x7F, 0.0f, 0.0f);
                    os.write(stopReq); os.flush();
                    Thread.sleep(60);
                }
            }

            java.util.Collections.sort(lat);
            j.append(",\"кадров\":").append(frames).append(",\"с_целью\":").append(hits)
             .append(",\"без_цели\":").append(misses)
             .append(",\"доля_с_целью\":").append(frames > 0 ? fmt(hits / (double) frames) : "0")
             .append(",\"телеметрии\":").append(telCount)
             .append(",\"протухших_нулей\":").append(staleZeros)
             .append(",\"биты\":{\"watchdog\":").append(bWd)
             .append(",\"потолок\":").append(bCap).append(",\"рампа\":").append(bRamp)
             .append(",\"энкодер\":").append(bEnc).append(",\"кламп\":").append(bClamp)
             .append(",\"срыв\":").append(bSlip).append("}");
            if (!lat.isEmpty())
                j.append(",\"rtt_ms\":{\"p50\":").append(fmt(lat.get(lat.size() / 2)))
                 .append(",\"p95\":").append(fmt(lat.get((int) (0.95 * (lat.size() - 1)))))
                 .append("}");
            if (cues.length() > 0) j.append(",\"реплики\":[").append(cues).append("]");
            j.append(",\"ok\":true");
        } catch (Throwable t) {
            Log.e(TAG, "слежение: " + t, t);
            j.append(",\"ok\":false,\"ошибка\":\"")
             .append(String.valueOf(t).replace('"', '\'')).append("\"");
        } finally {
            running = false;
            try { if (interp != null) interp.close(); } catch (Throwable ignored) {}
            try { if (sock != null) sock.close(); } catch (Throwable ignored) {}
            try { if (dev != null) dev.close(); } catch (Throwable ignored) {}
            try { if (reader != null) reader.close(); } catch (Throwable ignored) {}
            ht.quitSafely();
            try { if (tts != null) { tts.stop(); tts.shutdown(); } } catch (Throwable ignored) {}
            try { if (tone != null) tone.release(); } catch (Throwable ignored) {}
            try { if (wl != null && wl.isHeld()) wl.release(); } catch (Throwable ignored) {}
            try {
                write(new File(base.getPath() + ".json"), j.append("}").toString());
                write(new File(base.getPath() + ".csv"), csv.toString());
            } catch (Throwable ignored) {}
            RUNNING_ONE.set(false);
            Log.i(TAG, "ГОТОВО " + base.getPath());
            finish();
        }
    }

    /**
     * Горизонтальный сдвиг между двумя проекциями, в пикселях.
     *
     * Максимум взаимной корреляции с параболическим уточнением по трём точкам:
     * без него разрешение упёрлось бы в целый пиксель (0.038 град), а нам надо
     * различать доли этого. Профили центрируются перед корреляцией — иначе
     * общий уровень яркости даст ложный максимум на нулевом сдвиге.
     */
    static double xcorr(float[] a, float[] b, int maxLag) {
        int n = a.length;
        double ma = 0, mb = 0;
        for (int i = 0; i < n; i++) { ma += a[i]; mb += b[i]; }
        ma /= n; mb /= n;
        double best = -1e300; int bestLag = 0;
        double[] c = new double[2 * maxLag + 1];
        for (int lag = -maxLag; lag <= maxLag; lag++) {
            double s = 0; int cnt = 0;
            int i0 = Math.max(0, -lag), i1 = Math.min(n, n - lag);
            for (int i = i0; i < i1; i++) { s += (a[i] - ma) * (b[i + lag] - mb); cnt++; }
            if (cnt > 0) s /= cnt;
            c[lag + maxLag] = s;
            if (s > best) { best = s; bestLag = lag; }
        }
        int k = bestLag + maxLag;
        if (k <= 0 || k >= 2 * maxLag) return bestLag;
        double y0 = c[k - 1], y1 = c[k], y2 = c[k + 1];
        double d = 2 * (2 * y1 - y0 - y2);
        double sub = (d != 0) ? (y2 - y0) / d : 0;
        if (sub < -1 || sub > 1) sub = 0;
        return bestLag + sub;
    }

    /** Тензор NCHW float32 [0..1] -> PNG. Ошибки глушатся: диагностика не
     *  имеет права уронить прогон. */
    static void dumpTensor(byte[] raw, File f) {
        if (raw == null || f == null) return;
        try {
            java.nio.FloatBuffer fb = java.nio.ByteBuffer.wrap(raw)
                    .order(ByteOrder.nativeOrder()).asFloatBuffer();
            int[] px = new int[NET * NET];
            int plane = NET * NET;
            for (int i = 0; i < plane; i++) {
                int r = (int) (Math.max(0, Math.min(1, fb.get(i))) * 255);
                int g = (int) (Math.max(0, Math.min(1, fb.get(plane + i))) * 255);
                int b2 = (int) (Math.max(0, Math.min(1, fb.get(2 * plane + i))) * 255);
                px[i] = 0xFF000000 | (r << 16) | (g << 8) | b2;
            }
            android.graphics.Bitmap bm = android.graphics.Bitmap.createBitmap(
                    px, NET, NET, android.graphics.Bitmap.Config.ARGB_8888);
            try (FileOutputStream os2 = new FileOutputStream(f)) {
                bm.compress(android.graphics.Bitmap.CompressFormat.PNG, 90, os2);
            }
            bm.recycle();
        } catch (Throwable t) { Log.e(TAG, "кадр потери: " + t); }
    }

    static int bit(int st, int m) { return (st & m) != 0 ? 1 : 0; }
    static int clamp(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }
    static String fmt(double v) { return String.format(java.util.Locale.US, "%.4f", v); }

    static Size biggest(Size[] all) {
        Size b = all[0];
        for (Size s : all) if ((long) s.getWidth() * s.getHeight() > (long) b.getWidth() * b.getHeight()) b = s;
        return b;
    }

    static void write(File f, String s) throws Exception {
        try (OutputStreamWriter w = new OutputStreamWriter(new FileOutputStream(f), "UTF-8")) {
            w.write(s);
        }
    }

    CameraDevice open(CameraManager cm, String id) throws Exception {
        final CameraDevice[] box = new CameraDevice[1];
        final CountDownLatch l = new CountDownLatch(1);
        HandlerThread t = new HandlerThread("open"); t.start();
        cm.openCamera(id, new CameraDevice.StateCallback() {
            public void onOpened(CameraDevice d) { box[0] = d; l.countDown(); }
            public void onDisconnected(CameraDevice d) { d.close(); l.countDown(); }
            public void onError(CameraDevice d, int e) { d.close(); l.countDown(); }
        }, new Handler(t.getLooper()));
        if (!l.await(8, TimeUnit.SECONDS) || box[0] == null)
            throw new RuntimeException("камера не открылась");
        return box[0];
    }
}
