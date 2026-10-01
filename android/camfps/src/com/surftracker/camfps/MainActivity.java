package com.surftracker.camfps;

import android.app.Activity;
import android.graphics.Bitmap;
import android.graphics.ImageFormat;
import android.graphics.SurfaceTexture;
import android.hardware.camera2.*;
import android.hardware.camera2.params.OutputConfiguration;
import android.hardware.camera2.params.SessionConfiguration;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.media.MediaRecorder;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.util.Size;
import android.view.Surface;

import java.io.File;
import java.io.FileWriter;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import org.tensorflow.lite.Interpreter;

/**
 * Практический прогон комбинации потоков.
 *
 * isSessionConfigurationSupported говорит "поддержано" — это ЗАЯВЛЕНИЕ. Здесь
 * меряется фактическое: сколько кадров в секунду реально отдаёт каждый поток
 * при ИДУЩЕЙ записи, и не икает ли сама запись.
 *
 * Запись настоящая (MediaRecorder, H.264 4K), а не SurfaceTexture-заглушка:
 * весь смысл в нагрузке от кодировщика, без неё замер ничего не значит.
 */
public class MainActivity extends Activity {
    static final String TAG = "CamFps";
    HandlerThread bg; Handler h;
    final AtomicInteger yuvFrames = new AtomicInteger();
    final AtomicInteger results = new AtomicInteger();
    final AtomicInteger jpegFrames = new AtomicInteger();
    final AtomicLong firstNs = new AtomicLong(), lastNs = new AtomicLong();
    final List<Long> resultTs = java.util.Collections.synchronizedList(new ArrayList<>());
    /**
     * СИЛЬНАЯ ссылка на SurfaceTexture превью — обязательна.
     *
     * Была локальной переменной, и сборщик мусора собирал её прямо посреди
     * сессии: последнее использование заканчивалось на targets.add(), дальше
     * объект недостижим, финализатор бросает BufferQueue, камера получает
     * "BufferQueue has been abandoned" (-19) и встаёт ВЕСЬ конвейер — включая
     * запись и YUV. В логе это выглядело как обрыв записи на девятой секунде,
     * и первые три версии объяснения (YUV не тянет, экран гаснет, параметры
     * записи) были поэтому неверны. Surface сам по себе SurfaceTexture не
     * держит.
     */
    SurfaceTexture previewTexture;
    /** Кроп 640x640 в NCHW float32 RGB — ровно то, что ест модель. Заполняется
     *  из YUV-кадра; null, пока инференс не включён. */
    volatile java.nio.ByteBuffer cropOut;
    final AtomicInteger cropDone = new AtomicInteger();
    final java.util.List<Double> cropMs = java.util.Collections.synchronizedList(new ArrayList<>());
    /** Сквозной такт: приход кадра с камеры -> конец инференса. Именно эта
     *  величина сравнивается с бюджетом 333 мс, а не время интерпретатора:
     *  конвертация YUV->RGB тоже входит в такт. */
    final java.util.List<Double> tickMs = java.util.Collections.synchronizedList(new ArrayList<>());
    final AtomicLong cropArrivedNs = new AtomicLong();
    /** Поминутные корзины: [минута][список] для инференса и для такта. */
    final java.util.Map<Integer, java.util.List<Double>> byMinute =
            java.util.Collections.synchronizedMap(new java.util.LinkedHashMap<>());
    final java.util.Map<Integer, java.util.List<Double>> tickByMinute =
            java.util.Collections.synchronizedMap(new java.util.LinkedHashMap<>());
    final java.util.Map<Integer, java.util.List<Double>> cropByMinute =
            java.util.Collections.synchronizedMap(new java.util.LinkedHashMap<>());
    final java.util.Map<Integer, Integer> yuvByMinute =
            java.util.Collections.synchronizedMap(new java.util.LinkedHashMap<>());
    final java.util.Map<Integer, String> envByMinute =
            java.util.Collections.synchronizedMap(new java.util.LinkedHashMap<>());
    long runStartMs;
    android.os.PowerManager.WakeLock wake;
    /** Сегментация записи: файл никогда не растёт больше лимита. Переключение
     *  через setNextOutputFile — оно бесшовное, в отличие от stop/start, где
     *  сессия камеры пересобирается и дырка измеряется сотнями миллисекунд. */
    File videoDir;
    int segIndex = 0;
    final java.util.List<String> segEvents = java.util.Collections.synchronizedList(new ArrayList<>());
    final java.util.List<Long> segSwitchNs = java.util.Collections.synchronizedList(new ArrayList<>());
    /** Контроль честности (урок дефекта 4): раз в минуту сохраняем ПАРУ
     *  вход-выход — сам кроп картинкой и детекции по нему. Без этого нельзя
     *  отличить работающий конвейер от гоняющего нулевой буфер. */
    volatile int proofMinute = -1;
    /** 0 = полный путь, 1 = только чтение плоскостей, 2 = чтение+конвертация. */
    volatile int cropStage = 0;
    /** Время ожидания и получения Image из ImageReader — отдельная стадия. */
    final java.util.List<Double> acquireMs = java.util.Collections.synchronizedList(new ArrayList<>());
    final java.util.List<String> proofs = java.util.Collections.synchronizedList(new ArrayList<>());
    /** Левый верхний угол кропа в координатах ПОЛНОГО YUV-кадра. Фиксированный:
     *  трекера в этом прогоне нет, а место кропа на время инференса не влияет —
     *  влияет только объём конвертации, и он от места не зависит. */
    int cropX, cropY;
    /** Сторона ЗАПРОШЕННОГО окна в пикселях сенсора. Тензор всегда 640:
     *  окно крупнее честно уменьшается (см. Yuv.crop). Пока петли на телефоне
     *  нет, сторона приходит параметром запуска; когда появится — её будет
     *  задавать петля, как и в офлайне. */
    int cropSide = 640;

    /**
     * Конвертация YUV_420_888 -> RGB float32 NCHW ТОЛЬКО для окна 640x640.
     *
     * Полный кадр 4080x3060 — это 12.5 млн пикселей; конвертировать его целиком
     * ради 0.41 млн нужных значило бы отдать десятки миллисекунд ни за что.
     * Читаются только строки cropY..cropY+639 и в них только нужные столбцы,
     * из плоскостей Y (полное разрешение) и U/V (половинное, отсюда деление
     * координат на два).
     */
    /**
     * Профилирование пути кропа по стадиям.
     *
     * Вложенные таймеры вокруг участков в наносекунды исказили бы сами
     * участки, поэтому стадии разделяются ВАРИАНТАМИ: A — только чтение
     * плоскостей, B — чтение и конвертация, C — полный путь с упаковкой.
     * Разности дают вклад стадий, а C обязан сойтись с суммой (проверяется
     * в отчёте).
     *
     * Итог чтения в варианте A складывается в sink и печатается: без этого
     * JIT выбросит цикл целиком, и "чтение" окажется бесплатным.
     */
    volatile long sink;

    void cropFromYuv(Image im) {
        long t0 = System.nanoTime();
        cropArrivedNs.set(t0);
        // Сам перевод — в Yuv.convert, ОДНОЙ реализацией со стендом сверки
        // камерного зрения: скопированный "такой же" цикл разъехался
        // бы на первой правке, и доказательство зрения перестало бы
        // относиться к боевому пути.
        sink += Yuv.crop(im, cropOut, cropX, cropY, cropSide, 640, cropStage);
        cropMs.add((System.nanoTime() - t0) / 1e6);
        cropDone.incrementAndGet();
    }

    /** По той же причине, что и previewTexture: локальные ImageReader-ы
     *  становились недостижимы сразу после сборки запроса, финализатор бросал
     *  их BufferQueue, и захват вставал ровно через 9 секунд. Держать обязаны
     *  ВСЕ участники сессии, а не только те, к которым обращаемся позже. */
    ImageReader yuvReader, jpegReader;
    MediaRecorder recorder;
    Surface previewSurface;

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        bg = new HandlerThread("cam"); bg.start(); h = new Handler(bg.getLooper());
        if (checkSelfPermission(android.Manifest.permission.CAMERA)
                != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            Log.i(TAG, "ЖДУ РАЗРЕШЕНИЯ CAMERA");
            requestPermissions(new String[]{android.Manifest.permission.CAMERA,
                    android.Manifest.permission.RECORD_AUDIO}, 1);
        } else new Thread(this::run).start();
    }

    @Override public void onRequestPermissionsResult(int c, String[] p, int[] g) {
        new Thread(this::run).start();
    }

    void run() {
        // Без удержания экрана MIUI гасит дисплей и придерживает камеру у
        // фонового приложения: контрольные прогоны отдавали ~9 с из 30 ВО ВСЕХ
        // конфигурациях, включая вариант без YUV, — то есть дело было не в
        // потоке, а в этом. Флаг ставится из UI-потока.
        // Экран обязан остаться включённым: MIUI отбирает камеру у фонового
        // приложения (в логе Camera2ClientBase "start to disconnect" +
        // MIUISafety-Monitor op:26 active:false), и прогон обрывается на
        // первой же минуте. Настройка "не выключать экран при отладке"
        // действует ТОЛЬКО при зарядке, поэтому от батареи нужна своя защита.
        // Три независимых способа: реальный content view (флаг окна без него
        // ненадёжен), сам флаг и wake lock.
        runOnUiThread(() -> {
            android.widget.TextView tv = new android.widget.TextView(this);
            tv.setText("прогон идёт");
            setContentView(tv);
            getWindow().addFlags(
                    android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                    | android.view.WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                    | android.view.WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED);
        });
        try {
            android.os.PowerManager pm = getSystemService(android.os.PowerManager.class);
            wake = pm.newWakeLock(android.os.PowerManager.SCREEN_BRIGHT_WAKE_LOCK
                    | android.os.PowerManager.ACQUIRE_CAUSES_WAKEUP, "camfps:run");
            wake.acquire(40 * 60 * 1000L);
        } catch (Throwable t) {
            Log.e(TAG, "wake lock: " + t);
        }
        File dir = getExternalFilesDir(null);
        String combo = getIntent().getStringExtra("combo");
        if (combo == null) combo = "priv1080+rec4k+yuvmax";
        int seconds = getIntent().getIntExtra("seconds", 60);
        cropStage = getIntent().getIntExtra("crop_stage", 0);
        int jpegPerSec = getIntent().getIntExtra("jpeg_per_sec", 0);
        StringBuilder j = new StringBuilder();
        CameraDevice dev = null;
        MediaRecorder rec = null;
        try {
            CameraManager cm = getSystemService(CameraManager.class);
            String id = "0";
            StreamConfigurationMap map = cm.getCameraCharacteristics(id)
                    .get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            Size yuvMax = biggest(map.getOutputSizes(ImageFormat.YUV_420_888));
            cropSide = getIntent().getIntExtra("crop_side", 640);
            cropSide = Math.max(640, Math.min(cropSide,
                        Math.min(yuvMax.getWidth(), yuvMax.getHeight())));
            cropX = (yuvMax.getWidth() - cropSide) / 2;
            cropY = (yuvMax.getHeight() - cropSide) / 2;
            Size jpegMax = biggest(map.getOutputSizes(ImageFormat.JPEG));

            videoDir = new File(dir, "rec_" + combo);
            if (videoDir.isDirectory()) for (File f : videoDir.listFiles()) f.delete();
            videoDir.mkdirs();
            File video = new File(videoDir, "seg_0.mp4");

            List<Surface> targets = new ArrayList<>();
            previewTexture = new SurfaceTexture(0);
            previewTexture.setDefaultBufferSize(1920, 1080);
            Surface preview = previewSurface = new Surface(previewTexture);
            targets.add(preview);

            rec = recorder = new MediaRecorder();
            rec.setVideoSource(MediaRecorder.VideoSource.SURFACE);
            // Параметры записи — из ПРОФИЛЯ УСТРОЙСТВА, а не назначенные руками.
            // Руками заданные 3840x2160 @ 40 Мбит/с давали ошибку дорожки
            // (-1011) на девятой секунде и вешали весь конвейер: производитель
            // знает про свой кодировщик больше, чем мы.
            android.media.CamcorderProfile prof =
                    android.media.CamcorderProfile.hasProfile(0, android.media.CamcorderProfile.QUALITY_2160P)
                    ? android.media.CamcorderProfile.get(0, android.media.CamcorderProfile.QUALITY_2160P)
                    : android.media.CamcorderProfile.get(0, android.media.CamcorderProfile.QUALITY_HIGH);
            Log.i(TAG, "профиль записи " + prof.videoFrameWidth + "x" + prof.videoFrameHeight
                    + " @" + prof.videoFrameRate + " fps, " + prof.videoBitRate + " бит/с, кодек " + prof.videoCodec);
            rec.setOutputFormat(prof.fileFormat);
            rec.setOutputFile(video.getAbsolutePath());
            rec.setVideoEncoder(prof.videoCodec);
            rec.setVideoSize(prof.videoFrameWidth, prof.videoFrameHeight);
            rec.setVideoFrameRate(prof.videoFrameRate);
            rec.setVideoEncodingBitRate(prof.videoBitRate);
            long segBytes = getIntent().getLongExtra("segment_bytes", 0L);
            if (segBytes > 0) rec.setMaxFileSize(segBytes);
            // Без этих слушателей отказ записи выглядит как "камера отдала мало
            // кадров": когда MediaRecorder перестаёт разбирать буферы, встаёт
            // ВЕСЬ конвейер, включая другие потоки. Именно так и вышло: во всех
            // конфигурациях приходило ~9 с из 30-60, и виноват был не YUV.
            rec.setOnInfoListener((mr, what, extra) -> {
                Log.w(TAG, "MediaRecorder info what=" + what + " extra=" + extra);
                try {
                    if (what == MediaRecorder.MEDIA_RECORDER_INFO_MAX_FILESIZE_APPROACHING) {
                        // готовим СЛЕДУЮЩИЙ файл заранее: рекордер переключится
                        // на него сам, не останавливаясь
                        File nf = new File(videoDir, "seg_" + (++segIndex) + ".mp4");
                        mr.setNextOutputFile(nf);
                        segEvents.add(String.format(java.util.Locale.US,
                                "{\"t_s\": %.1f, \"событие\": \"подготовлен\", \"файл\": \"%s\"}",
                                (System.currentTimeMillis() - runStartMs) / 1000.0, nf.getName()));
                    } else if (what == MediaRecorder.MEDIA_RECORDER_INFO_NEXT_OUTPUT_FILE_STARTED) {
                        segSwitchNs.add(System.nanoTime());
                        segEvents.add(String.format(java.util.Locale.US,
                                "{\"t_s\": %.1f, \"событие\": \"переключение\"}",
                                (System.currentTimeMillis() - runStartMs) / 1000.0));
                    }
                } catch (Throwable t) {
                    Log.e(TAG, "переключение сегмента: " + t);
                    segEvents.add("{\"ошибка\": \"" + t + "\"}");
                }
            });
            rec.setOnErrorListener((mr, what, extra) ->
                    Log.e(TAG, "MediaRecorder ОШИБКА what=" + what + " extra=" + extra));
            rec.prepare();
            Surface recSurf = rec.getSurface();
            targets.add(recSurf);

            // Контроль: тот же прогон без YUV или с YUV поменьше. Без него
            // нельзя отличить "устройство не тянет комбинацию" от "наш код
            // затыкает конвейер", а это разные выводы.
            String yuvMode = getIntent().getStringExtra("yuv");
            if (yuvMode == null) yuvMode = "max";
            Size yuvSize = "1080".equals(yuvMode) ? new Size(1920, 1080) : yuvMax;
            ImageReader yuv = yuvReader = ImageReader.newInstance(
                    yuvSize.getWidth(), yuvSize.getHeight(), ImageFormat.YUV_420_888, 3);
            yuv.setOnImageAvailableListener(r -> {
                long ta = System.nanoTime();
                try (Image im = r.acquireLatestImage()) {
                    acquireMs.add((System.nanoTime() - ta) / 1e6);
                    if (im != null) {
                        long t = System.nanoTime();
                        firstNs.compareAndSet(0, t); lastNs.set(t);
                        yuvFrames.incrementAndGet();
                        if (cropOut != null) cropFromYuv(im);
                    }
                }
            }, h);
            // yuv_hz > 0: поток анализа НЕ входит в повторяющийся запрос, а
            // запрашивается отдельными capture() с нужной частотой. Смысл: в
            // повторяющемся запросе сенсор и ISP гонят полноразмерный YUV
            // каждый такт, и запись теряет треть кадров (29.8 -> 20.7 fps,
            // проверено по самим файлам). Нам же нужно 3-5 кадров в секунду.
            int yuvHz = getIntent().getIntExtra("yuv_hz", 0);
            if (!"none".equals(yuvMode)) targets.add(yuv.getSurface());

            ImageReader jpeg = null;
            if (jpegPerSec > 0) {
                jpeg = jpegReader = ImageReader.newInstance(jpegMax.getWidth(),
                        jpegMax.getHeight(), ImageFormat.JPEG, 3);
                jpeg.setOnImageAvailableListener(r -> {
                    try (Image im = r.acquireLatestImage()) {
                        if (im != null) jpegFrames.incrementAndGet();
                    }
                }, h);
                targets.add(jpeg.getSurface());
            }

            dev = open(cm, id);
            List<OutputConfiguration> cfgs = new ArrayList<>();
            for (Surface s : targets) cfgs.add(new OutputConfiguration(s));
            CountDownLatch ready = new CountDownLatch(1);
            final CameraCaptureSession[] box = new CameraCaptureSession[1];
            SessionConfiguration sc = new SessionConfiguration(SessionConfiguration.SESSION_REGULAR,
                    cfgs, r -> h.post(r), new CameraCaptureSession.StateCallback() {
                public void onConfigured(CameraCaptureSession s) { box[0] = s; ready.countDown(); }
                public void onConfigureFailed(CameraCaptureSession s) { ready.countDown(); }
            });
            dev.createCaptureSession(sc);
            if (!ready.await(10, TimeUnit.SECONDS) || box[0] == null)
                throw new RuntimeException("сессия не сконфигурировалась");

            CaptureRequest.Builder rq = dev.createCaptureRequest(CameraDevice.TEMPLATE_RECORD);
            rq.addTarget(preview); rq.addTarget(recSurf);
            if (!"none".equals(yuvMode) && yuvHz <= 0) rq.addTarget(yuv.getSurface());
            box[0].setRepeatingRequest(rq.build(), new CameraCaptureSession.CaptureCallback() {
                public void onCaptureCompleted(CameraCaptureSession s, CaptureRequest r, TotalCaptureResult res) {
                    results.incrementAndGet();
                    Long ts = res.get(CaptureResult.SENSOR_TIMESTAMP);
                    if (ts != null) resultTs.add(ts);
                }
            }, h);
            rec.start();

            // Худший случай целиком: инференс идёт ОДНОВРЕМЕННО с записью 4K и
            // выдачей YUV. Мерить их порознь бессмысленно — на телефоне они
            // делят и кристалл, и тепловой бюджет.
            Thread infer = null;
            final java.util.List<Double> lat = java.util.Collections.synchronizedList(new ArrayList<>());
            final boolean[] stop = {false};
            if (getIntent().getBooleanExtra("infer", false)) {
                infer = new Thread(() -> {
                    try {
                        File model = new File(getExternalFilesDir(null), "surf_w8a32.tflite");
                        Interpreter.Options o = new Interpreter.Options();
                        o.setNumThreads(1);
                        Interpreter it = new Interpreter(model, o);
                        java.nio.ByteBuffer bin = java.nio.ByteBuffer
                                .allocateDirect(4 * 3 * 640 * 640).order(java.nio.ByteOrder.nativeOrder());
                        cropOut = bin;   // с этого момента слушатель YUV пишет сюда кроп
                        float[][][] o2 = new float[1][5][8400];
                        int seen = -1;
                        while (!stop[0]) {
                            // ждём НОВЫЙ кроп: инференс идёт по кадрам камеры, а
                            // не крутится вхолостую на одном и том же буфере
                            int n = cropDone.get();
                            if (n == seen) { Thread.sleep(2); continue; }
                            seen = n;
                            bin.rewind();
                            long arrived = cropArrivedNs.get();
                            long t0 = System.nanoTime();
                            it.run(bin, o2);
                            long t1 = System.nanoTime();
                            double infMs = (t1 - t0) / 1e6;
                            double tickFull = (t1 - arrived) / 1e6;
                            lat.add(infMs);
                            tickMs.add(tickFull);
                            int min = (int) ((System.currentTimeMillis() - runStartMs) / 60000);
                            if (min != proofMinute) {
                                proofMinute = min;
                                saveProof(min, bin, o2);
                                envByMinute.put(min, env());
                            }
                            byMinute.computeIfAbsent(min, k ->
                                    java.util.Collections.synchronizedList(new ArrayList<>())).add(infMs);
                            cropByMinute.computeIfAbsent(min, k ->
                                    java.util.Collections.synchronizedList(new ArrayList<>()))
                                    .add(cropMs.isEmpty() ? 0.0 : cropMs.get(cropMs.size() - 1));
                            yuvByMinute.merge(min, 1, Integer::sum);
                            tickByMinute.computeIfAbsent(min, k ->
                                    java.util.Collections.synchronizedList(new ArrayList<>())).add(tickFull);
                        }
                        it.close();
                    } catch (Throwable t) { Log.e(TAG, "инференс: " + t); }
                });
                infer.start();
            }

            runStartMs = System.currentTimeMillis();
            long end = System.currentTimeMillis() + seconds * 1000L;
            long nextJpeg = System.currentTimeMillis();
            long nextYuv = System.currentTimeMillis();
            while (System.currentTimeMillis() < end) {
                if (yuvHz > 0 && !"none".equals(yuvMode) && System.currentTimeMillis() >= nextYuv) {
                    nextYuv += 1000 / yuvHz;
                    CaptureRequest.Builder an = dev.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
                    an.addTarget(yuv.getSurface());
                    box[0].capture(an.build(), null, h);
                }
                if (jpeg != null && System.currentTimeMillis() >= nextJpeg) {
                    nextJpeg += 1000 / Math.max(jpegPerSec, 1);
                    CaptureRequest.Builder still = dev.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE);
                    still.addTarget(jpeg.getSurface());
                    box[0].capture(still.build(), null, h);
                }
                Thread.sleep(20);
            }
            stop[0] = true;
            if (infer != null) infer.join(3000);
            box[0].stopRepeating();
            Thread.sleep(300);
            rec.stop();

            double secs = (lastNs.get() - firstNs.get()) / 1e9;
            long[] ts = new long[resultTs.size()];
            for (int i = 0; i < ts.length; i++) ts[i] = resultTs.get(i);
            java.util.Arrays.sort(ts);
            long maxGap = 0;
            for (int i = 1; i < ts.length; i++) maxGap = Math.max(maxGap, ts[i] - ts[i - 1]);
            double medGap = ts.length > 2 ? (ts[ts.length / 2] - ts[ts.length / 2 - 1]) / 1e6 : 0;

            j.append(String.format(java.util.Locale.US,
                    "{\"combo\": \"%s\", \"seconds\": %d, \"yuv_size\": \"%s\", "
                    + "\"yuv_frames\": %d, \"yuv_fps\": %.2f, \"capture_results\": %d, "
                    + "\"result_fps\": %.2f, \"max_gap_ms\": %.1f, \"median_gap_ms\": %.1f, "
                    + "\"jpeg_frames\": %d, \"video_bytes\": %d, \"yuv_hz_requested\": %d}\n",
                    combo, seconds, "none".equals(yuvMode) ? "нет" : yuvSize.toString(), yuvFrames.get(),
                    yuvFrames.get() / Math.max(secs, 1e-9), results.get(),
                    results.get() / (double) seconds, maxGap / 1e6, medGap,
                    jpegFrames.get(), video.length(), yuvHz));
            if (!lat.isEmpty()) {
                double[] a = new double[lat.size()];
                for (int i = 0; i < a.length; i++) a[i] = lat.get(i);
                java.util.Arrays.sort(a);
                android.os.PowerManager pmg = getSystemService(android.os.PowerManager.class);
                float hr = Float.NaN;
                try { hr = pmg.getThermalHeadroom(60); } catch (Throwable ignored) { }
                double[] cms = new double[cropMs.size()];
                for (int i = 0; i < cms.length; i++) cms[i] = cropMs.get(i);
                java.util.Arrays.sort(cms);
                Log.i(TAG, String.format(java.util.Locale.US,
                        "ИНФЕРЕНС ПОД ЗАПИСЬЮ: прогонов %d p50 %.1f p95 %.1f | кроп+YUV->RGB "
                        + "p50 %.1f p95 %.1f (кропов %d, из %dx%d в (%d,%d)) | headroom %s",
                        a.length, a[a.length / 2], a[(int) (0.95 * (a.length - 1))],
                        cms.length > 0 ? cms[cms.length / 2] : -1,
                        cms.length > 0 ? cms[(int) (0.95 * (cms.length - 1))] : -1,
                        cms.length, yuvSize.getWidth(), yuvSize.getHeight(), cropX, cropY,
                        Float.isNaN(hr) ? "нет" : String.format(java.util.Locale.US, "%.3f", hr)));
                StringBuilder pm = new StringBuilder("{\n  \"crop\": {\"from\": \""
                        + yuvSize.getWidth() + "x" + yuvSize.getHeight() + "\", \"at\": [" + cropX
                        + ", " + cropY + "], \"size\": " + cropSide + ", \"tensor\": 640},\n  \"по_минутам\": [\n");
                boolean f1 = true;
                for (Integer m : new java.util.TreeSet<>(byMinute.keySet())) {
                    double[] mi = arr(byMinute.get(m)), mt = arr(tickByMinute.get(m));
                    if (!f1) pm.append(",\n");
                    f1 = false;
                    double[] mc = arr(cropByMinute.getOrDefault(m, new ArrayList<>()));
                    pm.append(String.format(java.util.Locale.US,
                            "    {\"минута\": %d, \"кадров\": %d, \"инференс_p50\": %.1f, "
                            + "\"инференс_p95\": %.1f, \"такт_p50\": %.1f, \"такт_p95\": %.1f, "
                            + "\"кроп_p50\": %.1f, \"кроп_p95\": %.1f, \"yuv_кадров\": %d, %s}",
                            m + 1, mi.length, pct(mi, 50), pct(mi, 95), pct(mt, 50), pct(mt, 95),
                            pct(mc, 50), pct(mc, 95), yuvByMinute.getOrDefault(m, 0),
                            envByMinute.getOrDefault(m, "\"температура\": null")));
                }
                double[] ta = arr(tickMs);
                pm.append(String.format(java.util.Locale.US,
                        "\n  ],\n  \"стадия_кропа\": " + cropStage + ", \"acquire_p50\": "
                        + String.format(java.util.Locale.US, "%.2f", pct(arr(acquireMs), 50))
                        + ", \"acquire_p95\": "
                        + String.format(java.util.Locale.US, "%.2f", pct(arr(acquireMs), 95))
                        + ",\n  \"итого\": {\"кадров\": %d, \"инференс_p50\": %.1f, "
                        + "\"инференс_p95\": %.1f, \"такт_p50\": %.1f, \"такт_p95\": %.1f, "
                        + "\"кроп_p50\": %.1f, \"кроп_p95\": %.1f, \"headroom\": %s}\n}\n",
                        ta.length, pct(a, 50), pct(a, 95), pct(ta, 50), pct(ta, 95),
                        pct(cms, 50), pct(cms, 95),
                        Float.isNaN(hr) ? "null" : String.format(java.util.Locale.US, "%.3f", hr)));
                pm.setLength(pm.length() - 2);   // убираем закрывающую скобку объекта
                pm.append(",\n  \"сегменты\": [\n    ").append(String.join(",\n    ", segEvents))
                  .append("\n  ],\n  \"честность\": [\n    ").append(String.join(",\n    ", proofs))
                  .append("\n  ]\n}\n");
                try (FileWriter w2 = new FileWriter(new File(dir, "infer_" + combo + ".json"))) {
                    w2.write(pm.toString());
                }
            }
            Log.i(TAG, "ГОТОВО " + j);
        } catch (Throwable t) {
            Log.e(TAG, "ОШИБКА " + t, t);
            j.append("{\"error\": \"").append(String.valueOf(t).replace("\"", "'")).append("\"}\n");
        } finally {
            try { if (rec != null) rec.release(); } catch (Throwable ignored) { }
            if (dev != null) dev.close();
        }
        try (FileWriter w = new FileWriter(new File(dir, "camfps_" + combo + ".json"))) {
            w.write(j.toString());
        } catch (Exception ignored) { }
        try { if (wake != null && wake.isHeld()) wake.release(); } catch (Throwable ignored) { }
        finish();
    }

    /**
     * Контроль честности замера (урок дефекта 4: в первом худшем случае поток
     * инференса гонял НУЛЕВОЙ буфер и ни разу не читал камеру, а числа
     * выглядели правдоподобно).
     *
     * Раз в минуту сохраняем ПАРУ: сам вход картинкой (из того же буфера,
     * который ушёл в сеть, а не из нового кадра) и детекции по нему. Если
     * вход окажется нулевым — это будет видно чёрным квадратом, а не
     * останется догадкой.
     */
    void saveProof(int minute, java.nio.ByteBuffer in, float[][][] out) {
        try {
            final int S = 640, PLANE = S * S;
            int[] px = new int[PLANE];
            double sum = 0;
            for (int i = 0; i < PLANE; i++) {
                int r = (int) (in.getFloat(i * 4) * 255);
                int g = (int) (in.getFloat((PLANE + i) * 4) * 255);
                int b = (int) (in.getFloat((2 * PLANE + i) * 4) * 255);
                sum += r + g + b;
                px[i] = 0xFF000000 | (clamp(r) << 16) | (clamp(g) << 8) | clamp(b);
            }
            Bitmap bmp = Bitmap.createBitmap(px, S, S, Bitmap.Config.ARGB_8888);
            File f = new File(getExternalFilesDir(null), "proof_min" + (minute + 1) + ".png");
            try (java.io.FileOutputStream os = new java.io.FileOutputStream(f)) {
                bmp.compress(Bitmap.CompressFormat.PNG, 100, os);
            }
            StringBuilder d = new StringBuilder("[");
            int n = 0;
            for (int a = 0; a < 8400 && n < 8; a++) {
                float conf = out[0][4][a];
                if (conf < 0.25f) continue;
                if (n++ > 0) d.append(", ");
                d.append(String.format(java.util.Locale.US, "[%.1f, %.1f, %.1f, %.1f, %.3f]",
                        out[0][0][a] * S, out[0][1][a] * S, out[0][2][a] * S, out[0][3][a] * S, conf));
            }
            int total = 0;
            for (int a = 0; a < 8400; a++) if (out[0][4][a] >= 0.25f) total++;
            d.append("]");
            proofs.add(String.format(java.util.Locale.US,
                    "{\"минута\": %d, \"среднее_значение_входа\": %.1f, \"детекций_conf025\": %d, "
                    + "\"первые\": %s, \"файл\": \"%s\"}",
                    minute + 1, sum / (3.0 * PLANE), total, d, f.getName()));
        } catch (Throwable t) {
            proofs.add("{\"минута\": " + (minute + 1) + ", \"ошибка\": \"" + t + "\"}");
        }
    }

    static int clamp(int v) { return v < 0 ? 0 : v > 255 ? 255 : v; }

    /** Температура батареи, заряд и headroom — снимаются изнутри приложения,
     *  чтобы попасть в тот же поминутный лог, что и скорость. */
    String env() {
        float temp = Float.NaN, hr = Float.NaN;
        int lvl = -1, status = -1;
        try {
            android.content.Intent bi = registerReceiver(null,
                    new android.content.IntentFilter(android.content.Intent.ACTION_BATTERY_CHANGED));
            if (bi != null) {
                temp = bi.getIntExtra(android.os.BatteryManager.EXTRA_TEMPERATURE, -1) / 10.0f;
                lvl = bi.getIntExtra(android.os.BatteryManager.EXTRA_LEVEL, -1);
                status = bi.getIntExtra(android.os.BatteryManager.EXTRA_STATUS, -1);
            }
            hr = getSystemService(android.os.PowerManager.class).getThermalHeadroom(60);
        } catch (Throwable ignored) { }
        return String.format(java.util.Locale.US,
                "\"температура\": %.1f, \"заряд\": %d, \"питание\": %d, \"headroom\": %s",
                temp, lvl, status, Float.isNaN(hr) ? "null" : String.format(java.util.Locale.US, "%.3f", hr));
    }

    static double[] arr(java.util.List<Double> l) {
        double[] a = new double[l.size()];
        for (int i = 0; i < a.length; i++) a[i] = l.get(i);
        java.util.Arrays.sort(a);
        return a;
    }

    static double pct(double[] sorted, int p) {
        if (sorted.length == 0) return -1;
        return sorted[Math.min(sorted.length - 1, (int) (p / 100.0 * (sorted.length - 1)))];
    }

    static Size biggest(Size[] s) {
        Size m = s[0];
        for (Size z : s) if ((long) z.getWidth() * z.getHeight() > (long) m.getWidth() * m.getHeight()) m = z;
        return m;
    }

    CameraDevice open(CameraManager cm, String id) throws Exception {
        final CameraDevice[] box = new CameraDevice[1];
        CountDownLatch l = new CountDownLatch(1);
        cm.openCamera(id, new CameraDevice.StateCallback() {
            public void onOpened(CameraDevice d) { box[0] = d; l.countDown(); }
            public void onDisconnected(CameraDevice d) { d.close(); l.countDown(); }
            public void onError(CameraDevice d, int e) { d.close(); l.countDown(); }
        }, h);
        if (!l.await(8, TimeUnit.SECONDS) || box[0] == null) throw new RuntimeException("камера не открылась");
        return box[0];
    }
}
