package com.surftracker.camfps;

import android.app.Activity;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothSocket;
import android.os.Bundle;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.OutputStreamWriter;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.UUID;

/**
 * Мост телефон ↔ STM32 поверх Bluetooth SPP, протокол v2.
 *
 * Спецификация: docs/ПРОТОКОЛ.md. Протокол байтовый и про
 * транспорт ничего не знает — переход с USB на BT не потребовал в нём ни одной
 * правки, и смена платы с F411 на G431 тоже.
 *
 * ЧТО ИЗМЕНИЛОСЬ ПРОТИВ v1, кроме длин кадров:
 *
 *   - CRC ДРУГОЙ. v1 — 0x07 без отражения, v2 — 0x31 отражённый (0x8C при
 *     счёте с младшего бита). Реализации внешне похожи, и перепутать их легко:
 *     кадр соберётся, уедет и будет молча отвергнут приёмником. Контрольное
 *     значение CRC("123456789") = 0xA1 проверяется здесь же на старте, до
 *     первого кадра, — дешевле, чем искать причину тишины в эфире.
 *   - МАГИКИ РАЗНЫЕ у запроса и телеметрии: 0xA5 против 0x5A. Ресинхронизация
 *     приёмника ищет 0x5A. Байты зеркальные, и подставить один вместо другого
 *     не заметив — вопрос одной опечатки.
 *   - seq СЕМИБИТНЫЙ, старший бит кадра уставки занят версией. Маска 0x7F
 *     обязательна и при отправке, и при сопоставлении ответа.
 *
 * СОПОСТАВЛЕНИЕ ПО seq, А НЕ ОЖИДАНИЕ В ТАКТЕ. Ответ по BT приходит в среднем
 * через целый период, поэтому ждать его в такте бессмысленно. Просроченные
 * ответы ВЫБРАСЫВАЮТСЯ: оставленный в буфере, такой ответ читается следующим
 * тактом, и каждое опоздание навсегда сдвигает очередь. На ноутбуке этот
 * дефект дал отставание в 97 кадров за 120 секунд и выглядел как отказ
 * транспорта.
 *
 * РЕЖИМЫ:
 *   zero — уставка ноль. Мерится ЛИНИЯ, и результат не зависит от того, что
 *          делает мотор.
 *   sine — уставка синус (ступень 3 тикета: проверка контура без зрения).
 *          ω̇ считается аналитически, а не разностью: разность по шумному
 *          времени телефона дала бы дребезг в поле, которым приёмник
 *          экстраполирует, и мы мерили бы свой же шум.
 *
 *   am start -n com.surftracker.camfps/.BtLinkActivity \
 *       --es mac 38:18:2B:30:7D:86 --ei hz 10 --ei seconds 60 \
 *       [--es tag bt1] [--es mode sine] [--ef amp 0.3] [--ef period 8]
 */
public class BtLinkActivity extends Activity {
    static final String TAG = "btlink";
    static final UUID SPP = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB");

    // Кадрирование и CRC живут в ProtoV2 — ОДНА реализация, та же, что
    // проверяется векторами на ноутбуке (tools/link/java_vectors.sh). Копия
    // здесь проверялась бы тестом копии.
    static final int TEL_LEN = ProtoV2.TEL_LEN;
    static final int REQ_LEN = ProtoV2.REQ_LEN;

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        runOnUiThread(() -> {
            android.widget.TextView tv = new android.widget.TextView(this);
            tv.setText("мост BT v2");
            setContentView(tv);
            getWindow().addFlags(
                    android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                    | android.view.WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                    | android.view.WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED);
        });
        String[] need = {"android.permission.BLUETOOTH_CONNECT",
                          "android.permission.BLUETOOTH_SCAN"};
        boolean ok = true;
        for (String p : need)
            if (checkSelfPermission(p) != android.content.pm.PackageManager.PERMISSION_GRANTED)
                ok = false;
        if (!ok) requestPermissions(need, 7);
        else new Thread(this::run).start();
    }

    @Override public void onRequestPermissionsResult(int c, String[] p, int[] g) {
        new Thread(this::run).start();
    }

    void run() {
        // Частичный wake lock: держит ПРОЦЕССОР, а не экран.
        //
        // Без него прогон умирает молча. Телефон уходит в дозу, система
        // объявляет приложение фоновым и замораживает поток — в логе не
        // остаётся ни строки, даже ошибки, потому что finally не выполняется
        // тоже. Выглядит это как повисший Bluetooth, и я потратил на такую
        // диагностику два прогона.
        //
        // FLAG_KEEP_SCREEN_ON тут бессилен: он действует, только если экран
        // уже горит. `adb shell svc power stayon true` тоже — он держит экран
        // лишь при подключённом зарядном.
        android.os.PowerManager.WakeLock wl = null;
        try {
            android.os.PowerManager pm =
                (android.os.PowerManager) getSystemService(POWER_SERVICE);
            wl = pm.newWakeLock(android.os.PowerManager.PARTIAL_WAKE_LOCK,
                                 "surftracker:btlink");
            wl.acquire(20 * 60 * 1000L);   // потолок на случай, если release не дойдёт
        } catch (Throwable t) {
            Log.e(TAG, "wake lock: " + t);
        }

        String tag = getIntent().getStringExtra("tag");
        if (tag == null) tag = "btlink";
        String mac = getIntent().getStringExtra("mac");
        String mode = getIntent().getStringExtra("mode");
        if (mode == null) mode = "zero";
        float amp = getIntent().getFloatExtra("amp", 0.3f);
        float per = getIntent().getFloatExtra("period", 8.0f);
        int hz = getIntent().getIntExtra("hz", 10);
        int seconds = getIntent().getIntExtra("seconds", 60);
        File dir = new File(getExternalFilesDir(null), "link");
        dir.mkdirs();
        File base = new File(dir, tag);
        StringBuilder j = new StringBuilder("{");
        BluetoothSocket sock = null;

        // Поля лога по §3 спецификации, плюс разобранные биты статуса
        // отдельными колонками: читать лог глазами по числу 0x2A невозможно,
        // а именно глазами его и читают, когда что-то пошло не так.
        StringBuilder csv = new StringBuilder(
            "i,seq,t_отпр_ns,t_приёма_ns,rtt_ms,θ_enc,ω_ramp,статус,"
            + "watchdog,потолок,рампа,энкодер,кламп,срыв,crc_счёт\n");

        try {
            // Самопроверка CRC ДО эфира: перепутанный полином даёт кадры,
            // которые уходят и молча отвергаются, а выглядит это как мёртвая
            // линия. Отличить одно от другого в эфире дороже, чем проверить
            // здесь.
            byte[] chk = "123456789".getBytes("US-ASCII");
            int got = ProtoV2.crc8(chk, chk.length) & 0xFF;
            if (got != 0xA1)
                throw new RuntimeException("CRC не тот: ожидалось 0xA1, вышло 0x"
                                            + Integer.toHexString(got));

            BluetoothAdapter ad = BluetoothAdapter.getDefaultAdapter();
            if (ad == null || !ad.isEnabled()) throw new RuntimeException("Bluetooth выключен");
            BluetoothDevice dev = null;
            if (mac != null) dev = ad.getRemoteDevice(mac);
            else for (BluetoothDevice d : ad.getBondedDevices())
                    if ("SurfTracker-Link".equals(d.getName())) dev = d;
            if (dev == null) throw new RuntimeException("устройство не найдено");

            sock = dev.createInsecureRfcommSocketToServiceRecord(SPP);
            ad.cancelDiscovery();
            sock.connect();
            OutputStream os = sock.getOutputStream();
            InputStream is = sock.getInputStream();

            j.append("\"tag\":\"").append(tag).append("\",\"версия_протокола\":2")
             .append(",\"mac\":\"").append(dev.getAddress()).append("\",\"hz\":").append(hz)
             .append(",\"seconds\":").append(seconds).append(",\"режим\":\"").append(mode).append("\"");
            if ("sine".equals(mode))
                j.append(",\"амплитуда\":").append(fmt(amp)).append(",\"период_с\":").append(fmt(per));

            final long period = 1_000_000_000L / hz;
            int n = hz * seconds;
            // 128, а не 256: seq семибитный. Массив на 256 работал бы, но
            // половина его никогда не заполнялась бы, и это сбивало бы с толку
            // при чтении кода.
            long[] sendNs = new long[128];
            java.util.Arrays.fill(sendNs, 0L);
            byte[] req = new byte[REQ_LEN];
            byte[] rx = new byte[4096];
            int rxn = 0;
            List<Double> rtts = new ArrayList<>();
            List<Long> recvNs = new ArrayList<>();
            int okN = 0, stale = 0, badCrc = 0;
            int cWd = 0, cCap = 0, cRamp = 0, cEnc = 0, cClamp = 0, cSlip = 0;
            long t0 = System.nanoTime();
            long next = t0;

            for (int i = 0; i < n; i++) {
                long now = System.nanoTime();
                if (next > now) Thread.sleep((next - now) / 1_000_000L,
                                              (int) ((next - now) % 1_000_000L));
                int seq = i & 0x7F;

                float w = 0.0f, wdot = 0.0f;
                if ("sine".equals(mode)) {
                    double t = (System.nanoTime() - t0) / 1e9;
                    double k = 2 * Math.PI / per;
                    w    = (float) (amp * Math.sin(k * t));
                    wdot = (float) (amp * k * Math.cos(k * t));
                }
                ProtoV2.buildReq(req, seq, w, wdot);

                long ts = System.nanoTime();
                sendNs[seq] = ts;
                os.write(req);
                os.flush();

                // Разбираем ВСЁ, что пришло, не ожидая ответа именно на этот
                // кадр: по BT он придёт позже, и ждать его в такте значит
                // мерить не линию, а собственное терпение.
                long deadline = ts + period - 2_000_000L;
                while (System.nanoTime() < deadline) {
                    int av = is.available();
                    if (av <= 0) { Thread.sleep(1); continue; }
                    int g = is.read(rx, rxn, Math.min(av, rx.length - rxn));
                    if (g > 0) rxn += g;
                    int p = 0;
                    while (rxn - p >= TEL_LEN) {
                        ProtoV2.Tel tel = ProtoV2.parseTel(rx, p);
                        if (tel == null) {
                            // Магик не тот или CRC не сошлось — сдвигаемся на
                            // байт и ищем дальше. Выравнивание по ДЛИНЕ здесь
                            // залипло бы навсегда после потери одного байта.
                            if (rx[p] == ProtoV2.MAGIC_TEL) badCrc++;
                            p++;
                            continue;
                        }
                        long st = sendNs[tel.seq];
                        long tgot = System.nanoTime();
                        if (st == 0) { stale++; p += TEL_LEN; continue; }

                        double rtt = (tgot - st) / 1e6;
                        rtts.add(rtt);
                        recvNs.add(tgot);
                        sendNs[tel.seq] = 0;
                        okN++;

                        int stt = tel.status;
                        if ((stt & ProtoV2.ST_WATCHDOG)   != 0) cWd++;
                        if ((stt & ProtoV2.ST_EXTRAP_CAP) != 0) cCap++;
                        if ((stt & ProtoV2.ST_RAMP_SAT)   != 0) cRamp++;
                        if ((stt & ProtoV2.ST_ENC_OK)     != 0) cEnc++;
                        if ((stt & ProtoV2.ST_CLAMP)      != 0) cClamp++;
                        if ((stt & ProtoV2.ST_SLIP)       != 0) cSlip++;

                        csv.append(i).append(',').append(tel.seq).append(',')
                           .append(st).append(',').append(tgot).append(',')
                           .append(fmt(rtt)).append(',')
                           .append(fmt(tel.theta)).append(',').append(fmt(tel.wRamp)).append(',')
                           .append(stt).append(',')
                           .append(bit(stt, ProtoV2.ST_WATCHDOG)).append(',')
                           .append(bit(stt, ProtoV2.ST_EXTRAP_CAP)).append(',')
                           .append(bit(stt, ProtoV2.ST_RAMP_SAT)).append(',')
                           .append(bit(stt, ProtoV2.ST_ENC_OK)).append(',')
                           .append(bit(stt, ProtoV2.ST_CLAMP)).append(',')
                           .append(bit(stt, ProtoV2.ST_SLIP)).append(',')
                           .append((stt >> ProtoV2.ST_CRC_SHIFT) & 0x03).append('\n');
                        p += TEL_LEN;
                    }
                    if (p > 0) {
                        System.arraycopy(rx, p, rx, 0, rxn - p);
                        rxn -= p;
                    }
                }
                next += period;
            }

            double[] r = new double[rtts.size()];
            for (int i = 0; i < r.length; i++) r[i] = rtts.get(i);
            java.util.Arrays.sort(r);
            j.append(",\"sent\":").append(n).append(",\"ok\":").append(okN)
             .append(",\"lost\":").append(n - okN).append(",\"stale\":").append(stale)
             .append(",\"bad_crc\":").append(badCrc);
            j.append(",\"биты\":{\"watchdog\":").append(cWd)
             .append(",\"потолок\":").append(cCap)
             .append(",\"рампа\":").append(cRamp)
             .append(",\"энкодер\":").append(cEnc)
             .append(",\"кламп\":").append(cClamp)
             .append(",\"срыв\":").append(cSlip).append("}");
            if (r.length > 0)
                j.append(",\"rtt_ms\":{\"p50\":").append(fmt(r[r.length / 2]))
                 .append(",\"p95\":").append(fmt(r[(int) (0.95 * (r.length - 1))]))
                 .append(",\"max\":").append(fmt(r[r.length - 1])).append("}");
            Collections.sort(recvNs);
            if (recvNs.size() > 1) {
                double[] pe = new double[recvNs.size() - 1];
                for (int i = 1; i < recvNs.size(); i++)
                    pe[i - 1] = (recvNs.get(i) - recvNs.get(i - 1)) / 1e6;
                java.util.Arrays.sort(pe);
                j.append(",\"период_приёма_ms\":{\"p50\":").append(fmt(pe[pe.length / 2]))
                 .append(",\"p95\":").append(fmt(pe[(int) (0.95 * (pe.length - 1))]))
                 .append(",\"max\":").append(fmt(pe[pe.length - 1])).append("}");
            }
            j.append(",\"ok_flag\":true");
        } catch (Throwable t) {
            Log.e(TAG, "мост: " + t);
            j.append(",\"ok_flag\":false,\"error\":\"")
             .append(String.valueOf(t).replace('"', '\'')).append("\"");
        } finally {
            try { if (sock != null) sock.close(); } catch (Throwable ignored) {}
            try { if (wl != null && wl.isHeld()) wl.release(); } catch (Throwable ignored) {}
            try {
                write(new File(base.getPath() + ".json"), j.append("}").toString());
                write(new File(base.getPath() + ".csv"), csv.toString());
            } catch (Throwable ignored) {}
            Log.i(TAG, "ГОТОВО " + base.getPath());
            finish();
        }
    }

    static int bit(int status, int mask) { return (status & mask) != 0 ? 1 : 0; }

    static String fmt(double v) {
        return String.format(java.util.Locale.US, "%.3f", v);
    }

    static void write(File f, String s) throws Exception {
        try (OutputStreamWriter w = new OutputStreamWriter(new FileOutputStream(f), "UTF-8")) {
            w.write(s);
        }
    }
}
