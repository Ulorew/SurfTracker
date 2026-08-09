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
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.UUID;

/**
 * Мост телефон ↔ STM32 поверх Bluetooth SPP (тикет «замыкание контура»,
 * ступень 2) и он же измеритель линии.
 *
 * Протокол — тот же, что по проводу: docs/ПРОТОКОЛ_ТЕЛЕФОН_STM32.md. Он
 * байтовый и про транспорт ничего не знает, поэтому переход с USB на BT не
 * потребовал в нём ни одной правки.
 *
 * ЗАЧЕМ ОТДЕЛЬНЫЙ ЗАМЕР С ТЕЛЕФОНА. Цена Bluetooth уже измерена ноутбуком
 * (RTT p50 39.6 мс, p95 59.8), но хостом в бою будет телефон, и стек у него
 * свой. Переносить чужие числа на другую машину — ровно то, чего в этом
 * проекте стараются не делать.
 *
 * СОПОСТАВЛЕНИЕ ПО seq, А НЕ ОЖИДАНИЕ В ТАКТЕ. Ответ по BT приходит в
 * среднем через целый период, поэтому ждать его в такте бессмысленно.
 * Просроченные ответы ВЫБРАСЫВАЮТСЯ: оставленный в буфере, такой ответ
 * читается следующим тактом, и каждое опоздание навсегда сдвигает очередь.
 * На ноутбуке этот дефект дал отставание в 97 кадров за 120 секунд и выглядел
 * как отказ транспорта.
 *
 *   am start -n com.surftracker.camfps/.BtLinkActivity \
 *       --es mac 38:18:2B:30:7D:86 --ei hz 10 --ei seconds 60 [--es tag bt1]
 */
public class BtLinkActivity extends Activity {
    static final String TAG = "btlink";
    static final UUID SPP = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB");
    static final int REQ_LEN = 7, RESP_LEN = 8;
    static final byte MAGIC = (byte) 0xA5;

    static byte crc8(byte[] d, int n) {
        int c = 0;
        for (int i = 0; i < n; i++) {
            c ^= d[i] & 0xFF;
            for (int b = 0; b < 8; b++)
                c = ((c & 0x80) != 0) ? ((c << 1) ^ 0x07) & 0xFF : (c << 1) & 0xFF;
        }
        return (byte) c;
    }

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        runOnUiThread(() -> {
            android.widget.TextView tv = new android.widget.TextView(this);
            tv.setText("мост BT");
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
        String tag = getIntent().getStringExtra("tag");
        if (tag == null) tag = "btlink";
        String mac = getIntent().getStringExtra("mac");
        int hz = getIntent().getIntExtra("hz", 10);
        int seconds = getIntent().getIntExtra("seconds", 60);
        File dir = new File(getExternalFilesDir(null), "link");
        dir.mkdirs();
        File base = new File(dir, tag);
        StringBuilder j = new StringBuilder("{");
        BluetoothSocket sock = null;
        StringBuilder csv = new StringBuilder("i,seq,t_send_ns,rtt_ms,theta,status,ok\n");
        try {
            BluetoothAdapter ad = BluetoothAdapter.getDefaultAdapter();
            if (ad == null || !ad.isEnabled()) throw new RuntimeException("Bluetooth выключен");
            BluetoothDevice dev = null;
            if (mac != null) dev = ad.getRemoteDevice(mac);
            else for (BluetoothDevice d : ad.getBondedDevices())
                    if ("SurfTracker-Link".equals(d.getName())) dev = d;
            if (dev == null) throw new RuntimeException("устройство не найдено");

            // Небезопасный сокет: у заглушки нет ни PIN, ни шифрования, и
            // требовать сопряжения ради замера линии незачем.
            sock = dev.createInsecureRfcommSocketToServiceRecord(SPP);
            ad.cancelDiscovery();
            sock.connect();
            OutputStream os = sock.getOutputStream();
            InputStream is = sock.getInputStream();

            j.append("\"tag\":\"").append(tag).append("\",\"mac\":\"")
             .append(dev.getAddress()).append("\",\"hz\":").append(hz)
             .append(",\"seconds\":").append(seconds);

            final long period = 1_000_000_000L / hz;
            int n = hz * seconds;
            long[] sendNs = new long[256];
            java.util.Arrays.fill(sendNs, 0L);
            byte[] req = new byte[REQ_LEN];
            byte[] rx = new byte[4096];
            int rxn = 0;
            List<Double> rtts = new ArrayList<>();
            List<Long> recvNs = new ArrayList<>();
            int okN = 0, stale = 0, badCrc = 0;
            long t0 = System.nanoTime();
            long next = t0;

            for (int i = 0; i < n; i++) {
                long now = System.nanoTime();
                if (next > now) Thread.sleep((next - now) / 1_000_000L,
                                              (int) ((next - now) % 1_000_000L));
                int seq = i & 0xFF;
                req[0] = MAGIC; req[1] = (byte) seq;
                // omega = 0: замер линии не должен зависеть от того, что делает мотор
                req[2] = req[3] = req[4] = req[5] = 0;
                req[6] = crc8(req, REQ_LEN - 1);
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
                    int got = is.read(rx, rxn, Math.min(av, rx.length - rxn));
                    if (got > 0) rxn += got;
                    int p = 0;
                    while (rxn - p >= RESP_LEN) {
                        if (rx[p] != MAGIC) { p++; continue; }
                        byte[] f = java.util.Arrays.copyOfRange(rx, p, p + RESP_LEN);
                        if (crc8(f, RESP_LEN - 1) != f[RESP_LEN - 1]) { p++; badCrc++; continue; }
                        int rseq = f[1] & 0xFF;
                        long st = sendNs[rseq];
                        long tgot = System.nanoTime();
                        if (st != 0) {
                            double rtt = (tgot - st) / 1e6;
                            rtts.add(rtt);
                            recvNs.add(tgot);
                            sendNs[rseq] = 0;
                            okN++;
                            float th = java.nio.ByteBuffer.wrap(f, 2, 4)
                                    .order(java.nio.ByteOrder.LITTLE_ENDIAN).getFloat();
                            csv.append(i).append(',').append(rseq).append(',').append(st)
                               .append(',').append(String.format(java.util.Locale.US, "%.3f", rtt))
                               .append(',').append(th).append(',').append(f[6] & 0xFF)
                               .append(",1\n");
                        } else {
                            stale++;      // ответ на кадр, который уже закрыт
                        }
                        p += RESP_LEN;
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
            if (r.length > 0)
                j.append(",\"rtt_ms\":{\"p50\":").append(fmt(r[r.length / 2]))
                 .append(",\"p95\":").append(fmt(r[(int) (0.95 * (r.length - 1))]))
                 .append(",\"max\":").append(fmt(r[r.length - 1])).append("}");
            Collections.sort(recvNs);
            if (recvNs.size() > 1) {
                double[] per = new double[recvNs.size() - 1];
                for (int i = 1; i < recvNs.size(); i++)
                    per[i - 1] = (recvNs.get(i) - recvNs.get(i - 1)) / 1e6;
                java.util.Arrays.sort(per);
                j.append(",\"период_приёма_ms\":{\"p50\":").append(fmt(per[per.length / 2]))
                 .append(",\"p95\":").append(fmt(per[(int) (0.95 * (per.length - 1))]))
                 .append(",\"max\":").append(fmt(per[per.length - 1])).append("}");
            }
            j.append(",\"ok_flag\":true");
        } catch (Throwable t) {
            Log.e(TAG, "мост: " + t);
            j.append(",\"ok_flag\":false,\"error\":\"")
             .append(String.valueOf(t).replace('"', '\'')).append("\"");
        } finally {
            try { if (sock != null) sock.close(); } catch (Throwable ignored) {}
            try {
                write(new File(base.getPath() + ".json"), j.append("}").toString());
                write(new File(base.getPath() + ".csv"), csv.toString());
            } catch (Throwable ignored) {}
            Log.i(TAG, "ГОТОВО " + base.getPath());
            finish();
        }
    }

    static String fmt(double v) {
        return String.format(java.util.Locale.US, "%.3f", v);
    }

    static void write(File f, String s) throws Exception {
        try (OutputStreamWriter w = new OutputStreamWriter(new FileOutputStream(f))) {
            w.write(s);
        }
    }
}
