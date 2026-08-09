package com.surftracker.camfps;

import android.app.Activity;
import android.graphics.Bitmap;
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
import android.view.Surface;

import org.tensorflow.lite.Interpreter;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Стенд сквозной сверки камерного зрения (тикет «камерное зрение», блок А).
 *
 * Один снимок по команде: захват YUV на максимальном размере, сохранение
 * СЫРЫХ плоскостей до любой конвертации, затем боевой путь Yuv.convert ->
 * тензор -> инференс. Рядом кладётся пост-конвертационный кроп картинкой.
 *
 * Что здесь принципиально ново по сравнению с прежним «контролем честности»:
 * сохраняется вход ДО конвертации. Прежний контроль сохранял буфер ПОСЛЕ
 * неё и сверял его сам с собой — совпадение было гарантировано конструкцией
 * и не могло обнаружить ни одной ошибки камерного пути.
 *
 * Кроп НАВОДИТСЯ (cx, cy в пикселях сенсора), а не берётся из фиксированной
 * точки: иначе версию «в квадрате не было цели» не отличить ни от чего
 * другого, и ноль детекций опять останется необъяснённым.
 *
 *   am start -n com.surftracker.camfps/.StandActivity \
 *       --es tag f01 --ei cx 2040 --ei cy 1530 [--ei side 640] [--ez hist true]
 */
public class StandActivity extends Activity {
    static final String TAG = "stand";
    HandlerThread bg;
    Handler h;
    ImageReader reader;   // сильная ссылка: локальный ImageReader финализатор
                          // бросал BufferQueue и захват вставал (см. MainActivity)

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        bg = new HandlerThread("stand"); bg.start(); h = new Handler(bg.getLooper());
        runOnUiThread(() -> {
            android.widget.TextView tv = new android.widget.TextView(this);
            tv.setText("стенд: снимок");
            setContentView(tv);
            getWindow().addFlags(
                    android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                    | android.view.WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                    | android.view.WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED);
        });
        if (checkSelfPermission(android.Manifest.permission.CAMERA)
                != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{android.Manifest.permission.CAMERA}, 1);
        } else new Thread(this::shot).start();
    }

    @Override public void onRequestPermissionsResult(int c, String[] p, int[] g) {
        new Thread(this::shot).start();
    }

    void shot() {
        String tag = getIntent().getStringExtra("tag");
        if (tag == null) tag = "shot";
        // Сторона ЗАПРОШЕННОГО окна. Больше короткой стороны сенсора взять
        // нечего, меньше входа сети — незачем (пол тот же, что в петле).
        final int S = Math.max(640, getIntent().getIntExtra("side", 640));
        boolean wantHist = getIntent().getBooleanExtra("hist", false);
        File dir = new File(getExternalFilesDir(null), "stand");
        dir.mkdirs();
        File base = new File(dir, tag);
        StringBuilder j = new StringBuilder("{");
        CameraDevice dev = null;
        try {
            CameraManager cm = getSystemService(CameraManager.class);
            String id = "0";
            StreamConfigurationMap map = cm.getCameraCharacteristics(id)
                    .get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            Size yuvMax = biggest(map.getOutputSizes(ImageFormat.YUV_420_888));
            int W = yuvMax.getWidth(), H = yuvMax.getHeight();

            // Центр кропа: по умолчанию центр кадра — та же точка, что брал
            // прежний прогон, чтобы «ноль детекций» можно было воспроизвести
            // в тех же условиях, а не только в улучшенных.
            int cx = getIntent().getIntExtra("cx", W / 2);
            int cy = getIntent().getIntExtra("cy", H / 2);
            int Sc = Math.min(S, Math.min(W, H));   // потолок кадром
            int cropX = clamp(cx - Sc / 2, 0, W - Sc);
            int cropY = clamp(cy - Sc / 2, 0, H - Sc);
            j.append("\"tag\":\"").append(tag).append("\",\"sensor_w\":").append(W)
             .append(",\"sensor_h\":").append(H).append(",\"side\":").append(Sc)
             .append(",\"aim_cx\":").append(cx).append(",\"aim_cy\":").append(cy)
             .append(",\"crop_x\":").append(cropX).append(",\"crop_y\":").append(cropY);

            reader = ImageReader.newInstance(W, H, ImageFormat.YUV_420_888, 2);
            // Держим ВСЕГДА последний пришедший кадр, предыдущий закрываем:
            // снимок должен быть с устоявшимися экспозицией и балансом, а не
            // первым, что пришло из холодного ISP.
            final AtomicReference<Image> latest = new AtomicReference<>();
            reader.setOnImageAvailableListener(r -> {
                Image im = r.acquireLatestImage();
                if (im == null) return;
                Image prev = latest.getAndSet(im);
                if (prev != null) prev.close();
            }, h);

            dev = open(cm, id);
            List<Surface> targets = new ArrayList<>();
            targets.add(reader.getSurface());
            final CameraDevice fd = dev;
            final android.hardware.camera2.CameraCaptureSession[] box = new android.hardware.camera2.CameraCaptureSession[1];
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

            // Прогрев: первые кадры идут с неустоявшимися экспозицией и
            // балансом белого, и снимок экрана по ним говорил бы о разгоне
            // ISP, а не о конвейере.
            CaptureRequest.Builder rq = fd.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
            rq.addTarget(reader.getSurface());
            box[0].setRepeatingRequest(rq.build(), null, h);
            int warm = getIntent().getIntExtra("warmup_ms", 1500);
            Thread.sleep(warm);
            box[0].stopRepeating();
            Thread.sleep(200);            // дать последнему кадру дойти до слушателя
            Image im = latest.getAndSet(null);
            if (im == null) throw new RuntimeException("кадр не пришёл");
            j.append(",\"warmup_ms\":").append(warm);

            // Блок Б тикета: профилирование кропа. Ветка стоит ЗДЕСЬ, на уже
            // захваченном кадре, чтобы стоимость конвертации не смешивалась со
            // стоимостью захвата — это разные статьи бюджета такта.
            if (getIntent().getBooleanExtra("profile", false)) {
                profile(im, j, W, H, cx, cy);
                im.close();
                j.append(",\"ok\":true");
                return;   // finally допишет json и закроет камеру
            }

            // 1. СЫРОЙ вход — до любой конвертации
            Yuv.dumpRaw(im, base);
            if (wantHist) {
                long[] hist = Yuv.histY(im);
                long total = 0, below = 0, above = 0;
                for (int v = 0; v < 256; v++) {
                    total += hist[v];
                    if (v < 16) below += hist[v];
                    if (v > 235) above += hist[v];
                }
                j.append(",\"y_below_16\":").append(below / (double) total)
                 .append(",\"y_above_235\":").append(above / (double) total);
                StringBuilder hs = new StringBuilder("[");
                for (int v = 0; v < 256; v++) { if (v > 0) hs.append(","); hs.append(hist[v]); }
                hs.append("]");
                write(new File(base.getPath() + ".hist.json"), hs.toString());
            }

            // 2. БОЕВОЙ путь: тот же Yuv.crop, что в MainActivity. Тензор ВСЕГДА
            // 640 — вход сети фиксирован; окно крупнее честно уменьшается.
            // Раньше здесь выделялся тензор S*S, и при side != 640 в сеть уходил
            // буфер, которого она не принимает.
            final int NET = 640;
            ByteBuffer bin = ByteBuffer.allocateDirect(4 * 3 * NET * NET).order(ByteOrder.nativeOrder());
            long t0 = System.nanoTime();
            long sink = Yuv.crop(im, bin, cropX, cropY, Sc, NET, 0);
            double convMs = (System.nanoTime() - t0) / 1e6;
            j.append(",\"convert_ms\":").append(round3(convMs)).append(",\"sink\":").append(sink);
            im.close();

            // 3. Пост-конвертационный кроп картинкой — ровно то, что ушло в сеть
            savePng(bin, NET, new File(base.getPath() + ".rgb.png"));

            // 4. Инференс на этом же тензоре
            File model = new File(getExternalFilesDir(null), "surf_w8a32.tflite");
            if (model.exists()) {
                Interpreter.Options o = new Interpreter.Options();
                o.setNumThreads(1);
                Interpreter it = new Interpreter(model, o);
                float[][][] out = new float[1][5][8400];
                bin.rewind();
                long t1 = System.nanoTime();
                it.run(bin, out);
                double infMs = (System.nanoTime() - t1) / 1e6;
                it.close();
                j.append(",\"infer_ms\":").append(round3(infMs));
                    // Координаты — в пикселях ОБЛАСТИ Sc, а не тензора: сеть
                // видела уменьшенную копию, и обратно надо в те же пиксели,
                // из которых её сделали.
                j.append(",\"detections\":").append(detections(out[0], Sc));
            } else {
                j.append(",\"error_model\":\"нет surf_w8a32.tflite\"");
            }
            j.append(",\"ok\":true");
        } catch (Throwable t) {
            Log.e(TAG, "стенд: " + t);
            j.append(",\"ok\":false,\"error\":\"").append(String.valueOf(t).replace('"', '\'')).append("\"");
        } finally {
            if (dev != null) dev.close();
            try { write(new File(base.getPath() + ".json"), j.append("}").toString()); }
            catch (Throwable ignored) {}
            Log.i(TAG, "ГОТОВО " + base.getPath());
            finish();
        }
    }

    /** Приёмник аккумуляторов: без него JIT выбрасывает циклы стадий 1 и 2
     *  целиком, и «чтение» с «конвертацией» оказываются бесплатными. */
    volatile long sink;

    /**
     * Профиль кропа (тикет «камерное зрение», блок Б).
     *
     * Три стадии по отдельности (1 = выборка плоскостей, 2 = + конвертация
     * YUV->RGB, 0 = + запись тензора) на развёртке по стороне окна S, в двух
     * положениях (центр кадра и угол — эффекты stride), reps повторов на
     * каждую точку.
     *
     * Замеряется путь С УМЕНЬШЕНИЕМ до 640: вход сети фиксирован, и только
     * такой путь способен отдать сети то окно, которое просит петля. Нынешний
     * путь без ресайза меряется отдельной строкой при S=640 — при больших S
     * он не боевой (тензор S*S сеть не принимает), и мерить его там значило бы
     * строить кривую того, чего никто не собирается делать.
     *
     * ОГОВОРКА, которую обязан знать читатель чисел: повторы идут по ОДНОМУ
     * удержанному кадру, то есть кэш прогрет. В бою каждый такт приходит новый
     * буфер. Поэтому рядом снимается контроль cold: по одному замеру на свежих
     * кадрах (см. stand.py profile --cold).
     */
    void profile(Image im, StringBuilder j, int W, int H, int cx, int cy) {
        int reps = getIntent().getIntExtra("reps", 50);
        final int OUT = 640;
        int[] sides = parseInts(getIntent().getStringExtra("sides"),
                                 new int[]{640, 960, 1440, 2160, 3060});
        ByteBuffer out = ByteBuffer.allocateDirect(4 * 3 * OUT * OUT).order(ByteOrder.nativeOrder());
        j.append(",\"reps\":").append(reps).append(",\"out_side\":").append(OUT)
         .append(",\"battery_c_before\":").append(batteryC()).append(",\"runs\":[");
        boolean first = true;
        for (int S : sides) {
            if (S > Math.min(W, H)) continue;
            for (int pi = 0; pi < 2; pi++) {
                String pos = pi == 0 ? "center" : "corner";
                int cropX = pi == 0 ? clamp(cx - S / 2, 0, W - S) : 0;
                int cropY = pi == 0 ? clamp(cy - S / 2, 0, H - S) : 0;
                for (int stage : new int[]{1, 2, 0}) {
                    first = row(j, first, "scaled", S, pos, stage, cropX, cropY,
                                 measure(im, out, cropX, cropY, S, OUT, stage, reps, 1));
                }
            }
        }
        // Нынешний боевой путь: без ресайза, ровно 640 натурой.
        ByteBuffer raw = ByteBuffer.allocateDirect(4 * 3 * 640 * 640).order(ByteOrder.nativeOrder());
        int rx = clamp(cx - 320, 0, W - 640), ry = clamp(cy - 320, 0, H - 640);
        for (int stage : new int[]{1, 2, 0}) {
            first = row(j, first, "native640", 640, "center", stage, rx, ry,
                         measure(im, raw, rx, ry, 640, 640, stage, reps, 0));
        }
        j.append("],\"battery_c_after\":").append(batteryC())
         .append(",\"sink\":").append(sink);
    }

    /** reps замеров одной точки + 3 прогрева (иначе первый замер меряет JIT). */
    /** mode: 0 — прямой путь без ресайза, 1 — билинейный с уменьшением. */
    long one(int mode, Image im, ByteBuffer out, int cropX, int cropY,
              int S, int OUT, int stage) {
        if (mode == 0) return Yuv.convert(im, out, cropX, cropY, S, stage);
        return Yuv.convertScaled(im, out, cropX, cropY, S, OUT, stage);
    }

    double[] measure(Image im, ByteBuffer out, int cropX, int cropY, int S, int OUT,
                      int stage, int reps, int mode) {
        for (int r = 0; r < 3; r++)
            sink += one(mode, im, out, cropX, cropY, S, OUT, stage);
        double[] ms = new double[reps];
        for (int r = 0; r < reps; r++) {
            long t0 = System.nanoTime();
            sink += one(mode, im, out, cropX, cropY, S, OUT, stage);
            ms[r] = (System.nanoTime() - t0) / 1e6;
        }
        java.util.Arrays.sort(ms);
        return new double[]{ms[reps / 2], ms[(int) (reps * 0.95)], ms[0], ms[reps - 1]};
    }

    boolean row(StringBuilder j, boolean first, String path, int S, String pos, int stage,
                 int cropX, int cropY, double[] q) {
        if (!first) j.append(",");
        j.append("{\"path\":\"").append(path).append("\",\"side\":").append(S)
         .append(",\"pos\":\"").append(pos).append("\",\"stage\":").append(stage)
         .append(",\"crop_x\":").append(cropX).append(",\"crop_y\":").append(cropY)
         .append(",\"p50\":").append(round3(q[0])).append(",\"p95\":").append(round3(q[1]))
         .append(",\"min\":").append(round3(q[2])).append(",\"max\":").append(round3(q[3]))
         .append("}");
        return false;
    }

    static int[] parseInts(String csv, int[] def) {
        if (csv == null || csv.isEmpty()) return def;
        String[] p = csv.split(",");
        int[] a = new int[p.length];
        for (int i = 0; i < p.length; i++) a[i] = Integer.parseInt(p[i].trim());
        return a;
    }

    double batteryC() {
        android.content.Intent bi = registerReceiver(null,
                new android.content.IntentFilter(android.content.Intent.ACTION_BATTERY_CHANGED));
        return bi == null ? -1 : bi.getIntExtra("temperature", -1) / 10.0;
    }

    /** Выход [1][5][8400] -> список рамок выше порога, в пикселях КРОПА.
     *  Порог низкий: решение о пороге принимается на ноутбуке, телефон не
     *  должен ничего отфильтровывать молча. */
    static String detections(float[][] o, int S) {
        StringBuilder s = new StringBuilder("[");
        int n = o[0].length;
        boolean first = true;
        for (int i = 0; i < n; i++) {
            float conf = o[4][i];
            if (conf < 0.01f) continue;
            float cx = o[0][i] * S, cy = o[1][i] * S, w = o[2][i] * S, hh = o[3][i] * S;
            if (!first) s.append(",");
            first = false;
            s.append("{\"x0\":").append(round3(cx - w / 2)).append(",\"y0\":").append(round3(cy - hh / 2))
             .append(",\"x1\":").append(round3(cx + w / 2)).append(",\"y1\":").append(round3(cy + hh / 2))
             .append(",\"conf\":").append(round3(conf)).append("}");
        }
        return s.append("]").toString();
    }

    static void savePng(ByteBuffer bin, int S, File f) throws Exception {
        int[] px = new int[S * S];
        int PLANE = S * S;
        for (int i = 0; i < PLANE; i++) {
            int r = (int) (bin.getFloat(i * 4) * 255 + 0.5f);
            int g = (int) (bin.getFloat((PLANE + i) * 4) * 255 + 0.5f);
            int b = (int) (bin.getFloat((2 * PLANE + i) * 4) * 255 + 0.5f);
            px[i] = 0xFF000000 | (r << 16) | (g << 8) | b;
        }
        Bitmap bm = Bitmap.createBitmap(px, S, S, Bitmap.Config.ARGB_8888);
        try (FileOutputStream fo = new FileOutputStream(f)) {
            bm.compress(Bitmap.CompressFormat.PNG, 100, fo);
        }
    }

    static void write(File f, String s) throws Exception {
        try (OutputStreamWriter w = new OutputStreamWriter(new FileOutputStream(f))) { w.write(s); }
    }

    static double round3(double v) { return Math.round(v * 1000.0) / 1000.0; }

    static int clamp(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }

    static Size biggest(Size[] a) {
        Size best = a[0];
        for (Size s : a) if ((long) s.getWidth() * s.getHeight() > (long) best.getWidth() * best.getHeight()) best = s;
        return best;
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
