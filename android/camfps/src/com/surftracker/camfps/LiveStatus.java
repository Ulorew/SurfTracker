package com.surftracker.camfps;

/**
 * Текст живого состояния прогона — чистая функция, без Android.
 *
 * Вынесено ровно по той же причине, что такт трекера в Tracker.step() и
 * разбор в RunJson: внутри активности это нельзя ни запустить, ни проверить,
 * а веток здесь семь и есть деление на число тактов. Ошибка в такой функции
 * не падает, а показывает наблюдателю неверное число — и он уходит из кадра,
 * решив, что всё хорошо.
 */
public final class LiveStatus {

    private LiveStatus() {}

    /**
     * Короткая причина отказа. Из «java.lang.IllegalArgumentException:
     * getCameraCharacteristics:851: Unable to retrieve...» на экране нужно то,
     * что читается за секунду, а не пакет класса исключения.
     */
    public static String shortError(String e) {
        if (e == null) return "";
        String s = e;
        int c = s.lastIndexOf(':');
        // Берём хвост после последнего двоеточия, если он содержателен;
        // иначе — всю строку без пакета.
        if (c >= 0 && s.length() - c > 8) s = s.substring(c + 1).trim();
        else {
            int d = s.indexOf(' ');
            int p = s.lastIndexOf('.', d < 0 ? s.length() - 1 : d);
            if (p >= 0) s = s.substring(p + 1);
        }
        return s.length() > 60 ? s.substring(0, 60) + "…" : s;
    }

    /** Цвета фона: ожидание, ведёт, ищет, готово, отказ. */
    public static final int ЖДЁТ = 0x00000000, ВЕДЁТ = 0xFF1E3A1E,
                            ИЩЕТ = 0xFF3A2A1E, ГОТОВО = 0xFF223322,
                            ОТКАЗ = 0xFF4A1E1E;

    public static int color(boolean done, int ticks, boolean tracking, String error) {
        // Отказ проверяется ПЕРВЫМ. Упавший прогон уже показывал зелёное
        // «ГОТОВО»: наблюдатель, вернувшись, читал успех там, где камера
        // вообще не открылась.
        if (error != null) return ОТКАЗ;
        if (done) return ГОТОВО;
        if (ticks == 0) return ЖДЁТ;
        return tracking ? ВЕДЁТ : ИЩЕТ;
    }

    /** Доля тактов на цели, в процентах. Ноль тактов — ноль, а не деление. */
    public static int percent(int hits, int ticks) {
        return ticks > 0 ? (int) Math.round(100.0 * hits / ticks) : 0;
    }

    /**
     * @param leftMs сколько осталось; отрицательное или ноль — не показывать
     */
    public static String text(boolean done, int ticks, int hits, int loss,
                              boolean tracking, boolean dry, boolean link,
                              boolean rec, long leftMs, String error) {
        if (error != null) {
            // Причина — коротко и первой строкой после слова «ОТКАЗ».
            // Полное исключение уходит в прогон.json; на экране с трёх метров
            // важно одно: прогон НЕ состоялся, подходить и разбираться.
            return "ОТКАЗ\n" + shortError(error)
                    + (ticks > 0 ? ("\nуспело " + ticks + " тактов") : "\nне начался");
        }
        if (done) {
            return "ГОТОВО\n" + ticks + " тактов, на цели " + percent(hits, ticks) + "%"
                    + ", потерь " + loss + "\nможно подходить";
        }
        // До первого такта показывать нечего: камера ещё поднимается, и любая
        // цифра здесь была бы выдумкой.
        if (ticks == 0) return "";

        StringBuilder s = new StringBuilder(tracking ? "ВЕДЁТ" : "ИЩЕТ");
        if (leftMs > 0) s.append("   ещё ").append(Math.round(leftMs / 1000.0)).append(" с");
        s.append('\n').append(ticks).append(" тактов, на цели ")
         .append(percent(hits, ticks)).append('%');
        if (loss > 0) s.append(", потерь ").append(loss);
        s.append('\n');
        // Мотор и запись — то, о чём спрашивали ПОСЛЕ прогона: «идёт ли
        // запись», «связан ли мотор». Ответ должен быть на экране, пока ещё
        // можно что-то поправить, а не в логе, который смотрят потом.
        s.append(dry ? "без мотора" : (link ? "мотор на связи" : "МОТОР МОЛЧИТ"));
        if (rec) s.append("   пишет видео");
        return s.toString();
    }
}
