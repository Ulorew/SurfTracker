package com.surftracker.vibelog;

import android.content.Context;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;

import java.io.BufferedWriter;
import java.io.FileWriter;
import java.lang.reflect.Method;

/**
 * Тот же логгер, но без установки APK: запускается из adb shell через
 * app_process под пользователем shell. Понадобилось потому, что на аппарате
 * выключен MIUI-тумблер "Install via USB" (INSTALL_FAILED_USER_RESTRICTED),
 * а он переключается только руками на экране.
 *
 * Запуск:
 *   CLASSPATH=/data/local/tmp/vibelog.dex app_process /system/bin \
 *       com.surftracker.vibelog.Shell 10 /data/local/tmp/desk.csv
 *
 * Метка времени — event.timestamp КАК ЕСТЬ, без подмены на nanoTime.
 */
public class Shell {

    static final int CAP = 600000;
    static final long[] ts = new long[CAP];
    static final float[] ax = new float[CAP];
    static final float[] ay = new float[CAP];
    static final float[] az = new float[CAP];
    static volatile int n = 0;
    static volatile boolean rec = false;

    public static void main(String[] args) throws Exception {
        int secs = args.length > 0 ? Integer.parseInt(args[0]) : 10;
        String out = args.length > 1 ? args[1] : "/data/local/tmp/acc.csv";

        Looper.prepareMainLooper();
        Class<?> atc = Class.forName("android.app.ActivityThread");
        Method sysMain = atc.getMethod("systemMain");
        Object at = sysMain.invoke(null);
        Context ctx = (Context) atc.getMethod("getSystemContext").invoke(at);

        SensorManager sm = (SensorManager) ctx.getSystemService(Context.SENSOR_SERVICE);
        Sensor acc = sm.getDefaultSensor(Sensor.TYPE_ACCELEROMETER);
        if (acc == null) { System.out.println("NO_ACCELEROMETER"); System.exit(2); }

        System.out.println("SENSORINFO name=" + acc.getName()
                + " vendor=" + acc.getVendor()
                + " minDelayUs=" + acc.getMinDelay()
                + " declaredMaxRateHz=" + (acc.getMinDelay() > 0 ? 1e6 / acc.getMinDelay() : -1)
                + " resolution=" + acc.getResolution()
                + " maxRange=" + acc.getMaximumRange()
                + " fifoMax=" + acc.getFifoMaxEventCount());

        SensorEventListener lis = new SensorEventListener() {
            public void onSensorChanged(SensorEvent e) {
                if (!rec) return;
                int i = n;
                if (i >= CAP) return;
                ts[i] = e.timestamp;      // как есть
                ax[i] = e.values[0];
                ay[i] = e.values[1];
                az[i] = e.values[2];
                n = i + 1;
            }
            public void onAccuracyChanged(Sensor s, int a) { }
        };

        HandlerThread ht = new HandlerThread("sensor");
        ht.start();
        Handler h = new Handler(ht.getLooper());

        long wallStartMs = System.currentTimeMillis();
        rec = true;
        boolean ok = sm.registerListener(lis, acc, SensorManager.SENSOR_DELAY_FASTEST, 0, h);
        System.out.println("REGISTERED=" + ok + " secs=" + secs);
        Thread.sleep(secs * 1000L);
        rec = false;
        sm.unregisterListener(lis);
        Thread.sleep(200);

        int cnt = n;
        BufferedWriter w = new BufferedWriter(new FileWriter(out), 1 << 20);
        w.write("# sensor=" + acc.getName() + " vendor=" + acc.getVendor() + "\n");
        w.write("# minDelayUs=" + acc.getMinDelay()
                + " declaredMaxRateHz=" + (acc.getMinDelay() > 0 ? 1e6 / acc.getMinDelay() : -1) + "\n");
        w.write("# resolution=" + acc.getResolution()
                + " maxRange=" + acc.getMaximumRange()
                + " fifoMax=" + acc.getFifoMaxEventCount() + "\n");
        w.write("# samples=" + cnt + " requestedSecs=" + secs + " wallStartMs=" + wallStartMs + "\n");
        w.write("# t_ns = SensorEvent.timestamp RAW\n");
        w.write("t_ns,ax,ay,az\n");
        StringBuilder sb = new StringBuilder(1 << 16);
        for (int i = 0; i < cnt; i++) {
            sb.append(ts[i]).append(',').append(ax[i]).append(',')
              .append(ay[i]).append(',').append(az[i]).append('\n');
            if (sb.length() > (1 << 15)) { w.write(sb.toString()); sb.setLength(0); }
        }
        w.write(sb.toString());
        w.close();

        double span = cnt > 1 ? (ts[cnt - 1] - ts[0]) / 1e9 : 0;
        double hz = span > 0 ? (cnt - 1) / span : 0;
        System.out.println("CSV=" + out + " samples=" + cnt
                + String.format(" span=%.3fs effHz=%.2f", span, hz));
        System.exit(0);
    }
}
