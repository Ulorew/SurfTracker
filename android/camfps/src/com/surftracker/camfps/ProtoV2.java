package com.surftracker.camfps;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/**
 * Протокол v2 на стороне телефона: кадрирование и CRC.
 *
 * Спецификация: docs/ПРОТОКОЛ_ТЕЛЕФОН_STM32.md
 *
 * НИ ОДНОГО импорта из android.*, и это намеренно. Класс компилируется
 * обычным javac и гоняется на контрольных векторах на ноутбуке
 * (tools/link/java_vectors.sh). Будь протокол внутри активности, проверить его
 * можно было бы только на телефоне — то есть в самый неудобный момент и
 * вместе со всем остальным.
 *
 * Тот же приём, что на STM32: там proto_v2.h один на боевую прошивку и на
 * тест векторов. Копия реализации ради удобства теста проверяет копию.
 *
 * CRC-8/MAXIM: полином 0x31 ОТРАЖЁННЫЙ (0x8C при счёте с младшего бита).
 * В v1 был 0x07 без отражения — другой CRC. Перепутать легко: кадр соберётся,
 * уедет и будет молча отвергнут, а выглядеть это будет как мёртвая линия.
 */
public final class ProtoV2 {
    private ProtoV2() { }

    public static final byte MAGIC_REQ = (byte) 0xA5;
    public static final byte MAGIC_TEL = (byte) 0x5A;
    public static final int  REQ_LEN = 11, TEL_LEN = 12;
    public static final int  VERSION = 0;

    public static final int ST_WATCHDOG   = 1;
    public static final int ST_EXTRAP_CAP = 1 << 1;
    public static final int ST_RAMP_SAT   = 1 << 2;
    public static final int ST_ENC_OK     = 1 << 3;
    public static final int ST_CLAMP      = 1 << 4;
    public static final int ST_SLIP       = 1 << 5;
    public static final int ST_CRC_SHIFT  = 6;

    public static byte crc8(byte[] d, int n) {
        int c = 0;
        for (int i = 0; i < n; i++) {
            c ^= d[i] & 0xFF;
            for (int b = 0; b < 8; b++)
                c = ((c & 1) != 0) ? ((c >> 1) ^ 0x8C) & 0xFF : (c >> 1) & 0xFF;
        }
        return (byte) c;
    }

    /** Кадр уставки. seq семибитный: старший бит занят версией. */
    public static void buildReq(byte[] out, int seq, float w, float wdot) {
        out[0] = MAGIC_REQ;
        out[1] = (byte) (((VERSION & 1) << 7) | (seq & 0x7F));
        ByteBuffer.wrap(out, 2, 8).order(ByteOrder.LITTLE_ENDIAN)
                  .putFloat(w).putFloat(wdot);
        out[REQ_LEN - 1] = crc8(out, REQ_LEN - 1);
    }

    /** Разобранная телеметрия. */
    public static final class Tel {
        public int seq, status;
        public float theta, wRamp;
    }

    /** -> null, если магик или CRC не сошлись. */
    public static Tel parseTel(byte[] f, int off) {
        if (f[off] != MAGIC_TEL) return null;
        byte[] c = java.util.Arrays.copyOfRange(f, off, off + TEL_LEN);
        if (crc8(c, TEL_LEN - 1) != c[TEL_LEN - 1]) return null;
        Tel t = new Tel();
        t.seq = c[1] & 0x7F;
        ByteBuffer bb = ByteBuffer.wrap(c, 2, 8).order(ByteOrder.LITTLE_ENDIAN);
        t.theta = bb.getFloat();
        t.wRamp = bb.getFloat();
        t.status = c[10] & 0xFF;
        return t;
    }

    public static boolean bit(int status, int mask) { return (status & mask) != 0; }
}
