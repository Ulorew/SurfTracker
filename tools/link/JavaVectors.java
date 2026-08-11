import com.surftracker.camfps.ProtoV2;

/**
 * Контрольные векторы протокола v2 для реализации НА ТЕЛЕФОНЕ.
 *
 * Те же самые векторы, что гоняет stm/proto_test на целевом железе и tools/link
 * на хосте. Смысл именно в том, что они одни и те же: три независимые
 * реализации (C++ на MCU, Python на хосте, Java на телефоне) обязаны выдать
 * байт в байт одно и то же, иначе расхождение всплывёт в эфире, где его
 * отлаживать дороже всего.
 *
 * Гоняется на ноутбуке, а не на телефоне: ProtoV2 намеренно не зависит от
 * android.*, и ловить ошибку кадрирования на устройстве незачем.
 *
 *   tools/link/java_vectors.sh
 */
public class JavaVectors {

    static int fails = 0;

    static String hex(byte[] b, int n) {
        StringBuilder s = new StringBuilder();
        for (int i = 0; i < n; i++) {
            if (i > 0) s.append(' ');
            s.append(String.format("%02X", b[i] & 0xFF));
        }
        return s.toString();
    }

    static byte[] parseHex(String s) {
        String[] p = s.trim().split("\\s+");
        byte[] b = new byte[p.length];
        for (int i = 0; i < p.length; i++) b[i] = (byte) Integer.parseInt(p[i], 16);
        return b;
    }

    static void check(String name, String expected, byte[] got, int n) {
        String g = hex(got, n);
        boolean ok = g.equals(expected);
        if (!ok) fails++;
        System.out.printf("%-4s %-22s %s%n", ok ? "ok" : "СБОЙ", name, g);
        if (!ok) System.out.printf("     ожидалось          %s%n", expected);
    }

    public static void main(String[] a) {
        System.out.println("=== векторы протокола v2, реализация телефона (Java) ===");

        // 1. Контрольное значение CRC. Стандартная проверка для CRC-8/MAXIM.
        byte[] chk = "123456789".getBytes(java.nio.charset.StandardCharsets.US_ASCII);
        int c = ProtoV2.crc8(chk, chk.length) & 0xFF;
        boolean ok = (c == 0xA1);
        if (!ok) fails++;
        System.out.printf("%-4s CRC(\"123456789\")=0x%02X%n", ok ? "ok" : "СБОЙ", c);

        // 2. Кадры уставки — те же три, что в спецификации и в proto_test.
        byte[] out = new byte[ProtoV2.REQ_LEN];
        ProtoV2.buildReq(out, 0, 0.0f, 0.0f);
        check("уставка нулевая", "A5 00 00 00 00 00 00 00 00 00 17", out, ProtoV2.REQ_LEN);
        ProtoV2.buildReq(out, 1, 1.0f, 0.1f);
        check("уставка w=1 wd=0.1", "A5 01 00 00 80 3F CD CC CC 3D 44", out, ProtoV2.REQ_LEN);
        ProtoV2.buildReq(out, 127, -2.0f, 0.5f);
        check("уставка w=-2 wd=0.5", "A5 7F 00 00 00 C0 00 00 00 3F CD", out, ProtoV2.REQ_LEN);

        // 3. Разбор телеметрии — те же три кадра, но с другой стороны: на STM
        //    они строятся, здесь разбираются. Проверяется и CRC, и раскладка
        //    полей, а не только контрольная сумма.
        checkTel("телеметрия штатная", "5A 00 00 00 00 00 00 00 00 00 08 D5",
                  0, 0.0f, 0.0f, 0x08);
        checkTel("телеметрия статусная", "5A 2A C1 B8 32 3E 00 00 00 3F 2A 43",
                  42, 0.1745f, 0.5f, 0x2A);
        checkTel("телеметрия watchdog", "5A 7F 00 00 80 BF 00 00 00 00 19 21",
                  127, -1.0f, 0.0f, 0x19);

        // 4. Порченый кадр обязан быть отвергнут, а не разобран как попало.
        byte[] bad = parseHex("5A 00 00 00 00 00 00 00 00 00 08 D6");
        boolean rejected = (ProtoV2.parseTel(bad, 0) == null);
        if (!rejected) fails++;
        System.out.printf("%-4s порченый кадр отвергнут%n", rejected ? "ok" : "СБОЙ");

        // 5. Чужой магик — тоже отказ. 0xA5 и 0x5A зеркальны, и подставить один
        //    вместо другого можно опечаткой.
        byte[] wrongMagic = parseHex("A5 00 00 00 00 00 00 00 00 00 08 D5");
        boolean rejMagic = (ProtoV2.parseTel(wrongMagic, 0) == null);
        if (!rejMagic) fails++;
        System.out.printf("%-4s чужой магик отвергнут%n", rejMagic ? "ok" : "СБОЙ");

        System.out.println();
        System.out.println(fails == 0 ? "ИТОГ: ВСЕ ВЕКТОРЫ СОШЛИСЬ"
                                       : "ИТОГ: СБОЕВ " + fails);
        System.exit(fails == 0 ? 0 : 1);
    }

    static void checkTel(String name, String frameHex, int seq,
                          float theta, float wRamp, int status) {
        ProtoV2.Tel t = ProtoV2.parseTel(parseHex(frameHex), 0);
        StringBuilder why = new StringBuilder();
        if (t == null) why.append("не разобрался; ");
        else {
            if (t.seq != seq) why.append("seq ").append(t.seq).append("!=").append(seq).append("; ");
            if (Math.abs(t.theta - theta) > 1e-3f)
                why.append("theta ").append(t.theta).append("!=").append(theta).append("; ");
            if (Math.abs(t.wRamp - wRamp) > 1e-3f)
                why.append("w_ramp ").append(t.wRamp).append("!=").append(wRamp).append("; ");
            if (t.status != status)
                why.append("статус ").append(t.status).append("!=").append(status).append("; ");
        }
        boolean ok = why.length() == 0;
        if (!ok) fails++;
        System.out.printf("%-4s %-22s%s%n", ok ? "ok" : "СБОЙ", name, ok ? "" : "  " + why);
    }
}
