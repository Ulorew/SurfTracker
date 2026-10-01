package com.surftracker.verify;

import android.app.Activity;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Bundle;
import android.util.Log;

import java.io.File;
import java.io.FileWriter;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;

import org.tensorflow.lite.Interpreter;

/**
 * Сверка инференса на устройстве.
 *
 * Контракт входа/выхода взят из export/phone_pack/README.md и НЕ угадывается:
 *   вход  [1,3,640,640] float32, NCHW, порядок каналов RGB, значения /255;
 *   выход [1,5,8400], строки cx,cy,w,h,conf, координаты НОРМИРОВАНЫ.
 * Раннер отдаёт выходы СЫРЫМИ (без умножения на 640) — домножение живёт в
 * компараторе на ноутбуке, и дублировать его здесь значило бы получить
 * расхождение ровно в 640 раз и искать его в модели.
 *
 * Всё общение — через файлы в собственной внешней папке приложения:
 * туда adb push кладёт пакет, оттуда adb pull забирает результат, и никаких
 * разрешений на хранилище не нужно.
 */
public class MainActivity extends Activity {
    static final String TAG = "SurfVerify";
    static final int SIDE = 640;
    static final int ANCHORS = 8400;
    static final int ROWS = 5;
    static final float CONF_MIN = 0.25f;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        new Thread(this::run).start();
    }

    void run() {
        File dir = getExternalFilesDir(null);
        try {
            String mode = getIntent().getStringExtra("mode");
            if ("bench".equals(mode)) { bench(dir); return; }
            if ("thermal".equals(mode)) { thermal(dir); return; }
            String modelName = getIntent().getStringExtra("model");
            if (modelName == null) modelName = "surf_w8a32.tflite";
            int threads = getIntent().getIntExtra("threads", 1);
            boolean xnn = !"0".equals(getIntent().getStringExtra("xnnpack"));

            File model = new File(dir, modelName);
            File frames = new File(dir, "frames");
            if (!model.exists()) throw new IllegalStateException("нет модели: " + model);
            if (!frames.isDirectory()) throw new IllegalStateException("нет кадров: " + frames);

            Interpreter.Options opts = new Interpreter.Options();
            opts.setNumThreads(threads);
            try {
                Interpreter.Options.class.getMethod("setUseXNNPACK", boolean.class)
                        .invoke(opts, xnn);
            } catch (Throwable t) {
                // XNNPACK в этой сборке включён по умолчанию и флага нет —
                // сообщаем в лог, а не молчим: конфигурация обязана быть явной
                Log.w(TAG, "setUseXNNPACK недоступен: " + t);
            }

            Interpreter interp = new Interpreter(model, opts);
            int[] inShape = interp.getInputTensor(0).shape();
            int[] outShape = interp.getOutputTensor(0).shape();
            Log.i(TAG, "вход " + Arrays.toString(inShape) + " " + interp.getInputTensor(0).dataType()
                    + ", выход " + Arrays.toString(outShape));

            File[] files = frames.listFiles((d, n) -> n.toLowerCase().endsWith(".png"));
            Arrays.sort(files);
            StringBuilder json = new StringBuilder("{\n");

            ByteBuffer in = ByteBuffer.allocateDirect(4 * 3 * SIDE * SIDE).order(ByteOrder.nativeOrder());
            float[][][] out = new float[1][ROWS][ANCHORS];

            for (int fi = 0; fi < files.length; fi++) {
                Bitmap bmp = BitmapFactory.decodeFile(files[fi].getAbsolutePath());
                if (bmp.getWidth() != SIDE || bmp.getHeight() != SIDE) {
                    // ресайза здесь быть не должно: кадры уже 640x640. Молчаливый
                    // ресайз сломал бы сверку так, что это выглядело бы дефектом модели
                    throw new IllegalStateException("кадр не 640x640: " + files[fi].getName()
                            + " " + bmp.getWidth() + "x" + bmp.getHeight());
                }
                int[] px = new int[SIDE * SIDE];
                bmp.getPixels(px, 0, SIDE, 0, 0, SIDE, SIDE);

                in.rewind();
                // NCHW: сначала весь канал R, потом G, потом B
                for (int c = 0; c < 3; c++) {
                    int shift = c == 0 ? 16 : (c == 1 ? 8 : 0);   // ARGB -> R,G,B
                    for (int i = 0; i < px.length; i++) {
                        in.putFloat(((px[i] >> shift) & 0xFF) / 255.0f);
                    }
                }
                in.rewind();
                if (fi == 0) {
                    // Отпечаток входного тензора: если он совпадёт с ноутбучным,
                    // причина расхождения не в препроцессинге, и искать надо
                    // дальше по цепочке. Сумма — глобальный отпечаток, первые
                    // значения каналов показывают порядок RGB и раскладку.
                    double sum = 0;
                    for (int i = 0; i < 3 * SIDE * SIDE; i++) sum += in.getFloat(i * 4);
                    in.rewind();
                    Log.i(TAG, String.format("ОТПЕЧАТОК px0=%08x сумма=%.6f "
                            + "R0=%.6f G0=%.6f B0=%.6f",
                            px[0], sum, in.getFloat(0),
                            in.getFloat(4 * SIDE * SIDE), in.getFloat(8 * SIDE * SIDE)));
                }
                interp.run(in, out);

                if (fi > 0) json.append(",\n");
                json.append("  \"").append(files[fi].getName()).append("\": [");
                boolean first = true;
                for (int a = 0; a < ANCHORS; a++) {
                    float conf = out[0][4][a];
                    if (conf < CONF_MIN) continue;
                    if (!first) json.append(", ");
                    first = false;
                    json.append("[")
                        .append(fmt(out[0][0][a])).append(", ")
                        .append(fmt(out[0][1][a])).append(", ")
                        .append(fmt(out[0][2][a])).append(", ")
                        .append(fmt(out[0][3][a])).append(", ")
                        .append(fmt(conf)).append("]");
                }
                json.append("]");
                Log.i(TAG, "кадр " + (fi + 1) + "/" + files.length + " " + files[fi].getName());
            }
            json.append("\n}\n");

            File outFile = new File(dir, "phone_out.json");
            try (FileWriter w = new FileWriter(outFile)) { w.write(json.toString()); }
            interp.close();
            Log.i(TAG, "ГОТОВО кадров=" + files.length + " файл=" + outFile);
        } catch (Throwable t) {
            Log.e(TAG, "ОШИБКА " + t, t);
            try (FileWriter w = new FileWriter(new File(dir, "phone_error.txt"))) {
                w.write(String.valueOf(t));
            } catch (Exception ignored) { }
        }
        finish();
    }

    /**
     * Скорость. Бенчмарк живёт ВНУТРИ приложения, а не запускается
     * adb-бинарём, — и это принципиально: планировщик Android душит
     * фоновые процессы, и на многопоточном CPU разница видна.
     *
     * Меряется чистый interp.run() на одном и том же кадре: препроцессинг и
     * декод PNG в бюджет инференса не входят и мерились бы иначе.
     */
    void bench(File dir) throws Exception {
        String modelName = getIntent().getStringExtra("model");
        if (modelName == null) modelName = "surf_w8a32.tflite";
        int threads = getIntent().getIntExtra("threads", 1);
        boolean xnn = !"0".equals(getIntent().getStringExtra("xnnpack"));
        int warmup = getIntent().getIntExtra("warmup", 30);
        int runs = getIntent().getIntExtra("runs", 200);

        Interpreter.Options opts = new Interpreter.Options();
        opts.setNumThreads(threads);
        String xnnNote;
        try {
            Interpreter.Options.class.getMethod("setUseXNNPACK", boolean.class).invoke(opts, xnn);
            xnnNote = String.valueOf(xnn);
        } catch (Throwable t) {
            xnnNote = "флага нет, по умолчанию";
        }
        Interpreter interp = new Interpreter(new File(dir, modelName), opts);

        File[] files = new File(dir, "frames").listFiles((d, n) -> n.toLowerCase().endsWith(".png"));
        Arrays.sort(files);
        Bitmap bmp = BitmapFactory.decodeFile(files[0].getAbsolutePath());
        int[] px = new int[SIDE * SIDE];
        bmp.getPixels(px, 0, SIDE, 0, 0, SIDE, SIDE);
        ByteBuffer in = ByteBuffer.allocateDirect(4 * 3 * SIDE * SIDE).order(ByteOrder.nativeOrder());
        for (int c = 0; c < 3; c++) {
            int shift = c == 0 ? 16 : (c == 1 ? 8 : 0);
            for (int i = 0; i < px.length; i++) in.putFloat(((px[i] >> shift) & 0xFF) / 255.0f);
        }
        float[][][] out = new float[1][ROWS][ANCHORS];

        for (int i = 0; i < warmup; i++) { in.rewind(); interp.run(in, out); }

        long[] ns = new long[runs];
        for (int i = 0; i < runs; i++) {
            in.rewind();
            long t0 = System.nanoTime();
            interp.run(in, out);
            ns[i] = System.nanoTime() - t0;
        }
        interp.close();
        long[] sorted = ns.clone();
        Arrays.sort(sorted);
        double p50 = sorted[(int) (0.50 * (runs - 1))] / 1e6;
        double p95 = sorted[(int) (0.95 * (runs - 1))] / 1e6;
        double p99 = sorted[(int) (0.99 * (runs - 1))] / 1e6;
        double mn = sorted[0] / 1e6, mx = sorted[runs - 1] / 1e6;
        double mean = 0; for (long v : ns) mean += v / 1e6 / runs;

        String json = String.format(java.util.Locale.US,
                "{\"model\": \"%s\", \"threads\": %d, \"xnnpack\": \"%s\", "
                + "\"warmup\": %d, \"runs\": %d, \"p50_ms\": %.3f, \"p95_ms\": %.3f, "
                + "\"p99_ms\": %.3f, \"min_ms\": %.3f, \"max_ms\": %.3f, \"mean_ms\": %.3f}\n",
                modelName, threads, xnnNote, warmup, runs, p50, p95, p99, mn, mx, mean);
        try (FileWriter w = new FileWriter(new File(dir, "bench_t" + threads + ".json"))) {
            w.write(json);
        }
        Log.i(TAG, "БЕНЧ " + json.trim());
        finish();
    }

    /**
     * Пункт 3: тепло. Непрерывный инференс, кривая задержки по минутам,
     * getThermalHeadroom раз в 2 с, заряд в лог.
     *
     * Первая минута в отчёт не входит (прогрев) — но пишется, чтобы было
     * видно, с чего начиналось. NaN у headroom проверяется явно: MediaTek
     * может не отдавать его вовсе, и молча получить "нет данных" вместо
     * "перегрева нет" — худший исход.
     */
    void thermal(File dir) throws Exception {
        int minutes = getIntent().getIntExtra("minutes", 15);
        int threads = getIntent().getIntExtra("threads", 1);
        String modelName = getIntent().getStringExtra("model");
        if (modelName == null) modelName = "surf_w8a32.tflite";

        getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        Interpreter.Options opts = new Interpreter.Options();
        opts.setNumThreads(threads);
        Interpreter interp = new Interpreter(new File(dir, modelName), opts);

        File[] files = new File(dir, "frames").listFiles((d, n) -> n.toLowerCase().endsWith(".png"));
        Arrays.sort(files);
        Bitmap bmp = BitmapFactory.decodeFile(files[0].getAbsolutePath());
        int[] px = new int[SIDE * SIDE];
        bmp.getPixels(px, 0, SIDE, 0, 0, SIDE, SIDE);
        ByteBuffer in = ByteBuffer.allocateDirect(4 * 3 * SIDE * SIDE).order(ByteOrder.nativeOrder());
        for (int c = 0; c < 3; c++) {
            int shift = c == 0 ? 16 : (c == 1 ? 8 : 0);
            for (int i = 0; i < px.length; i++) in.putFloat(((px[i] >> shift) & 0xFF) / 255.0f);
        }
        float[][][] out = new float[1][ROWS][ANCHORS];

        android.os.PowerManager pm = getSystemService(android.os.PowerManager.class);
        android.os.BatteryManager bm = getSystemService(android.os.BatteryManager.class);

        StringBuilder j = new StringBuilder("{\n  \"minutes\": " + minutes
                + ", \"threads\": " + threads + ",\n  \"samples\": [\n");
        long t_end = System.currentTimeMillis() + minutes * 60_000L;
        long nextSample = System.currentTimeMillis();
        java.util.ArrayList<Double> bucket = new java.util.ArrayList<>();
        int minute = 0;
        boolean firstRow = true;
        long minuteEnd = System.currentTimeMillis() + 60_000L;

        while (System.currentTimeMillis() < t_end) {
            in.rewind();
            long t0 = System.nanoTime();
            interp.run(in, out);
            bucket.add((System.nanoTime() - t0) / 1e6);

            long now = System.currentTimeMillis();
            if (now >= nextSample) {
                nextSample = now + 2000;
                float hr = Float.NaN;
                try { hr = pm.getThermalHeadroom(60); } catch (Throwable ignored) { }
                int lvl = -1;
                try { lvl = bm.getIntProperty(android.os.BatteryManager.BATTERY_PROPERTY_CAPACITY); }
                catch (Throwable ignored) { }
                if (!firstRow) j.append(",\n");
                firstRow = false;
                j.append(String.format(java.util.Locale.US,
                        "    {\"t_s\": %d, \"headroom\": %s, \"battery\": %d, \"status\": %d}",
                        (minutes * 60 - (t_end - now) / 1000), Float.isNaN(hr) ? "null" : String.format(java.util.Locale.US, "%.4f", hr),
                        lvl, pm.getCurrentThermalStatus()));
            }
            if (now >= minuteEnd) {
                minuteEnd = now + 60_000L;
                double[] a = new double[bucket.size()];
                for (int i = 0; i < a.length; i++) a[i] = bucket.get(i);
                Arrays.sort(a);
                Log.i(TAG, String.format(java.util.Locale.US,
                        "ТЕПЛО минута %d: прогонов %d p50 %.1f p95 %.1f",
                        ++minute, a.length, a[a.length / 2], a[(int) (0.95 * (a.length - 1))]));
                j.append(String.format(java.util.Locale.US,
                        ",\n    {\"minute\": %d, \"runs\": %d, \"p50_ms\": %.2f, \"p95_ms\": %.2f}",
                        minute, a.length, a[a.length / 2], a[(int) (0.95 * (a.length - 1))]));
                bucket.clear();
            }
        }
        interp.close();
        j.append("\n  ]\n}\n");
        try (FileWriter w = new FileWriter(new File(dir, "thermal.json"))) { w.write(j.toString()); }
        Log.i(TAG, "ТЕПЛО ГОТОВО");
        finish();
    }

    /** Полная точность: сверка идёт с допуском 1e-5, округление её сломает. */
    static String fmt(float v) {
        return Float.toString(v);
    }
}
