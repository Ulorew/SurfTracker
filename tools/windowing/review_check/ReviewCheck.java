import com.surftracker.camfps.ReviewModel;

/**
 * Проверка разбора лога и геометрии наложения.
 *
 * Опасность здесь особого рода: ошибка не падает и не выглядит ошибкой.
 * Рамка, нарисованная не там, читается как «трекер вёл не туда», и разбираться
 * пойдут с алгоритмом. Поэтому проверяется:
 *   - разбор по ИМЕНАМ колонок, а не по номерам (лог уже дважды менял состав);
 *   - привязка такта ко времени видео вместе со смещением записи;
 *   - пересчёт сенсорных пикселей в пиксели кадра при РАЗНЫХ размерах;
 *   - момент ПЕРЕХОДА в потерю, а не каждый такт потери.
 */
public class ReviewCheck {

    static int failed = 0;

    static void eq(String what, Object got, Object want) {
        boolean ok = String.valueOf(got).equals(String.valueOf(want));
        if (!ok) failed++;
        System.out.println((ok ? "  ok   " : "  ПЛОХО ") + what + ": " + got
                + (ok ? "" : "   ожидалось " + want));
    }

    static void near(String what, double got, double want) {
        boolean ok = Math.abs(got - want) < 0.51;
        if (!ok) failed++;
        System.out.println((ok ? "  ok   " : "  ПЛОХО ") + what + ": " + got
                + (ok ? "" : "   ожидалось " + want));
    }

    // Заголовок повторяет боевой, включая колонки, добавленные позже.
    static final String HEAD =
        "i,t_ms,есть_цель,conf,cx_сенсор,ошибка_град,ω_уставка,"
      + "θ_enc,ω_ramp,статус,watchdog,потолок,рампа,энкодер,кламп,срыв,"
      + "инференс_мс,такт_мс,Sc,winCx,winCy,ω_сглаж,ужатие,"
      + "кандидатов,до_предсказания,порог,состояние,промахов,"
      + "bx,by,размер_детекции,размер_фильтра";

    static String row(int i, int t, int hit, int sc, int wcx, int wcy,
                      String st, int miss, String bx, String by, String sz) {
        return i + "," + t + "," + hit + ",0.9000,1920,3.5000,0.1200,"
             + "1.0,0.1,3,0,0,0,1,0,0,"
             + "83.0,120.0," + sc + "," + wcx + "," + wcy + ",0.1,1.0,"
             + "2,140.0,432.0," + st + "," + miss + ","
             + bx + "," + by + "," + sz + ",300.0";
    }

    static final String JSON =
        "{\"tag\":\"t\",\"сенсор\":\"3840x2160\",\"запись\":\"1920x1080@24\","
      + "\"видео_смещение_мс\":2500,\"ok\":true}";

    public static void main(String[] args) {
        StringBuilder csv = new StringBuilder(HEAD).append('\n');
        csv.append(row(0,    0, 1, 1440, 1900, 1000, "вед",    0, "1910.0", "1005.0", "300.0")).append('\n');
        csv.append(row(1,  120, 1, 1440, 1905, 1002, "вед",    0, "1912.0", "1008.0", "310.0")).append('\n');
        csv.append(row(2,  240, 0, 1440, 1910, 1004, "вед",    1, "",       "",       "")).append('\n');
        csv.append(row(3,  360, 0, 1656, 1915, 1006, "потеря", 5, "",       "",       "")).append('\n');
        csv.append(row(4,  480, 0, 1656, 1915, 1006, "потеря", 6, "",       "",       "")).append('\n');
        csv.append(row(5,  600, 1, 1440, 1920, 1010, "вед",    0, "1930.0", "1012.0", "305.0")).append('\n');
        csv.append(row(6,  720, 0, 1440, 1925, 1012, "потеря", 5, "",       "",       "")).append('\n');

        ReviewModel m = ReviewModel.parse(csv.toString(), JSON);

        System.out.println("== разбор ==");
        eq("тактов", m.ticks.size(), 7);
        eq("пригоден", m.usable(), true);
        eq("нет жалобы на winCy", m.noWinY, false);
        eq("сенсор", m.sensorW + "x" + m.sensorH, "3840x2160");
        eq("видео", m.videoW + "x" + m.videoH, "1920x1080");
        eq("смещение", m.videoOffsetMs, 2500L);

        System.out.println("== потери: переходы, а не каждый такт ==");
        // Тактов в состоянии «потеря» четыре (3,4,6), но ПЕРЕХОДОВ два.
        // Список из четырёх пунктов на два события сделал бы кнопку «к потере»
        // бесполезной: она топталась бы внутри одного эпизода.
        eq("переходов в потерю", m.losses.size(), 2);
        eq("первый переход", m.losses.get(0), 3);
        eq("второй переход", m.losses.get(1), 6);

        System.out.println("== привязка ко времени ==");
        eq("нулевой такт со смещением", m.videoMs(m.ticks.get(0)), 2500L);
        eq("пятый такт", m.videoMs(m.ticks.get(5)), 3100L);
        eq("такт по времени видео", m.tickAtVideoMs(3100), 5);
        // Между тактами берётся ближайший, а не предыдущий: на 12 Гц соседние
        // такты отстоят на 80 мс, и «предыдущий» показывал бы устаревшую рамку.
        eq("ближайший, а не предыдущий", m.tickAtVideoMs(2800), 2);
        eq("до начала лога", m.tickAtVideoMs(0), 0);
        eq("после конца", m.tickAtVideoMs(999999), 6);

        System.out.println("== пересчёт координат ==");
        // Сенсор 3840 -> видео 1920: ровно вдвое. Забыть этот пересчёт значит
        // нарисовать рамку вдвое дальше от центра, чем она была.
        near("x сенсора 1920 -> кадр 1920", m.toViewX(1920, 1920), 960);
        near("y сенсора 1080 -> кадр 1080", m.toViewY(1080, 1080), 540);
        near("край по x", m.toViewX(3840, 1920), 1920);
        // Показ на экране уже 1080 в ширину — тот же пересчёт, другая ширина
        near("тот же x при показе 1080", m.toViewX(1920, 1080), 540);

        System.out.println("== значения такта ==");
        ReviewModel.Tick t0 = m.ticks.get(0);
        eq("цель есть", t0.hit, true);
        near("bx", t0.cx, 1910);
        near("размер", t0.size, 300);
        eq("окно", t0.win, 1440);
        eq("центр окна x", t0.winCx, 1900);
        eq("центр окна y", t0.winCy, 1000);
        eq("ведёт", t0.tracking, true);
        ReviewModel.Tick t3 = m.ticks.get(3);
        eq("потеря распознана", t3.tracking, false);
        eq("окно раздулось", t3.win, 1656);
        eq("пустая рамка -> NaN", Double.isNaN(t3.cx), true);

        System.out.println("== состав колонок изменился ==");
        // Колонку вставили В СЕРЕДИНУ. Разбор по номерам сдвинул бы всё
        // следующее на позицию и показал бы чужие числа как свои.
        String head2 = HEAD.replace(",Sc,", ",НОВАЯ_КОЛОНКА,Sc,");
        StringBuilder csv2 = new StringBuilder(head2).append('\n');
        csv2.append(row(0, 0, 1, 1440, 1900, 1000, "вед", 0, "1910.0", "1005.0", "300.0")
                .replace(",120.0,1440,", ",120.0,999,1440,")).append('\n');
        ReviewModel m2 = ReviewModel.parse(csv2.toString(), JSON);
        eq("окно не съехало", m2.ticks.get(0).win, 1440);
        eq("центр окна не съехал", m2.ticks.get(0).winCx, 1900);

        System.out.println("== старый лог без winCy ==");
        String head3 = HEAD.replace(",winCy", "");
        StringBuilder csv3 = new StringBuilder(head3).append('\n');
        csv3.append(row(0, 0, 1, 1440, 1900, 1000, "вед", 0, "1910.0", "1005.0", "300.0")
                .replace(",1900,1000,", ",1900,")).append('\n');
        ReviewModel m3 = ReviewModel.parse(csv3.toString(), JSON);
        eq("разбирается дальше", m3.usable(), true);
        eq("и говорит об этом", m3.noWinY, true);
        eq("вертикаль помечена неизвестной", m3.ticks.get(0).winCy, -1);

        System.out.println("== мусор на входе ==");
        eq("пустой лог", ReviewModel.parse("", JSON).usable(), false);
        eq("только заголовок", ReviewModel.parse(HEAD, JSON).usable(), false);
        ReviewModel bad = ReviewModel.parse("а,б,в\n1,2,3\n", JSON);
        eq("чужой csv не пригоден", bad.usable(), false);
        eq("и назван", bad.error != null, true);
        // Без «запись» в json видео считается равным сенсору — пересчёт тогда
        // тождественный, а не случайный.
        ReviewModel noRec = ReviewModel.parse(csv.toString(),
                "{\"сенсор\":\"3840x2160\",\"ok\":true}");
        eq("видео = сенсор", noRec.videoW, 3840);
        eq("смещение 0", noRec.videoOffsetMs, 0L);

        System.out.println();
        if (failed == 0) System.out.println("ИТОГ: разбор записи исправен");
        else { System.out.println("ИТОГ: " + failed + " ПРОВЕРОК ПРОВАЛЕНО"); System.exit(1); }
    }
}
