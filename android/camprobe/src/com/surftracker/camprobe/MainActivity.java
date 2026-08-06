package com.surftracker.camprobe;

import android.app.Activity;
import android.graphics.ImageFormat;
import android.graphics.SurfaceTexture;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.params.MandatoryStreamCombination;
import android.hardware.camera2.params.OutputConfiguration;
import android.hardware.camera2.params.SessionConfiguration;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.ImageReader;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.util.Size;
import android.view.Surface;

import java.io.File;
import java.io.FileWriter;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executor;
import java.util.concurrent.TimeUnit;

/**
 * Диагностика камеры (тикет "телефон", п.0 и п.4а) — ТОЛЬКО измерения.
 *
 * Часть А не требует открытия камеры: уровень поддержки, размеры выходов,
 * stall duration, обязательные комбинации потоков — всё из
 * CameraCharacteristics.
 *
 * Часть Б требует открытой камеры: на Android 14 (API 34) проверка
 * isSessionConfigurationSupported живёт на CameraDevice, а не на
 * CameraDeviceSetup (тот появился в API 35). Поэтому камера открывается —
 * и именно поэтому нужно разрешение CAMERA, выдаваемое через adb.
 *
 * Результат — json, никакого чтения экрана.
 */
public class MainActivity extends Activity {
    static final String TAG = "CamProbe";
    HandlerThread bg;
    Handler handler;

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        bg = new HandlerThread("probe"); bg.start();
        handler = new Handler(bg.getLooper());
        new Thread(this::run).start();
    }

    void run() {
        File out = new File(getExternalFilesDir(null), "camera_probe.json");
        StringBuilder j = new StringBuilder("{\n");
        try {
            CameraManager cm = getSystemService(CameraManager.class);
            String[] ids = cm.getCameraIdList();
            j.append("  \"camera_ids\": ").append(jstr(Arrays.toString(ids))).append(",\n");
            j.append("  \"cameras\": {\n");
            for (int i = 0; i < ids.length; i++) {
                if (i > 0) j.append(",\n");
                j.append("    ").append(jstr(ids[i])).append(": ").append(describe(cm, ids[i]));
            }
            j.append("\n  }\n}\n");
        } catch (Throwable t) {
            Log.e(TAG, "ОШИБКА " + t, t);
            j.append("  \"error\": ").append(jstr(String.valueOf(t))).append("\n}\n");
        }
        try (FileWriter w = new FileWriter(out)) { w.write(j.toString()); } catch (Exception ignored) { }
        Log.i(TAG, "ГОТОВО " + out);
        finish();
    }

    String describe(CameraManager cm, String id) throws Exception {
        CameraCharacteristics ch = cm.getCameraCharacteristics(id);
        StringBuilder s = new StringBuilder("{\n");
        Integer level = ch.get(CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL);
        s.append("      \"hardware_level\": ").append(jstr(levelName(level))).append(",\n");
        Integer facing = ch.get(CameraCharacteristics.LENS_FACING);
        s.append("      \"facing\": ").append(facing).append(",\n");

        StreamConfigurationMap map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        s.append("      \"sizes\": {\n");
        int[] formats = {ImageFormat.YUV_420_888, ImageFormat.PRIVATE, ImageFormat.JPEG};
        String[] fnames = {"YUV_420_888", "PRIVATE", "JPEG"};
        for (int fi = 0; fi < formats.length; fi++) {
            Size[] sizes = map.getOutputSizes(formats[fi]);
            if (fi > 0) s.append(",\n");
            s.append("        ").append(jstr(fnames[fi])).append(": [");
            if (sizes != null) {
                for (int k = 0; k < sizes.length; k++) {
                    if (k > 0) s.append(", ");
                    s.append(jstr(sizes[k].toString()));
                }
            }
            s.append("]");
        }
        s.append("\n      },\n");

        // stall duration для МАКСИМАЛЬНОГО YUV — то, ради чего пункт и заведён:
        // ненулевой stall означает, что каждый такой кадр тормозит остальные потоки
        Size[] yuv = map.getOutputSizes(ImageFormat.YUV_420_888);
        if (yuv != null && yuv.length > 0) {
            Size max = yuv[0];
            for (Size z : yuv) if ((long) z.getWidth() * z.getHeight() > (long) max.getWidth() * max.getHeight()) max = z;
            s.append("      \"max_yuv\": ").append(jstr(max.toString())).append(",\n");
            s.append("      \"max_yuv_stall_ns\": ")
             .append(map.getOutputStallDuration(ImageFormat.YUV_420_888, max)).append(",\n");
            s.append("      \"max_yuv_min_frame_ns\": ")
             .append(map.getOutputMinFrameDuration(ImageFormat.YUV_420_888, max)).append(",\n");
        }

        MandatoryStreamCombination[] combos =
                ch.get(CameraCharacteristics.SCALER_MANDATORY_STREAM_COMBINATIONS);
        s.append("      \"mandatory_combinations\": [");
        if (combos != null) {
            for (int k = 0; k < combos.length; k++) {
                if (k > 0) s.append(", ");
                s.append(jstr(String.valueOf(combos[k].getDescription())));
            }
        }
        s.append("],\n");

        s.append("      \"candidates\": ").append(candidates(cm, id, map)).append("\n    }");
        return s.toString();
    }

    /** Часть Б: реальная проверка кандидатов на открытой камере. */
    String candidates(CameraManager cm, String id, StreamConfigurationMap map) {
        String[][] want = {
            {"PRIV 1080p + PRIV 4K + YUV 1080p", "PRIV:1920x1080", "PRIV:3840x2160", "YUV:1920x1080"},
            {"PRIV 1080p + PRIV 4K + YUV max",   "PRIV:1920x1080", "PRIV:3840x2160", "YUV:MAX"},
            {"PRIV 4K + YUV max (без превью)",   "PRIV:3840x2160", "YUV:MAX"},
            {"PRIV 1080p + YUV max (без записи)", "PRIV:1920x1080", "YUV:MAX"},
            {"PRIV 1080p + PRIV 4K + JPEG 4K",   "PRIV:1920x1080", "PRIV:3840x2160", "JPEG:3840x2160"},
        };
        StringBuilder s = new StringBuilder("[");
        CameraDevice dev = null;
        try {
            dev = open(cm, id);
            for (int i = 0; i < want.length; i++) {
                if (i > 0) s.append(",\n        ");
                else s.append("\n        ");
                String verdict;
                List<Surface> made = new ArrayList<>();
                try {
                    List<OutputConfiguration> cfgs = new ArrayList<>();
                    for (int k = 1; k < want[i].length; k++) {
                        Surface surf = makeSurface(want[i][k], map, made);
                        cfgs.add(new OutputConfiguration(surf));
                    }
                    Executor ex = r -> handler.post(r);
                    SessionConfiguration sc = new SessionConfiguration(
                            SessionConfiguration.SESSION_REGULAR, cfgs, ex,
                            new android.hardware.camera2.CameraCaptureSession.StateCallback() {
                                public void onConfigured(android.hardware.camera2.CameraCaptureSession s2) { }
                                public void onConfigureFailed(android.hardware.camera2.CameraCaptureSession s2) { }
                            });
                    boolean ok = dev.isSessionConfigurationSupported(sc);
                    verdict = ok ? "поддержано" : "НЕ поддержано";
                } catch (Throwable t) {
                    verdict = "ошибка: " + t;
                }
                s.append("{\"комбинация\": ").append(jstr(want[i][0]))
                 .append(", \"вердикт\": ").append(jstr(verdict)).append("}");
            }
        } catch (Throwable t) {
            s.append("\n        {\"error\": ").append(jstr(String.valueOf(t))).append("}");
        } finally {
            if (dev != null) dev.close();
        }
        return s.append("\n      ]").toString();
    }

    Surface makeSurface(String spec, StreamConfigurationMap map, List<Surface> keep) {
        String[] p = spec.split(":");
        Size size;
        if ("MAX".equals(p[1])) {
            Size[] yuv = map.getOutputSizes(ImageFormat.YUV_420_888);
            size = yuv[0];
            for (Size z : yuv) if ((long) z.getWidth() * z.getHeight() > (long) size.getWidth() * size.getHeight()) size = z;
        } else {
            String[] wh = p[1].split("x");
            size = new Size(Integer.parseInt(wh[0]), Integer.parseInt(wh[1]));
        }
        Surface surf;
        if ("PRIV".equals(p[0])) {
            SurfaceTexture st = new SurfaceTexture(0);
            st.setDefaultBufferSize(size.getWidth(), size.getHeight());
            surf = new Surface(st);
        } else {
            int fmt = "JPEG".equals(p[0]) ? ImageFormat.JPEG : ImageFormat.YUV_420_888;
            ImageReader ir = ImageReader.newInstance(size.getWidth(), size.getHeight(), fmt, 2);
            surf = ir.getSurface();
        }
        keep.add(surf);
        return surf;
    }

    CameraDevice open(CameraManager cm, String id) throws Exception {
        final CameraDevice[] box = new CameraDevice[1];
        final Throwable[] err = new Throwable[1];
        CountDownLatch latch = new CountDownLatch(1);
        cm.openCamera(id, new CameraDevice.StateCallback() {
            public void onOpened(CameraDevice d) { box[0] = d; latch.countDown(); }
            public void onDisconnected(CameraDevice d) { d.close(); latch.countDown(); }
            public void onError(CameraDevice d, int e) {
                err[0] = new RuntimeException("openCamera error " + e); d.close(); latch.countDown();
            }
        }, handler);
        if (!latch.await(8, TimeUnit.SECONDS)) throw new RuntimeException("таймаут открытия камеры");
        if (box[0] == null) throw (err[0] != null ? new RuntimeException(err[0]) : new RuntimeException("камера не открылась"));
        return box[0];
    }

    static String levelName(Integer l) {
        if (l == null) return "null";
        switch (l) {
            case CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LEGACY: return "LEGACY";
            case CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LIMITED: return "LIMITED";
            case CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_FULL: return "FULL";
            case CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_3: return "LEVEL_3";
            case CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_EXTERNAL: return "EXTERNAL";
            default: return "неизвестный(" + l + ")";
        }
    }

    static String jstr(String s) {
        return "\"" + (s == null ? "" : s.replace("\\", "\\\\").replace("\"", "\\\"")
                .replace("\n", " ")) + "\"";
    }
}
