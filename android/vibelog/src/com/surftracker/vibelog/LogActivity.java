package com.surftracker.vibelog;

import android.app.Activity;
import android.graphics.Color;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.io.BufferedWriter;
import java.io.File;
import java.io.FileWriter;

/**
 * Минимальный логгер акселерометра. Никакой обработки на телефоне:
 * копим сырые отсчёты в память и сбрасываем в CSV. Разбор — на ноутбуке.
 *
 * Метка времени пишется как event.timestamp БЕЗ ПОДМЕНЫ на System.nanoTime():
 * фактическую частоту потом считаем по разностям меток, а не по заявленной.
 *
 * Headless-режим для adb:
 *   am start -n com.surftracker.vibelog/.LogActivity --ei secs 10 --es tag desk
 * — сам запишет secs секунд, сохранит CSV и закроется.
 */
public class LogActivity extends Activity implements SensorEventListener {

    private static final String TAG = "VIBELOG";
    private static final int CAP = 600000;   // ~20 мин при 500 Гц

    private SensorManager sm;
    private Sensor acc;

    private final long[] ts = new long[CAP];
    private final float[] ax = new float[CAP];
    private final float[] ay = new float[CAP];
    private final float[] az = new float[CAP];

    private volatile int n = 0;
    private volatile boolean recording = false;

    private String tag = "rec";
    private int autoSecs = 0;
    private int periodUs = 0;
    private long wallStartMs = 0;
    private long firstEventNs = 0;

    private TextView status;
    private Button btnStart, btnStop;
    private final Handler h = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setShowWhenLocked(true);
        setTurnScreenOn(true);

        sm = (SensorManager) getSystemService(SENSOR_SERVICE);
        acc = sm.getDefaultSensor(Sensor.TYPE_ACCELEROMETER);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.BLACK);
        root.setPadding(40, 80, 40, 40);
        root.setGravity(Gravity.CENTER_HORIZONTAL);

        status = new TextView(this);
        status.setTextColor(Color.WHITE);
        status.setTextSize(18);
        root.addView(status);

        btnStart = new Button(this);
        btnStart.setText("СТАРТ");
        btnStart.setTextSize(26);
        btnStart.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { startRec(); }
        });
        root.addView(btnStart);

        btnStop = new Button(this);
        btnStop.setText("СТОП + CSV");
        btnStop.setTextSize(26);
        btnStop.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { stopAndSave(); }
        });
        root.addView(btnStop);

        setContentView(root);

        if (acc == null) {
            status.setText("АКСЕЛЕРОМЕТРА НЕТ");
            Log.e(TAG, "no accelerometer");
            return;
        }

        String info = "sensor=" + acc.getName()
                + "\nvendor=" + acc.getVendor()
                + "\nminDelay=" + acc.getMinDelay() + " us"
                + "\n-> max rate = " + (acc.getMinDelay() > 0 ? (1e6 / acc.getMinDelay()) : -1) + " Hz"
                + "\nresolution=" + acc.getResolution() + " m/s^2"
                + "\nmaxRange=" + acc.getMaximumRange() + " m/s^2"
                + "\nfifoMax=" + acc.getFifoMaxEventCount();
        Log.i(TAG, "SENSORINFO " + info.replace('\n', ' '));
        status.setText(info + "\n\nготов");

        autoSecs = getIntent().getIntExtra("secs", 0);
        periodUs = getIntent().getIntExtra("us", SensorManager.SENSOR_DELAY_FASTEST);
        String t = getIntent().getStringExtra("tag");
        if (t != null) tag = t;
        if (autoSecs > 0) {
            h.postDelayed(new Runnable() {
                public void run() {
                    startRec();
                    h.postDelayed(new Runnable() {
                        public void run() { stopAndSave(); finish(); }
                    }, autoSecs * 1000L);
                }
            }, 800);   // дать окну проснуться
        }
    }

    private void startRec() {
        if (recording || acc == null) return;
        n = 0;
        firstEventNs = 0;
        wallStartMs = System.currentTimeMillis();
        recording = true;
        // samplingPeriodUs = SENSOR_DELAY_FASTEST, maxReportLatencyUs = 0 (без батчинга)
        // periodUs: 0 = FASTEST, иначе желаемый период в мкс (нужно для проверки на алиасинг)
        Log.i(TAG, "REGISTER periodUs=" + periodUs);
        sm.registerListener(this, acc, periodUs, 0);
        status.setText("ПИШЕМ...");
        Log.i(TAG, "REC START tag=" + tag);
    }

    private void stopAndSave() {
        if (!recording) return;
        recording = false;
        sm.unregisterListener(this);
        int cnt = n;

        File dir = getExternalFilesDir(null);
        File f = new File(dir, tag + "_" + wallStartMs + ".csv");
        try {
            BufferedWriter w = new BufferedWriter(new FileWriter(f), 1 << 20);
            w.write("# sensor=" + acc.getName() + " vendor=" + acc.getVendor() + "\n");
            w.write("# minDelayUs=" + acc.getMinDelay()
                    + " declaredMaxRateHz=" + (acc.getMinDelay() > 0 ? (1e6 / acc.getMinDelay()) : -1) + "\n");
            w.write("# resolution=" + acc.getResolution()
                    + " maxRange=" + acc.getMaximumRange()
                    + " fifoMax=" + acc.getFifoMaxEventCount() + "\n");
            w.write("# samples=" + cnt + " wallStartMs=" + wallStartMs + " tag=" + tag + "\n");
            w.write("# t_ns = SensorEvent.timestamp RAW (не подменён на nanoTime)\n");
            w.write("t_ns,ax,ay,az\n");
            StringBuilder sb = new StringBuilder(1 << 16);
            for (int i = 0; i < cnt; i++) {
                sb.append(ts[i]).append(',')
                  .append(ax[i]).append(',')
                  .append(ay[i]).append(',')
                  .append(az[i]).append('\n');
                if (sb.length() > (1 << 15)) { w.write(sb.toString()); sb.setLength(0); }
            }
            w.write(sb.toString());
            w.close();
            double span = cnt > 1 ? (ts[cnt - 1] - ts[0]) / 1e9 : 0;
            double hz = span > 0 ? (cnt - 1) / span : 0;
            String msg = "CSV " + f.getAbsolutePath() + " samples=" + cnt
                    + " span=" + String.format("%.3f", span) + "s"
                    + " effHz=" + String.format("%.1f", hz);
            Log.i(TAG, msg);
            status.setText(msg.replace(' ', '\n'));
        } catch (Exception e) {
            Log.e(TAG, "save failed", e);
            status.setText("ОШИБКА: " + e);
        }
    }

    @Override
    public void onSensorChanged(SensorEvent e) {
        if (!recording) return;
        int i = n;
        if (i >= CAP) return;
        ts[i] = e.timestamp;          // как есть, без подмены
        ax[i] = e.values[0];
        ay[i] = e.values[1];
        az[i] = e.values[2];
        n = i + 1;
    }

    @Override
    public void onAccuracyChanged(Sensor s, int a) { }

    @Override
    protected void onPause() {
        super.onPause();
        if (recording && autoSecs <= 0) stopAndSave();
    }
}
