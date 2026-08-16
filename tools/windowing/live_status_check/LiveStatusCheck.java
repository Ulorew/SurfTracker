import com.surftracker.camfps.LiveStatus;

/**
 * Проверка того, что видно на экране во время прогона.
 *
 * Эта функция не падает при ошибке — она показывает наблюдателю неверное
 * число. Он стоит в трёх метрах, читает «на цели 100%» и спокойно уходит из
 * кадра. Поэтому проверяется не «не бросает исключение», а КАЖДАЯ ветка и
 * граница: ноль тактов, ноль попаданий, отсутствие связи, режим без мотора.
 */
public class LiveStatusCheck {

    static int failed = 0;

    static void eq(String what, Object got, Object want) {
        boolean ok = String.valueOf(got).equals(String.valueOf(want));
        if (!ok) failed++;
        System.out.println((ok ? "  ok   " : "  ПЛОХО ") + what + ": [" + got + "]"
                + (ok ? "" : "   ожидалось [" + want + "]"));
    }

    static void has(String what, String s, String part) {
        boolean ok = s.contains(part);
        if (!ok) failed++;
        System.out.println((ok ? "  ok   " : "  ПЛОХО ") + what
                + (ok ? "" : ": нет «" + part + "» в [" + s + "]"));
    }

    static void hasNo(String what, String s, String part) {
        boolean ok = !s.contains(part);
        if (!ok) failed++;
        System.out.println((ok ? "  ok   " : "  ПЛОХО ") + what
                + (ok ? "" : ": лишнее «" + part + "» в [" + s + "]"));
    }

    public static void main(String[] args) {
        System.out.println("== до первого такта показывать нечего ==");
        eq("пусто", LiveStatus.text(false, 0, 0, 0, false, false, false, false, 60000, null), "");
        eq("цвет ожидания", LiveStatus.color(false, 0, false, null), LiveStatus.ЖДЁТ);

        System.out.println("== ведёт ==");
        String s = LiveStatus.text(false, 100, 87, 1, true, false, true, true, 45000, null);
        System.out.println("    " + s.replace("\n", "\n    "));
        has("состояние", s, "ВЕДЁТ");
        has("остаток времени", s, "ещё 45 с");
        has("доля", s, "на цели 87%");
        has("потери", s, "потерь 1");
        has("связь", s, "мотор на связи");
        has("запись", s, "пишет видео");
        eq("цвет ведения", LiveStatus.color(false, 100, true, null), LiveStatus.ВЕДЁТ);

        System.out.println("== ищет, мотор молчит, без записи ==");
        s = LiveStatus.text(false, 40, 10, 3, false, false, false, false, 5000, null);
        System.out.println("    " + s.replace("\n", "\n    "));
        has("состояние", s, "ИЩЕТ");
        // Молчащий мотор обязан читаться КРУПНО и без вопросов: прогон, в
        // котором телефон считает, а вал стоит, выглядит нормально во всём
        // остальном.
        has("молчание мотора", s, "МОТОР МОЛЧИТ");
        hasNo("нет ложной записи", s, "пишет видео");
        eq("цвет поиска", LiveStatus.color(false, 40, false, null), LiveStatus.ИЩЕТ);

        System.out.println("== без мотора — это не отказ связи ==");
        s = LiveStatus.text(false, 40, 30, 0, true, true, false, false, 1000, null);
        has("режим без мотора", s, "без мотора");
        hasNo("не пугает молчанием", s, "МОТОР МОЛЧИТ");
        hasNo("нет потерь — нет строки", s, "потерь");

        System.out.println("== готово ==");
        s = LiveStatus.text(true, 412, 361, 1, false, false, false, false, 0, null);
        System.out.println("    " + s.replace("\n", "\n    "));
        has("готово", s, "ГОТОВО");
        has("итог", s, "412 тактов, на цели 88%");
        has("разрешение подойти", s, "можно подходить");
        eq("цвет готовности", LiveStatus.color(true, 412, false, null), LiveStatus.ГОТОВО);

        System.out.println("== ОТКАЗ не должен выглядеть как успех ==");
        // Прогон падал на открытии камеры и показывал зелёное «ГОТОВО»:
        // наблюдатель читал успех там, где не было ни одного такта.
        String err = "java.lang.IllegalArgumentException: getCameraCharacteristics:851: "
                   + "Unable to retrieve camera characteristics for unknown device 0";
        s = LiveStatus.text(true, 0, 0, 0, false, true, false, false, 0, err);
        System.out.println("    " + s.replace("\n", "\n    "));
        has("назван отказом", s, "ОТКАЗ");
        hasNo("не назван готовым", s, "ГОТОВО");
        hasNo("не зовёт подходить как за результатом", s, "можно подходить");
        has("сказано, что не начался", s, "не начался");
        eq("цвет отказа", LiveStatus.color(true, 0, false, err), LiveStatus.ОТКАЗ);
        // Отказ посреди прогона: часть тактов успела пройти, и это надо сказать
        s = LiveStatus.text(true, 57, 40, 1, false, false, true, true, 0, err);
        has("успевшие такты", s, "успело 57 тактов");
        eq("цвет отказа важнее ведения", LiveStatus.color(false, 57, true, err), LiveStatus.ОТКАЗ);
        System.out.println("    короткая причина: [" + LiveStatus.shortError(err) + "]");
        hasNo("без пакета класса", LiveStatus.shortError(err), "java.lang");
        eq("пустая причина", LiveStatus.shortError(null), "");

        System.out.println("== границы ==");
        eq("ноль тактов не делится", LiveStatus.percent(0, 0), 0);
        eq("ноль попаданий", LiveStatus.percent(0, 50), 0);
        eq("все попадания", LiveStatus.percent(50, 50), 100);
        eq("округление вверх", LiveStatus.percent(875, 1000), 88);
        // Время вышло, а прогон ещё доигрывает остановку: «ещё -3 с» было бы
        // хуже, чем ничего.
        hasNo("нет отрицательного остатка",
                LiveStatus.text(false, 10, 5, 0, true, true, false, false, -3000, null), "ещё");
        hasNo("нет нулевого остатка",
                LiveStatus.text(false, 10, 5, 0, true, true, false, false, 0, null), "ещё");
        // Итог «ГОТОВО» на прогоне без единого такта не должен делить на ноль
        has("готово при нуле тактов",
                LiveStatus.text(true, 0, 0, 0, false, false, false, false, 0, null), "на цели 0%");

        System.out.println();
        if (failed == 0) System.out.println("ИТОГ: экран прогона показывает верное");
        else { System.out.println("ИТОГ: " + failed + " ПРОВЕРОК ПРОВАЛЕНО"); System.exit(1); }
    }
}
