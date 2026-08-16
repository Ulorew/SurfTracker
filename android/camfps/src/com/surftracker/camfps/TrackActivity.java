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
import android.media.MediaRecorder;
import android.graphics.SurfaceTexture;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.params.OutputConfiguration;
import android.hardware.camera2.params.SessionConfiguration;
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

    /**
     * ЖИВОЕ СОСТОЯНИЕ ПРОГОНА — то, что видно от места съёмки.
     *
     * Заведено по двум случаям, которые уже стоили прогонов. Первый: «не вижу,
     * когда что-то залилось, приходится гадать» — с трёх метров экран не
     * читался вовсе, и наблюдатель не знал, начался ли прогон. Второй:
     * «кстати, горячо и включено» — прогон давно кончился, а телефон грелся,
     * и понять это можно было только на ощупь.
     *
     * Поля volatile и пишутся из петли слежения БЕЗ блокировок: показания
     * обновляются по таймеру раз в полсекунды, и рассинхронизация на один
     * такт здесь безразлична, а замок в петле — нет.
     */
    volatile android.widget.TextView liveView;
    volatile boolean uiTracking, uiLink, uiRec, uiDry, uiDone;
    volatile int uiTicks, uiHits, uiLoss;
    volatile long uiEndsAtMs;
    volatile String uiPhrase = "";
    /** Причина отказа для экрана. null — отказа не было. */
    volatile String uiError;
    volatile android.widget.Button stopButton, startButton;
    /** Просьба остановиться, поданная кнопкой. Петля проверяет её сама. */
    volatile boolean stopRequested;
    // Защёлка старта: прогон не начинается, пока наблюдатель не нажал кнопку.
    // Так согласование момента уходит из переписки в приложение — Hero жмёт,
    // когда встал, а не когда прочитал сообщение.
    final CountDownLatch startGate = new CountDownLatch(1);

    /**
     * Обновление живого состояния раз в полсекунды.
     *
     * По таймеру, а не из петли слежения. Петля идёт 5-12 раз в секунду, и
     * рисовать по каждому такту значило бы отнимать у неё время на разметку
     * текста ради частоты, которую глаз всё равно не читает.
     */
    void startLiveTicker() {
        final android.os.Handler h = new android.os.Handler(android.os.Looper.getMainLooper());
        h.post(new Runnable() {
            @Override public void run() {
                android.widget.TextView v = liveView;
                if (v != null) {
                    v.setText(liveText());
                    v.setBackgroundColor(LiveStatus.color(uiDone, uiTicks, uiTracking, uiError));
                    // Кнопка после конца прогона закрывает экран, а не
                    // останавливает то, что уже стоит.
                    // Кнопка старта после начала прогона бесполезна и только
                    // занимает половину экрана, на котором надо читать состояние.
                    android.widget.Button gb = startButton;
                    if (gb != null && (uiTicks > 0 || uiDone || uiError != null)
                            && gb.getVisibility() == android.view.View.VISIBLE)
                        gb.setVisibility(android.view.View.GONE);
                    android.widget.Button sb = stopButton;
                    if (sb != null && (uiDone || uiError != null) && sb.isEnabled()) {
                        sb.setText("ЗАКРЫТЬ");
                        sb.setOnClickListener(x -> finish());
                    }
                }
                if (!isFinishing()) h.postDelayed(this, 500);
            }
        });
    }

    /** Строки состояния берутся из LiveStatus — там их достаёт стенд
     *  tools/windowing/live_status_check. */
    String liveText() {
        return LiveStatus.text(uiDone, uiTicks, uiHits, uiLoss, uiTracking,
                uiDry, uiLink, uiRec, uiEndsAtMs - System.currentTimeMillis(), uiError);
    }

    void say(String phrase) {
        Log.i(TAG, "СУФЛЁР: " + phrase);
        uiPhrase = phrase;
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
    MediaRecorder recorder;
    Surface recSurface, previewSurface;
    SurfaceTexture previewTexture;

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
            // Крупное состояние. Размер выбран не на глаз: наблюдатель стоит в
            // трёх метрах и должен различать «ведёт» и «ищет» боковым зрением,
            // не подходя к телефону — подходить нельзя, он в кадре.
            android.widget.TextView live = new android.widget.TextView(this);
            live.setTextSize(44);
            live.setPadding(0, 30, 0, 30);
            live.setText("");
            liveView = live;

            android.widget.Button stop = new android.widget.Button(this);
            stop.setTextSize(24);
            stop.setText("ОСТАНОВИТЬ");
            stopButton = stop;
            startButton = go;
            stop.setOnClickListener(v -> {
                // Просьба, а не убийство процесса: петле надо доиграть
                // остановку вала, дописать лог и снять напряжение. Прежде
                // прогон обрывали через force-stop, и вал оставался под
                // командой до срабатывания сторожа.
                stopRequested = true;
                stop.setEnabled(false);
                stop.setText("останавливаю...");
            });

            root.addView(go, new android.widget.LinearLayout.LayoutParams(
                    android.widget.LinearLayout.LayoutParams.MATCH_PARENT, 320));
            root.addView(live);
            root.addView(tv);
            root.addView(stop, new android.widget.LinearLayout.LayoutParams(
                    android.widget.LinearLayout.LayoutParams.MATCH_PARENT, 180));
            setContentView(root);
            startLiveTicker();
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

        // ИМЯ ПАПКИ = ВРЕМЯ + метка.
        //
        // Время впереди по двум причинам. Оно даёт порядок: список прогонов
        // сортируется по имени, и новые оказываются сверху сами, без разбора
        // дат внутри файлов. И оно даёт различимость: раньше два прогона с
        // одной меткой писались в одни и те же файлы, второй молча затирал
        // первый — а с папкой затирание было бы ещё хуже, прогоны смешались бы
        // в одном каталоге, и разобрать, от какого запуска какой файл, стало
        // бы нечем.
        String stamp = new java.text.SimpleDateFormat("yyMMdd_HHmm",
                java.util.Locale.US).format(new java.util.Date());
        // Разрешатель настроек: интент -> сохранённое на экране -> умолчание.
        // Правило и умолчания живут в RunSettings, проверяются стендом
        // tools/windowing/run_settings_check. Здесь только применение.
        final Cfg cfg = new Cfg(getIntent(), getSharedPreferences("прогон", MODE_PRIVATE));

        String tag = cfg.s("tag");
        tag = (tag == null || tag.isEmpty()) ? stamp : (stamp + "_" + tag);
        String mac = cfg.s("mac");
        if (mac != null && mac.isEmpty()) mac = null;   // пусто = искать сохранённый
        String mn = cfg.s("model");
        int seconds = cfg.i("seconds");
        int side = cfg.i("side");
        float K = cfg.f("k");
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
        int sign = cfg.i("sign");
        boolean dry = cfg.b("dry");

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
        float coast = cfg.f("coast");

        // РАСШИРЕНИЕ ОКНА ПРИ ДОЛГОЙ ПОТЕРЕ. Окно слежения сужает поле зрения
        // ради разрешения; когда цели нет давно, разрешение уже не нужно, нужен
        // охват. Возвращаем узкое окно сразу после захвата.
        float relost = cfg.f("relost");

        // ПРЕДЕЛ ДЛИТЕЛЬНОЙ СКОРОСТИ. Развёртка для глаза показала две полосы
        // раскачки: 0.20-0.30 и 0.45-0.50 рад/с, где гармоники привода
        // пересекают резонанс. Транзит через них безопасен, ЗАДЕРЖКА — нет
        // (гистерезис: заход снизу возбуждает, заход сверху нет).
        //
        // Поэтому ограничивается не мгновенная команда, а СГЛАЖЕННАЯ: короткие
        // броски проходят, длительное сидение в полосе — нет.
        float dwell = cfg.f("dwell");

        // ВОЗВРАТ В ИСХОДНОЕ. После сеанса камера остаётся там, куда доехала,
        // и следующий сеанс начинается с наведения в пустоту. На стенде это
        // уже стоило одного потерянного прогона.
        boolean home = cfg.b("home");

        // СЦЕНАРИЙ СУФЛЁРА. Один прогон — один механизм: так и выполнимо для
        // наблюдателя, и при отказе видно, ЧТО именно отказало.
        String scen = cfg.s("scen");
        if (scen == null) scen = "";

        // ЗАПИСЬ ПРОГОНА. Пишутся кадры В ТОМ ВИДЕ, В КАКОМ ИХ ВИДЕЛА МОДЕЛЬ,
        // то есть уже кроп и уменьшение. Полный кадр показал бы больше
        // контекста, но не ответил бы на главный вопрос «почему не узнала» —
        // модель полного кадра не видит вовсе.
        //
        // JPEG, а не PNG: при 5 кадрах в секунду сжатие PNG отняло бы у цикла
        // больше, чем стоит разница в качестве для разбора.
        boolean rec = cfg.b("rec");
        // ПОЛНОЦЕННАЯ ЗАПИСЬ — то, ради чего изделие и делается: видео с
        // камеры, которая ведёт цель. Кадры модели (rec) отвечают на вопрос
        // «почему потеряла», а это — собственно результат.
        boolean video = cfg.b("video");

        boolean flow = getIntent().getBooleanExtra("flow", false);
        float spinW = getIntent().getFloatExtra("spin", 0.15f);

        // ПРОГОН — ЭТО ПАПКА, а не россыпь файлов с общим префиксом.
        //
        // Раньше видео, лог, конфигурация и кадры лежали в одном каталоге
        // вперемешку с прогонами всех прошлых дней, и на вопрос «где смотреть»
        // приходилось отвечать путём с точностью до имени файла. Разбор
        // требовал стянуть три файла и не перепутать, от одного ли они запуска.
        //
        // Имя папки — метка прогона; внутри имена ПОСТОЯННЫЕ, поэтому любой
        // разборщик получает путь вида <прогон>/лог.csv, не зная метки.
        File dir = new File(getExternalFilesDir(null), "track");
        File runDir = new File(dir, tag);
        runDir.mkdirs();
        File base = new File(runDir, "прогон");
        File recDir = new File(runDir, "кадры");
        if (rec) recDir.mkdirs();
        StringBuilder j = new StringBuilder("{");
        // НАСТРОЙКИ ПИШУТСЯ СРАЗУ, до камеры и модели.
        //
        // Раньше они уходили в json уже после открытия камеры, и прогон,
        // упавший раньше, оставлял файл с одной лишь строкой ошибки: понять,
        // с какими параметрами он пытался идти, было нельзя. А отказ на старте
        // — как раз тот случай, когда это нужнее всего.
        //
        // Здесь только то, что известно ДО железа. Величины, вычисляемые из
        // камеры (сенсор, поле зрения, градусы на пиксель), дописываются ниже,
        // когда камера открыта.
        j.append("\"tag\":\"").append(tag).append("\",\"модель\":\"").append(mn)
         .append("\",\"K\":").append(fmt(K)).append(",\"знак\":").append(sign)
         .append(",\"окно\":").append(side).append(",\"секунд\":").append(seconds)
         .append(",\"выбег\":").append(fmt(coast))
         .append(",\"перезахват\":").append(fmt(relost))
         .append(",\"удержание\":").append(fmt(dwell))
         .append(",\"без_мотора\":").append(dry)
         .append(",\"возврат\":").append(home)
         .append(",\"видео\":").append(video)
         .append(",\"кадры\":").append(rec);
        StringBuilder csv = new StringBuilder(
            "i,t_ms,есть_цель,conf,cx_сенсор,ошибка_град,ω_уставка,"
            + "θ_enc,ω_ramp,статус,watchdog,потолок,рампа,энкодер,кламп,срыв,"
            // Четыре диагностические колонки. Без них прогон не отличает
            // «механизм сработал» от «механизм — заглушка», а это ровно тот
            // класс отказа, которым проект уже болел. Значения писались в
            // строки и раньше, но имён в заголовке не было — разборщик молча
            // выбрасывал их, то есть данные существовали и были нечитаемы.
            + "инференс_мс,такт_мс,Sc,winCx,ω_сглаж,ужатие,"
            // Решение трекера: сколько было кандидатов и насколько выбранный
            // близок к предсказанию. Без них нельзя отличить «вёл того же»
            // от «в кадре был только один» — а именно это и проверяется на
            // сценарии с двумя людьми.
            + "кандидатов,до_предсказания,порог,состояние,промахов,"
            // Рамка в координатах ТЕНЗОРА (0..640): именно её надо рисовать
            // поверх записанного кадра, а пересчёт в сенсорные пиксели для
            // этого пришлось бы обращать.
            + "bx,by,размер_детекции,размер_фильтра\n");

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

            // Только вычисленное из камеры: остальное записано выше, до неё.
            j.append(",\"сенсор\":\"").append(W).append("x").append(H)
             .append("\",\"поле_зрения_град\":").append(fmt(hfovDeg))
             .append(",\"град_на_пиксель\":").append(String.format(java.util.Locale.US, "%.5f", degPerPx));
            // Выбег, перезахват, удержание, возврат и режим без мотора отсюда
            // УБРАНЫ: они пишутся выше, до камеры. Дублировать их здесь нельзя
            // — разбор берёт ПЕРВОЕ вхождение ключа, и вторая запись под другим
            // именем («расширение» вместо «перезахват») жила бы в файле молча,
            // никем не читаемая, но выглядящая как настройка.

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

            // Запись: превью + рекордер + анализ. Превью в наборе обязательно —
            // это проверенная на этом аппарате комбинация потоков (см.
            // reports/ТЕЛЕФОН.md, «PRIV 1080p + PRIV 4K + YUV max»), и без
            // него сессия на части устройств не собирается.
            if (video) {
                previewTexture = new SurfaceTexture(0);
                previewTexture.setDefaultBufferSize(1920, 1080);
                previewSurface = new Surface(previewTexture);
                targets.add(previewSurface);

                recorder = new MediaRecorder();
                recorder.setVideoSource(MediaRecorder.VideoSource.SURFACE);
                // Параметры ИЗ ПРОФИЛЯ УСТРОЙСТВА, а не назначенные руками:
                // заданные вручную 3840x2160 на 40 Мбит/с давали ошибку
                // дорожки на девятой секунде и вешали весь конвейер.
                // Производитель знает про свой кодировщик больше.
                // Качество выбирается, а не берётся максимальным. Замер:
                // запись 4K съедает четверть такта слежения (196 -> 252 мс,
                // 5.1 -> 3.97 к/с) и роняет удержание цели с 98% до 77%.
                // Для разбора поведения 1080p достаточно, а слежение при нём
                // остаётся на своей частоте.
                // ТРИ НЕЗАВИСИМЫЕ ОСИ, а не одна ступенчатая ручка.
                //
                // Кодировщик грузится произведением «пиксели × кадры/с», и
                // разрешение — лишь один сомножитель. Ступени 4K/1080/720
                // прыгают вчетверо по площади и не дают ничего между; между
                // тем частота кадров у нас избыточна (камера ведёт цель
                // плавно, 24 к/с для просмотра не хуже 30, а стоит на пятую
                // часть меньше), а битрейт влияет на запись в память, но не на
                // счёт.
                //
                //   quality  2160 | 1440 | 1080 | 720   — площадь кадра
                //   fps      число                      — нагрузка линейно
                //   mbps     число                      — только ввод-вывод
                //
                // Профиль всё равно берётся у устройства и лишь правится:
                // назначать параметры целиком руками уже пробовали, дорожка
                // падала на девятой секунде.
                String q = cfg.s("quality");
                int qid;
                if ("720".equals(q))       qid = android.media.CamcorderProfile.QUALITY_720P;
                else if ("1080".equals(q)) qid = android.media.CamcorderProfile.QUALITY_1080P;
                else if ("1440".equals(q)) qid = android.media.CamcorderProfile.QUALITY_QHD;
                else                        qid = android.media.CamcorderProfile.QUALITY_2160P;
                // Откат ВНИЗ по лестнице до первого поддержанного. Не вверх:
                // если запрошенное качество аппарат не тянет, подниматься выше
                // тем более незачем.
                if (!android.media.CamcorderProfile.hasProfile(0, qid)) {
                    int[] ladder = { android.media.CamcorderProfile.QUALITY_2160P,
                                     android.media.CamcorderProfile.QUALITY_QHD,
                                     android.media.CamcorderProfile.QUALITY_1080P,
                                     android.media.CamcorderProfile.QUALITY_720P };
                    int start = 0;
                    for (int i = 0; i < ladder.length; i++) if (ladder[i] == qid) start = i;
                    int chosen = android.media.CamcorderProfile.QUALITY_HIGH;
                    for (int i = start; i < ladder.length; i++)
                        if (android.media.CamcorderProfile.hasProfile(0, ladder[i])) { chosen = ladder[i]; break; }
                    Log.w(TAG, "профиль " + q + " не поддержан, откат вниз");
                    qid = chosen;
                }
                android.media.CamcorderProfile prof =
                        android.media.CamcorderProfile.hasProfile(0, qid)
                        ? android.media.CamcorderProfile.get(0, qid)
                        : android.media.CamcorderProfile.get(0,
                                android.media.CamcorderProfile.QUALITY_HIGH);
                int wantFps = getIntent().getIntExtra("fps", 0);
                if (wantFps > 0) prof.videoFrameRate = wantFps;
                int wantMbps = getIntent().getIntExtra("mbps", 0);
                if (wantMbps > 0) prof.videoBitRate = wantMbps * 1000000;
                File vf = new File(runDir, "видео.mp4");
                recorder.setOutputFormat(prof.fileFormat);
                recorder.setOutputFile(vf.getAbsolutePath());
                recorder.setVideoEncoder(prof.videoCodec);
                recorder.setVideoSize(prof.videoFrameWidth, prof.videoFrameHeight);
                recorder.setVideoFrameRate(prof.videoFrameRate);
                recorder.setVideoEncodingBitRate(prof.videoBitRate);
                recorder.setOnErrorListener((mr, what, extra) ->
                        Log.e(TAG, "рекордер ОШИБКА what=" + what + " extra=" + extra));
                recorder.prepare();
                recSurface = recorder.getSurface();
                targets.add(recSurface);
                j.append(",\"запись\":\"").append(prof.videoFrameWidth).append("x")
                 .append(prof.videoFrameHeight).append("@").append(prof.videoFrameRate)
                 .append("\"");
            }
            final CameraCaptureSession[] box = new CameraCaptureSession[1];
            CountDownLatch sessionReady = new CountDownLatch(1);   // защёлка настройки сессии камеры
            List<OutputConfiguration> cfgs = new ArrayList<>();
            for (Surface sf : targets) cfgs.add(new OutputConfiguration(sf));
            dev.createCaptureSession(new SessionConfiguration(
                    SessionConfiguration.SESSION_REGULAR, cfgs, r -> h.post(r),
                    new CameraCaptureSession.StateCallback() {
                        public void onConfigured(CameraCaptureSession s2) { box[0] = s2; sessionReady.countDown(); }
                        public void onConfigureFailed(CameraCaptureSession s2) { sessionReady.countDown(); }
                    }));
            if (!sessionReady.await(10, TimeUnit.SECONDS) || box[0] == null)
                throw new RuntimeException("сессия не собралась");
            CaptureRequest.Builder rq = dev.createCaptureRequest(
                    video ? CameraDevice.TEMPLATE_RECORD : CameraDevice.TEMPLATE_PREVIEW);
            rq.addTarget(reader.getSurface());
            if (video) { rq.addTarget(previewSurface); rq.addTarget(recSurface); }
            box[0].setRepeatingRequest(rq.build(), null, h);
            if (video) {
                recorder.start();
                Log.i(TAG, "запись пошла");
            }

            // ---------- модель ----------
            File model = new File(getExternalFilesDir(null), mn);
            if (!flow && !model.exists()) throw new RuntimeException("нет файла модели " + mn);
            if (flow) { j.append(",\"режим\":\"поток\",\"spin\":").append(fmt(spinW)); }
            // Число потоков и XNNPACK — ПАРАМЕТРЫ, а не константы. Четыре
            // потока с XNNPACK дали 550 мс на кадр, тогда как прежний рабочий
            // замер на этом же телефоне давал 183 мс на ОДНОМ потоке и без
            // XNNPACK, да ещё под записью 4K. Больше потоков здесь оказалось
            // хуже, и подбирать это надо перебором, а не рассуждением.
            int threads = cfg.i("threads");
            boolean wantXnn = cfg.b("xnn");
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
            // ---- то, из чего складывается ИТОГ прогона ----
            //
            // Считается по ходу, а не разбором лога задним числом: разборщик
            // пришлось бы держать в двух местах (телефон и ноут), и он бы
            // разъехался с форматом при первой же новой колонке.
            //
            // Потеря считается по СОСТОЯНИЮ ТРЕКЕРА, а не по «нет детекции на
            // такте». Один пустой такт — это промах, а не потеря; смешивать их
            // значит показывать десятки потерь там, где цель ни разу не терялась.
            int nLoss = 0, nReacq = 0;
            int prevTrkStatus = Tracker.LOST;
            List<Double> scHist = new ArrayList<>();
            List<Double> infHist = new ArrayList<>();
            List<Double> loopHist = new ArrayList<>();
            int missStreak = 0, missStreakMax = 0;
            // Окно слежения: центр — последняя уверенная детекция. При потере
            // НЕ расширяется: расширение прячет потерю и мешает увидеть, как
            // часто она случается. Для первого прогона важнее честность.
            int winCx = W / 2, winCy = H / 2;
            final int ScNarrow = Math.min(side, Math.min(W, H));
            final int ScWide = Math.min(W, H);
            int Sc = ScNarrow;
            // Петля слежения — перенос замороженного офлайн-трекера
            // (tracking-v1-frozen). Сличён с оригиналом на синтетическом
            // сценарии: tools/windowing/port_check/check.py.
            Tracker trk = new Tracker(W, H);
            final int MAX_DET = 16;
            float[][] dets = new float[MAX_DET][3];
            double[][] detsPx = new double[MAX_DET][3];
            long prevTickNs = 0;

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
            else if (scen.equals("двое")) script = new String[][]{
                // ПРОВЕРКА УДЕРЖАНИЯ ЦЕЛИ. Решающий момент — когда двое
                // расходятся: камера обязана вести того, кого вела, даже если
                // второй крупнее и «увереннее».
                //
                // Раньше выбор шёл по уверенности, и на этом сценарии камера
                // ушла бы за более контрастным. На воде сёрферов несколько,
                // и такой перескок портит съёмку целиком, а не слегка.
                {"0","Первый встаёт в центр кадра. Второй ждёт в стороне"},
                {"8","Второй входит в кадр и встаёт рядом с первым"},
                {"8","Разойдитесь в разные стороны, не спеша"},
                {"8","Стойте. Прогон закончен"}};
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


            // Просьба об остановке проверяется В УСЛОВИИ ЦИКЛА, а не обрывом:
            // дальше по коду идут остановка вала, возврат в исходное и запись
            // файлов, и все они обязаны отработать. Кнопка «Остановить» должна
            // кончать прогон так же, как его кончает время.
            uiEndsAtMs = System.currentTimeMillis() + (long) seconds * 1000L;
            uiDry = dry; uiRec = video;
            while (!stopRequested && (System.nanoTime() - t0) / 1e9 < seconds) {
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
                // Все кандидаты, а не сильнейший: выбор делает трекер по
                // близости к предсказанию, и ему нужно из чего выбирать.
                float[][] o0 = out[0];
                int nDet = flow ? 0 : Tracker.nms(o0, o0[0].length, dets, MAX_DET);
                float bestC = 0; float bcx = 0, bcy = 0, bw = 0, bh = 0;
                for (int a = 0; a < o0[0].length; a++) if (o0[4][a] > bestC) bestC = o0[4][a];
                if (flow) { bestC = 0; }
                // Такт трекера: перевод детекций в пиксели сенсора, план окна,
                // выбор ближайшего к предсказанию.
                long nowTickNs = System.nanoTime();
                double dtTick = (prevTickNs == 0) ? 0.2 : (nowTickNs - prevTickNs) / 1e9;
                prevTickNs = nowTickNs;
                if (dtTick <= 0 || dtTick > 2.0) dtTick = 0.2;
                int chosenDet = -1;
                double distToPred = Double.NaN, gateNow = 0;
                boolean stepped = false;
                // Сколько длится потеря — нужно ЗДЕСЬ, до решения трекера:
                // затравка после долгой потери открывается по этому же числу.
                double lostSecNow = (lastGoodNs == 0) ? 0
                        : (System.nanoTime() - lastGoodNs) / 1e9;
                if (!flow && nDet > 0) {
                    double scale = Sc / (double) NET;
                    for (int q = 0; q < nDet; q++) {
                        detsPx[q][0] = cropX + dets[q][0] * NET * scale;
                        detsPx[q][1] = cropY + dets[q][1] * NET * scale;
                        detsPx[q][2] = dets[q][2] * NET * scale;
                    }
                    // Затравка открывается заново после ДОЛГОЙ потери.
                    //
                    // Без этого потеря — поглощающее состояние: приём навсегда
                    // заперт в круге вокруг последнего предсказания, и цель,
                    // полностью видимая в кадре, но вне этого круга, не
                    // принимается никогда. Тот же дефект уже был найден в
                    // питоновском трекере и описан отдельным отчётом.
                    boolean mayReseed = !trk.initialized
                            || (trk.status == Tracker.LOST && lostSecNow > relost);
                    if (mayReseed) {
                        // Затравка: цели ещё нет, брать по близости не к чему.
                        // Берём сильнейшую — единственный случай, когда
                        // уверенность участвует в выборе.
                        // Индекс 0: nms сортирует по УБЫВАНИЮ уверенности, и
                        // первый элемент — самый уверенный.
                        //
                        // Прежняя строка сравнивала dets[q][2], а это РАЗМЕР:
                        // в dets уверенности нет вовсе, nms кладёт только
                        // центр и размер. То есть затравка бралась по самому
                        // КРУПНОМУ объекту в кропе, а комментарий рядом
                        // утверждал «берём сильнейшую».
                        int b = 0;
                        if (bestC >= CONF_MIN) {
                            trk.seed(detsPx[b][0], detsPx[b][1], detsPx[b][2]);
                            chosenDet = b;
                        }
                    } else {
                        // Весь такт — одним вызовом. Расстояние и радиус приёма
                        // приходят ОТТУДА ЖЕ, где принималось решение: пока их
                        // считали здесь, два диагностических столбца успели
                        // разойтись с логикой и врали в отчёт.
                        Tracker.Tick tk = trk.step(detsPx, nDet, dtTick);
                        chosenDet = tk.chosen;
                        distToPred = tk.dist;
                        gateNow = tk.gate;
                        stepped = true;
                    }
                }
                // advance только если такт НЕ прошёл через step(): внутри него
                // промах уже обработан, второй вызов сдвинул бы убеждение дважды.
                if (!flow && !stepped && chosenDet < 0 && trk.initialized) trk.advance(dtTick);
                boolean hit = flow ? true : (chosenDet >= 0);
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
                    cxSensor = detsPx[chosenDet][0];
                    double cySensor = detsPx[chosenDet][1];
                    // Через арктангенс, а не умножением: на краю кадра
                    // (±36 град) линейное приближение врёт на четверть.
                    errDeg = Math.toDegrees(Math.atan((cxSensor - W / 2.0) / fPx));
                    w = sign * K * Math.toRadians(errDeg);
                    // Окно ведёт ТРЕКЕР: центр — предсказание, сторона — из
                    // фильтра размера. Прежняя телефонная версия ставила окно
                    // по последней детекции, то есть на такт позади цели.
                    double sideNext = trk.windowSide();
                    winCx = (int) trk.planCx(dtTick, sideNext);
                    winCy = (int) trk.planCy(dtTick, sideNext);
                    Sc = (int) Math.round(sideNext);

                    hits++;
                    lastGoodW = w; lastGoodNs = System.nanoTime();
                    // Sc здесь НЕ трогается: окном владеет трекер. Прежняя
                    // строка Sc = ScNarrow осталась от правки выбега и шла
                    // ПОСЛЕ присвоения из трекера — каждый такт выбрасывала
                    // верно посчитанное окно 1440 и возвращала минимальное 640.
                    // Порог приёма при этом считался от верного окна, а кроп от
                    // затёртого: цель заполняла кадр модели целиком, соседи в
                    // него не помещались, и трекеру было не из чего выбирать.
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
                if (rec && !flow) {
                    bin.rewind();
                    byte[] cur = new byte[NET * NET * 3 * 4];
                    bin.get(cur); bin.rewind();
                    dumpJpeg(cur, new File(recDir, String.format(
                            java.util.Locale.US, "%05d.jpg", frames)));
                }
                if (prevHit && !hit) {          // МОМЕНТ ПОТЕРИ
                    lossIdx++;
                    for (int q = 0; q < RING_N; q++) {
                        int idx = (ringAt + q) % RING_N;
                        if (ring[idx] == null) continue;
                        dumpTensor(ring[idx], new File(runDir,
                                "потеря" + lossIdx + "_до" + (RING_N - q) + ".png"));
                    }
                    dumpLeft = RING_N;
                } else if (dumpLeft > 0 && !flow) {
                    bin.rewind();
                    byte[] cur = new byte[NET * NET * 3 * 4];
                    bin.get(cur); bin.rewind();
                    dumpTensor(cur, new File(runDir,
                            "потеря" + lossIdx + "_после" + (RING_N - dumpLeft + 1) + ".png"));
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
                   .append(fmt(wSmooth)).append(',').append(fmt(shrink)).append(',')
                   .append(nDet).append(',')
                   .append(Double.isNaN(distToPred) ? "" : fmt(distToPred))
                   .append(',').append(fmt(gateNow))
                   .append(',').append(trk.status == Tracker.TRACKING ? "вед" : "потеря")
                   .append(',').append(trk.missCount).append(',')
                   .append(chosenDet >= 0 ? fmt(detsPx[chosenDet][0]) : "").append(',')
                   .append(chosenDet >= 0 ? fmt(detsPx[chosenDet][1]) : "").append(',')
                   .append(chosenDet >= 0 ? fmt(detsPx[chosenDet][2]) : "").append(',')
                   .append(fmt(trk.filteredSize)).append('\n');
                frames++;

                // Показания для экрана. Отдельные поля, а не чтение frames/hits
                // из другого потока: те живут в стеке петли и снаружи не видны.
                uiTicks = frames; uiHits = hits;
                uiTracking = !flow && trk.status == Tracker.TRACKING;
                uiLink = gotTel;

                // Итог копится здесь же, по тем же величинам, что ушли в лог.
                if (!flow) {
                    if (prevTrkStatus == Tracker.TRACKING && trk.status == Tracker.LOST) { nLoss++; uiLoss = nLoss; }
                    if (prevTrkStatus == Tracker.LOST && trk.status == Tracker.TRACKING
                            && frames > 1) nReacq++;
                    prevTrkStatus = trk.status;
                    if (hit) missStreak = 0;
                    else { missStreak++; if (missStreak > missStreakMax) missStreakMax = missStreak; }
                    scHist.add((double) Sc);
                }
                if (infMs > 0) infHist.add(infMs);
                if (loopMs > 0 && loopMs < 5000) loopHist.add(loopMs);
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
            // ИТОГ — отдельным объектом и с постоянными именами: его читает
            // экран прогонов, и он не должен зависеть от того, какие ключи
            // добавились в диагностику выше.
            j.append(",\"итог\":{")
             .append("\"тактов\":").append(frames)
             .append(",\"доля_на_цели\":").append(frames > 0 ? fmt(hits / (double) frames) : "0")
             .append(",\"потерь\":").append(nLoss)
             .append(",\"повторных_захватов\":").append(nReacq)
             .append(",\"промахов_подряд_макс\":").append(missStreakMax)
             .append(",\"окно_медиана\":").append(fmt(median(scHist)))
             .append(",\"инференс_мс_медиана\":").append(fmt(median(infHist)))
             .append(",\"такт_мс_медиана\":").append(fmt(median(loopHist)))
             // НЕ "секунд": этим именем выше записана ЗАКАЗАННАЯ длительность
             // прогона, и разбор по ключу нашёл бы её вместо фактической.
             .append(",\"длительность_с\":").append(fmt((System.nanoTime() - t0) / 1e9))
             .append(",\"батарея_нагрев\":").append(fmt(batteryTempC()))
             .append("}");
            j.append(",\"ok\":true");
        } catch (Throwable t) {
            Log.e(TAG, "слежение: " + t, t);
            uiError = String.valueOf(t);
            j.append(",\"ok\":false,\"ошибка\":\"")
             .append(String.valueOf(t).replace('"', '\'')).append("\"");
        } finally {
            running = false;
            try { if (interp != null) interp.close(); } catch (Throwable ignored) {}
            try { if (sock != null) sock.close(); } catch (Throwable ignored) {}
            // Рекордер останавливается ДО камеры: иначе кодировщик остаётся
            // без входа и файл выходит без хвоста, а иногда и без индекса.
            try { if (recorder != null) { recorder.stop(); recorder.release(); } }
            catch (Throwable t) { Log.e(TAG, "остановка записи: " + t); }
            try { if (dev != null) dev.close(); } catch (Throwable ignored) {}
            try { if (previewSurface != null) previewSurface.release(); } catch (Throwable ignored) {}
            try { if (previewTexture != null) previewTexture.release(); } catch (Throwable ignored) {}
            try { if (reader != null) reader.close(); } catch (Throwable ignored) {}
            ht.quitSafely();
            try { if (tts != null) { tts.stop(); tts.shutdown(); } } catch (Throwable ignored) {}
            // tone НЕ освобождается здесь: сигнал окончания играет ниже, в
            // этом же блоке. Освобождённый генератор молчит без единой ошибки —
            // ровно тот отказ, который не заметен ни в логе, ни в коде при
            // беглом чтении.
            try { if (wl != null && wl.isHeld()) wl.release(); } catch (Throwable ignored) {}
            // «ГОТОВО» на экране. Прогон кончался звуковым сигналом, но если
            // наблюдатель его не услышал (ветер, шум воды), телефон выглядел
            // ровно так же, как работающий, — отсюда «кстати, горячо и
            // включено» уже после конца.
            uiDone = true;
            // Последняя реплика суфлёра («Один» из отсчёта) висела на экране
            // после конца прогона и читалась как состояние.
            final android.widget.TextView sv2 = statusView;
            if (sv2 != null) runOnUiThread(() -> sv2.setText(""));
            try {
                write(new File(base.getPath() + ".json"), j.append("}").toString());
                write(new File(base.getPath() + ".csv"), csv.toString());
            } catch (Throwable ignored) {}
            // Звук окончания. Наблюдатель стоит в кадре и не видит ни экрана,
            // ни лога: без сигнала он не знает, когда можно расходиться, и
            // либо стоит лишнее, либо уходит раньше времени.
            //
            // Сигнал ОТЛИЧАЕТСЯ от реплик суфлёра: две восходящие ноты вместо
            // речи. Речь можно принять за очередной пункт сценария, а конец
            // прогона должен читаться однозначно.
            try {
                if (tone == null)
                    tone = new android.media.ToneGenerator(
                            android.media.AudioManager.STREAM_MUSIC, 100);
                tone.startTone(android.media.ToneGenerator.TONE_PROP_BEEP, 200);
                Thread.sleep(260);
                tone.startTone(android.media.ToneGenerator.TONE_PROP_BEEP2, 500);
                Thread.sleep(560);
            } catch (Throwable t) { Log.e(TAG, "звук окончания: " + t); }
            try { if (tone != null) tone.release(); } catch (Throwable ignored) {}

            RUNNING_ONE.set(false);
            Log.i(TAG, "ГОТОВО " + base.getPath());
            // ЭКРАН НЕ ЗАКРЫВАЕТСЯ САМ, если прогон запущен человеком.
            //
            // Прежде активность здесь заканчивалась, и телефон возвращался в
            // лаунчер — то есть законченный прогон выглядел ровно как
            // незапущенный. Наблюдатель, вернувшись, не мог отличить «всё
            // прошло» от «не стартовало», и уже был случай, когда телефон
            // остался включённым и горячим, а понять это удалось на ощупь.
            //
            // В режиме auto (запуск скриптом с ноутбука) закрываемся как
            // раньше: там результат забирают файлами, а висящая активность
            // мешает следующему запуску.
            if (getIntent().getBooleanExtra("auto", false)) finish();
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

    /** Тензор NCHW float32 [0..1] -> JPEG. Для записи прогона. */
    static void dumpJpeg(byte[] raw, File f) {
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
                bm.compress(android.graphics.Bitmap.CompressFormat.JPEG, 60, os2);
            }
            bm.recycle();
        } catch (Throwable t) { Log.e(TAG, "запись кадра: " + t); }
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
    /**
     * Значения параметров прогона: интент, потом сохранённое, потом умолчание.
     *
     * Тонкость с интентом: extras типизированы, и «есть ли ключ» ещё не значит
     * «строка». Поэтому значение берётся в строковом виде через общий
     * getExtras().get(key) — иначе --ei seconds 180 из скрипта пришёл бы как
     * Integer, getString вернул бы null, и параметр молча уехал бы в умолчание.
     */
    static final class Cfg {
        private final RunSettings.Source intent, saved;

        Cfg(android.content.Intent i, android.content.SharedPreferences p) {
            final android.os.Bundle ex = (i == null) ? null : i.getExtras();
            intent = new RunSettings.Source() {
                public boolean has(String k) { return ex != null && ex.containsKey(k); }
                public String get(String k) {
                    Object v = (ex == null) ? null : ex.get(k);
                    return v == null ? null : String.valueOf(v);
                }
            };
            saved = new RunSettings.Source() {
                public boolean has(String k) { return p != null && p.contains(k); }
                public String get(String k) { return p == null ? null : p.getString(k, null); }
            };
        }

        String s(String k) { return RunSettings.resolve(intent, saved, k); }
        int i(String k) { return RunSettings.asInt(s(k), 0); }
        float f(String k) { return RunSettings.asFloat(s(k), 0f); }
        boolean b(String k) { return RunSettings.asBool(s(k)); }
    }

    static String fmt(double v) { return String.format(java.util.Locale.US, "%.4f", v); }

    /** Медиана. Пустой список -> 0, чтобы итог не превращался в NaN в json. */
    static double median(List<Double> v) {
        if (v == null || v.isEmpty()) return 0;
        List<Double> c = new ArrayList<>(v);
        java.util.Collections.sort(c);
        return c.get(c.size() / 2);
    }

    /**
     * Температура батареи, °C. Она же — единственная доступная мера нагрева:
     * телефон грелся на каждом длинном прогоне, и это уже влияло на результат
     * (троттлинг инференса), но нигде не записывалось.
     */
    float batteryTempC() {
        try {
            android.content.Intent bi = registerReceiver(null,
                    new android.content.IntentFilter(android.content.Intent.ACTION_BATTERY_CHANGED));
            if (bi == null) return 0;
            return bi.getIntExtra(android.os.BatteryManager.EXTRA_TEMPERATURE, 0) / 10.0f;
        } catch (Throwable t) { return 0; }
    }

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
