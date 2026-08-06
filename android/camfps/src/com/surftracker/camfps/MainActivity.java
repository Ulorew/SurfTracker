package com.surftracker.camfps;

import android.app.Activity;
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
 * Практический прогон комбинации потоков (тикет "телефон", п.4б и 4в).
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
        runOnUiThread(() -> getWindow().addFlags(
                android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
        File dir = getExternalFilesDir(null);
        String combo = getIntent().getStringExtra("combo");
        if (combo == null) combo = "priv1080+rec4k+yuvmax";
        int seconds = getIntent().getIntExtra("seconds", 60);
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
            Size jpegMax = biggest(map.getOutputSizes(ImageFormat.JPEG));

            File video = new File(dir, "rec_" + combo + ".mp4");
            if (video.exists()) video.delete();

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
            // знает про свой кодировщик больше, чем я.
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
            // Без этих слушателей отказ записи выглядит как "камера отдала мало
            // кадров": когда MediaRecorder перестаёт разбирать буферы, встаёт
            // ВЕСЬ конвейер, включая другие потоки. Именно так и вышло: во всех
            // конфигурациях приходило ~9 с из 30-60, и виноват был не YUV.
            rec.setOnInfoListener((mr, what, extra) ->
                    Log.w(TAG, "MediaRecorder info what=" + what + " extra=" + extra));
            rec.setOnErrorListener((mr, what, extra) ->
                    Log.e(TAG, "MediaRecorder ОШИБКА what=" + what + " extra=" + extra));
            rec.prepare();
            Surface recSurf = rec.getSurface();
            targets.add(recSurf);

            // Контроль: тот же прогон без YUV или с YUV поменьше. Без него
            // нельзя отличить "устройство не тянет комбинацию" от "мой код
            // затыкает конвейер", а это разные выводы.
            String yuvMode = getIntent().getStringExtra("yuv");
            if (yuvMode == null) yuvMode = "max";
            Size yuvSize = "1080".equals(yuvMode) ? new Size(1920, 1080) : yuvMax;
            ImageReader yuv = yuvReader = ImageReader.newInstance(
                    yuvSize.getWidth(), yuvSize.getHeight(), ImageFormat.YUV_420_888, 3);
            yuv.setOnImageAvailableListener(r -> {
                try (Image im = r.acquireLatestImage()) {
                    if (im != null) {
                        long t = System.nanoTime();
                        firstNs.compareAndSet(0, t); lastNs.set(t);
                        yuvFrames.incrementAndGet();
                    }
                }
            }, h);
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
            if (!"none".equals(yuvMode)) rq.addTarget(yuv.getSurface());
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
                        float[][][] o2 = new float[1][5][8400];
                        while (!stop[0]) {
                            bin.rewind();
                            long t0 = System.nanoTime();
                            it.run(bin, o2);
                            lat.add((System.nanoTime() - t0) / 1e6);
                        }
                        it.close();
                    } catch (Throwable t) { Log.e(TAG, "инференс: " + t); }
                });
                infer.start();
            }

            long end = System.currentTimeMillis() + seconds * 1000L;
            long nextJpeg = System.currentTimeMillis();
            while (System.currentTimeMillis() < end) {
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
                    + "\"jpeg_frames\": %d, \"video_bytes\": %d}\n",
                    combo, seconds, "none".equals(yuvMode) ? "нет" : yuvSize.toString(), yuvFrames.get(),
                    yuvFrames.get() / Math.max(secs, 1e-9), results.get(),
                    results.get() / (double) seconds, maxGap / 1e6, medGap,
                    jpegFrames.get(), video.length()));
            if (!lat.isEmpty()) {
                double[] a = new double[lat.size()];
                for (int i = 0; i < a.length; i++) a[i] = lat.get(i);
                java.util.Arrays.sort(a);
                android.os.PowerManager pmg = getSystemService(android.os.PowerManager.class);
                float hr = Float.NaN;
                try { hr = pmg.getThermalHeadroom(60); } catch (Throwable ignored) { }
                Log.i(TAG, String.format(java.util.Locale.US,
                        "ИНФЕРЕНС ПОД ЗАПИСЬЮ: прогонов %d p50 %.1f p95 %.1f headroom %s",
                        a.length, a[a.length / 2], a[(int) (0.95 * (a.length - 1))],
                        Float.isNaN(hr) ? "нет" : String.format(java.util.Locale.US, "%.3f", hr)));
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
        finish();
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
