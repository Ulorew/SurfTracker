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
        runOnUiThread(() -> {
            android.widget.TextView tv = new android.widget.TextView(this);
            tv.setText("слежение");
            setContentView(tv);
            getWindow().addFlags(
                    android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                    | android.view.WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                    | android.view.WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED);
        });
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

    void run() {
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
        int sign = getIntent().getIntExtra("sign", 1);
        boolean dry = getIntent().getBooleanExtra("dry", false);

        File dir = new File(getExternalFilesDir(null), "track");
        dir.mkdirs();
        File base = new File(dir, tag);
        StringBuilder j = new StringBuilder("{");
        StringBuilder csv = new StringBuilder(
            "i,t_ms,есть_цель,conf,cx_сенсор,ошибка_град,ω_уставка,"
            + "θ_enc,ω_ramp,статус,watchdog,потолок,рампа,энкодер,кламп,срыв,"
            + "инференс_мс,такт_мс\n");

        CameraDevice dev = null;
        BluetoothSocket sock = null;
        HandlerThread ht = new HandlerThread("cam");
        ht.start();
        Handler h = new Handler(ht.getLooper());
        Interpreter interp = null;

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
            // Масштаб считается от ФАКТИЧЕСКОЙ ширины потока, поэтому смена
            // разрешения не меняет ни ошибку в градусах, ни коэффициент K:
            // меньше пикселей — крупнее градус на пиксель, произведение то же.
            final double degPerPx = hfovDeg / (double) W;

            j.append("\"tag\":\"").append(tag).append("\",\"модель\":\"").append(mn)
             .append("\",\"сенсор\":\"").append(W).append("x").append(H)
             .append("\",\"поле_зрения_град\":").append(fmt(hfovDeg))
             .append(",\"град_на_пиксель\":").append(String.format(java.util.Locale.US, "%.5f", degPerPx))
             .append(",\"K\":").append(fmt(K)).append(",\"знак\":").append(sign)
             .append(",\"окно\":").append(side).append(",\"секунд\":").append(seconds)
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
            if (!model.exists()) throw new RuntimeException("нет файла модели " + mn);
            // Число потоков и XNNPACK — ПАРАМЕТРЫ, а не константы. Четыре
            // потока с XNNPACK дали 550 мс на кадр, тогда как прежний рабочий
            // замер на этом же телефоне давал 183 мс на ОДНОМ потоке и без
            // XNNPACK, да ещё под записью 4K. Больше потоков здесь оказалось
            // хуже, и подбирать это надо перебором, а не рассуждением.
            int threads = getIntent().getIntExtra("threads", 1);
            boolean wantXnn = getIntent().getBooleanExtra("xnn", false);
            Interpreter.Options o = new Interpreter.Options();
            o.setNumThreads(threads);
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
            interp = new Interpreter(model, o);
            int[] osh = interp.getOutputTensor(0).shape();
            // Форма СПРАШИВАЕТСЯ, а не берётся константой: у сёрфовой модели
            // 5 строк, у COCO — 84, и захардкоженная пятёрка дала бы не
            // исключение, а тихо неверный разбор.
            float[][][] out = new float[1][osh[1]][osh[2]];
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
            byte[] req = new byte[ProtoV2.REQ_LEN];
            byte[] rx = new byte[4096];
            int rxn = 0;
            long[] sendNs = new long[128];
            int seq = 0, frames = 0, hits = 0, misses = 0;
            // Окно слежения: центр — последняя уверенная детекция. При потере
            // НЕ расширяется: расширение прячет потерю и мешает увидеть, как
            // часто она случается. Для первого прогона важнее честность.
            int winCx = W / 2, winCy = H / 2;
            int Sc = Math.min(side, Math.min(W, H));
            long t0 = System.nanoTime();
            long lastLoop = t0;
            List<Double> lat = new ArrayList<>();
            int stWd = 0, stCap = 0, stRamp = 0, stEnc = 0, stClamp = 0, stSlip = 0, telN = 0;

            while ((System.nanoTime() - t0) / 1e9 < seconds) {
                Image im = latest.getAndSet(null);
                if (im == null) { Thread.sleep(3); continue; }
                long tf = System.nanoTime();

                int cropX = clamp(winCx - Sc / 2, 0, W - Sc);
                int cropY = clamp(winCy - Sc / 2, 0, H - Sc);
                Yuv.crop(im, bin, cropX, cropY, Sc, NET, 0);
                im.close();

                bin.rewind();
                long ti = System.nanoTime();
                interp.run(bin, out);
                double infMs = (System.nanoTime() - ti) / 1e6;

                // Лучшая детекция по строке 4. Для COCO это класс 0 = person;
                // строки 5..83 не читаются вовсе, поэтому фильтр по классу
                // достаётся бесплатно.
                float bestC = 0; float bcx = 0, bcy = 0;
                float[][] o0 = out[0];
                for (int a = 0; a < o0[0].length; a++) {
                    float c = o0[4][a];
                    if (c > bestC) { bestC = c; bcx = o0[0][a]; bcy = o0[1][a]; }
                }
                boolean hit = bestC >= CONF_MIN;
                double errDeg = 0; double w = 0;
                double cxSensor = winCx;
                if (hit) {
                    // Координаты выхода НОРМИРОВАНЫ: умножать на сторону сети,
                    // потом на масштаб кропа. Забыть об этом — значит собрать
                    // все рамки в левом верхнем углу.
                    cxSensor = cropX + (bcx * NET) * (Sc / (double) NET);
                    double cySensor = cropY + (bcy * NET) * (Sc / (double) NET);
                    errDeg = (cxSensor - W / 2.0) * degPerPx;
                    w = sign * K * Math.toRadians(errDeg);
                    winCx = (int) cxSensor; winCy = (int) cySensor;
                    hits++;
                } else {
                    // Цель потеряна — уставка НОЛЬ, а не последняя команда.
                    // Продолжать крутить вслепую значит уезжать от цели тем
                    // дальше, чем дольше её нет.
                    w = 0;
                    misses++;
                }

                float wf = (float) w;
                int st = 0; float th = 0, wr = 0; boolean gotTel = false;
                if (!dry) {
                    int sq = seq & 0x7F;
                    ProtoV2.buildReq(req, sq, wf, 0.0f);   // ω̇ = 0, см. шапку
                    sendNs[sq] = System.nanoTime();
                    os.write(req); os.flush();
                    seq++;
                    int av = is.available();
                    if (av > 0) {
                        int g = is.read(rx, rxn, Math.min(av, rx.length - rxn));
                        if (g > 0) rxn += g;
                        int p = 0;
                        while (rxn - p >= ProtoV2.TEL_LEN) {
                            ProtoV2.Tel t = ProtoV2.parseTel(rx, p);
                            if (t == null) { p++; continue; }
                            long snt = sendNs[t.seq];
                            if (snt != 0) {
                                lat.add((System.nanoTime() - snt) / 1e6);
                                sendNs[t.seq] = 0;
                            }
                            st = t.status; th = t.theta; wr = t.wRamp; gotTel = true;
                            telN++;
                            if ((st & ProtoV2.ST_WATCHDOG) != 0) stWd++;
                            if ((st & ProtoV2.ST_EXTRAP_CAP) != 0) stCap++;
                            if ((st & ProtoV2.ST_RAMP_SAT) != 0) stRamp++;
                            if ((st & ProtoV2.ST_ENC_OK) != 0) stEnc++;
                            if ((st & ProtoV2.ST_CLAMP) != 0) stClamp++;
                            if ((st & ProtoV2.ST_SLIP) != 0) stSlip++;
                            p += ProtoV2.TEL_LEN;
                        }
                        if (p > 0) { System.arraycopy(rx, p, rx, 0, rxn - p); rxn -= p; }
                    }
                }

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
                   .append(fmt(infMs)).append(',').append(fmt(loopMs)).append('\n');
                frames++;
            }

            // Остановить вал ЯВНО. Полагаться на сторож нельзя: он сработает,
            // но через 300 мс и с поднятым битом, то есть штатный выход
            // выглядел бы как отказ связи.
            if (!dry) for (int i = 0; i < 5; i++) {
                ProtoV2.buildReq(req, (seq + i) & 0x7F, 0.0f, 0.0f);
                os.write(req); os.flush();
                Thread.sleep(60);
            }

            java.util.Collections.sort(lat);
            j.append(",\"кадров\":").append(frames).append(",\"с_целью\":").append(hits)
             .append(",\"без_цели\":").append(misses)
             .append(",\"доля_с_целью\":").append(frames > 0 ? fmt(hits / (double) frames) : "0")
             .append(",\"телеметрии\":").append(telN)
             .append(",\"биты\":{\"watchdog\":").append(stWd)
             .append(",\"потолок\":").append(stCap).append(",\"рампа\":").append(stRamp)
             .append(",\"энкодер\":").append(stEnc).append(",\"кламп\":").append(stClamp)
             .append(",\"срыв\":").append(stSlip).append("}");
            if (!lat.isEmpty())
                j.append(",\"rtt_ms\":{\"p50\":").append(fmt(lat.get(lat.size() / 2)))
                 .append(",\"p95\":").append(fmt(lat.get((int) (0.95 * (lat.size() - 1)))))
                 .append("}");
            j.append(",\"ok\":true");
        } catch (Throwable t) {
            Log.e(TAG, "слежение: " + t, t);
            j.append(",\"ok\":false,\"ошибка\":\"")
             .append(String.valueOf(t).replace('"', '\'')).append("\"");
        } finally {
            try { if (interp != null) interp.close(); } catch (Throwable ignored) {}
            try { if (sock != null) sock.close(); } catch (Throwable ignored) {}
            try { if (dev != null) dev.close(); } catch (Throwable ignored) {}
            try { if (reader != null) reader.close(); } catch (Throwable ignored) {}
            ht.quitSafely();
            try { if (wl != null && wl.isHeld()) wl.release(); } catch (Throwable ignored) {}
            try {
                write(new File(base.getPath() + ".json"), j.append("}").toString());
                write(new File(base.getPath() + ".csv"), csv.toString());
            } catch (Throwable ignored) {}
            Log.i(TAG, "ГОТОВО " + base.getPath());
            finish();
        }
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
