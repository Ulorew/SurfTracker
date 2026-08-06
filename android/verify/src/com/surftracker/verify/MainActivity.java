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
 * Сверка инференса на устройстве (тикет "телефон", п.1).
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

    /** Полная точность: сверка идёт с допуском 1e-5, округление её сломает. */
    static String fmt(float v) {
        return Float.toString(v);
    }
}
