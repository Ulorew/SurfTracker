package com.surftracker.camfps;

import java.io.File;
import java.io.FileInputStream;

/**
 * Чтение прогон.json — БЕЗ единой зависимости от Android.
 *
 * Вынесено из активности не ради чистоты: активность нельзя запустить на
 * ноутбуке, а значит, разбор внутри неё нельзя и проверить. Ровно этой
 * причиной уже был испорчен перенос трекера — логика такта жила в активности,
 * и стенд сличения был вынужден повторять её у себя. Здесь то же лекарство,
 * применённое заранее.
 *
 * Разбор по подстроке, а не полноценный парсер. Оправдано тем, что json пишет
 * тот же проект в известном формате, а тянуть библиотеку ради десятка чисел в
 * приложение без единой зависимости — хуже. Ключ ищется ВМЕСТЕ С КАВЫЧКАМИ,
 * поэтому «окно» не совпадает с «окно_медиана», а «p50» — с содержимым чужой
 * строки.
 *
 * ГЛАВНАЯ ЛОВУШКА, о которую разбор уже спотыкался: ключ берётся ПЕРВЫЙ по
 * тексту. В прогон.json есть «секунд» (заказанная длительность, пишется в
 * начале) и фактическая длительность в итоге; пока фактическая называлась
 * так же, карточка показывала заказ вместо результата. Поэтому имена в итоге
 * не должны повторять имена настроек, и на это есть проверка в стенде.
 */
public final class RunJson {

    private RunJson() {}

    public static String read(File f) {
        try (FileInputStream in = new FileInputStream(f)) {
            byte[] b = new byte[(int) f.length()];
            int n = in.read(b);
            return new String(b, 0, Math.max(n, 0), "UTF-8");
        } catch (Throwable t) { return null; }
    }

    public static double num(String json, String key) {
        String v = raw(json, key);
        if (v == null) return 0;
        int e = 0;
        while (e < v.length()) {
            char c = v.charAt(e);
            if (Character.isDigit(c) || c == '-' || c == '+' || c == '.'
                    || c == 'e' || c == 'E') e++;
            else break;
        }
        if (e == 0) return 0;
        try { return Double.parseDouble(v.substring(0, e)); }
        catch (Throwable t) { return 0; }
    }

    public static boolean bool(String json, String key) {
        String v = raw(json, key);
        return v != null && v.startsWith("true");
    }

    public static String str(String json, String key) {
        String v = raw(json, key);
        if (v == null || !v.startsWith("\"")) return null;
        int e = v.indexOf('"', 1);
        return e < 0 ? null : v.substring(1, e);
    }

    /** Есть ли ключ вообще. Отличает «нет ключа» от «значение равно нулю». */
    public static boolean has(String json, String key) {
        return raw(json, key) != null;
    }

    private static String raw(String json, String key) {
        if (json == null) return null;
        int i = json.indexOf('"' + key + '"');
        if (i < 0) return null;
        int c = json.indexOf(':', i);
        if (c < 0) return null;
        int s = c + 1;
        while (s < json.length() && json.charAt(s) == ' ') s++;
        return json.substring(s);
    }

    public static String mmss(double sec) {
        int t = (int) Math.round(sec);
        return String.format(java.util.Locale.US, "%d:%02d", t / 60, t % 60);
    }

    public static String fmt1(double v) {
        return String.format(java.util.Locale.US, "%.1f", v);
    }

    /**
     * Сводка для карточки прогона. Чистая функция от текста json — её и
     * проверяет стенд.
     *
     * @param hasVideo лежит ли рядом видео (файловая система — не дело разбора)
     */
    public static String summary(String json, boolean hasVideo) {
        if (json == null) return "прогон.json не читается";
        double ticks = num(json, "тактов");
        if (ticks <= 0) {
            String err = str(json, "ошибка");
            return "ПРОГОН НЕ СОСТОЯЛСЯ — тактов нет" + (err != null ? "\n" + err : "");
        }
        StringBuilder s = new StringBuilder();
        s.append("на цели ").append(Math.round(num(json, "доля_на_цели") * 100)).append('%')
         .append("   потерь ").append((int) num(json, "потерь"));
        double reacq = num(json, "повторных_захватов");
        if (reacq > 0) s.append(" (вернулась ").append((int) reacq).append(')');
        s.append('\n').append(mmss(num(json, "длительность_с")))
         .append("   тактов ").append((int) ticks);
        double loop = num(json, "такт_мс_медиана");
        if (loop > 0) s.append("   ").append(fmt1(1000.0 / loop)).append(" Гц");
        s.append('\n').append("окно ").append((int) num(json, "окно_медиана")).append(" px");
        double temp = num(json, "батарея_нагрев");
        if (temp > 0) s.append("   батарея ").append(fmt1(temp)).append("°C");
        if (!hasVideo) s.append("\nбез видео");
        if (!bool(json, "ok")) s.append("\nзавершился с ошибкой");
        return s.toString();
    }
}
