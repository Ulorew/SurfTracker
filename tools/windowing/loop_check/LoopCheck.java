import com.surftracker.camfps.LoopControl;

/**
 * Проверка расчёта уставки — первой правки петли, у которой до сих пор не было
 * ни одного стенда.
 *
 * Почему это важнее, чем кажется: приёмка синхронизации делалась на
 * НЕПОДВИЖНОМ стенде, где формула тождественна прежней, — то есть измеряла то,
 * что не могло измениться. Здесь угол вала между съёмкой и командой меняется
 * намеренно, и разница видна.
 */
public class LoopCheck {

    static int failed = 0;

    static void near(String what, double got, double want) {
        boolean ok = Math.abs(got - want) < 1e-6;
        if (!ok) failed++;
        System.out.println((ok ? "  ok   " : "  ПЛОХО ") + what + ": " + got
                + (ok ? "" : "   ожидалось " + want));
    }

    static void eq(String what, Object got, Object want) {
        boolean ok = String.valueOf(got).equals(String.valueOf(want));
        if (!ok) failed++;
        System.out.println((ok ? "  ok   " : "  ПЛОХО ") + what + ": " + got
                + (ok ? "" : "   ожидалось " + want));
    }

    public static void main(String[] a) {
        final double K = 1.2; final int SIGN = -1;

        System.out.println("== вал не двигался: формула тождественна прежней ==");
        // Ровно то, что мерила прежняя приёмка. Здесь она обязана совпасть, и
        // именно поэтому НИЧЕГО не проверяла.
        LoopControl.Out a1 = LoopControl.command(10.0, 5.0, 5.0, K, SIGN, true);
        double prev = SIGN * K * Math.toRadians(10.0);
        near("уставка как в прежней формуле", a1.w, prev);
        eq("синхронизация применена", a1.synced, true);

        System.out.println("== вал ушёл между съёмкой и командой ==");
        // Цель неподвижна, вал за это время повернулся на 0.05 рад в сторону
        // цели: свежая ошибка обязана СТАТЬ МЕНЬШЕ на эти же 0.05 рад.
        LoopControl.Out a2 = LoopControl.command(10.0, 5.0, 5.0 - 0.05, K, SIGN, true);
        near("свежая ошибка уменьшилась на поворот",
                Math.toRadians(a2.errFreshDeg),
                SIGN * Math.toRadians(10.0) + 0.05);
        boolean smaller = Math.abs(a2.w) < Math.abs(prev);
        eq("уставка меньше прежней", smaller, true);
        // И в другую сторону: вал ушёл ОТ цели — команда обязана вырасти
        LoopControl.Out a3 = LoopControl.command(10.0, 5.0, 5.0 + 0.05, K, SIGN, true);
        eq("уставка больше прежней", Math.abs(a3.w) > Math.abs(prev), true);

        System.out.println("== снимок угла негоден: откат на прежнюю формулу ==");
        LoopControl.Out b1 = LoopControl.command(10.0, 0.0, 5.0, K, SIGN, false);
        near("уставка прежняя", b1.w, prev);
        eq("синхронизации не было", b1.synced, false);
        eq("не отвергнута (её и не пробовали)", b1.rejected, false);
        eq("цель в мире не пишется", Double.isNaN(b1.tgtWorldRad), true);

        System.out.println("== первый такт прогона 17 августа ==");
        // Телеметрии ещё не было -> снимок 0, а угол уже 11.881 рад.
        // Свежая ошибка выходила -661°, уставка -13.8 рад/с: четверть секунды
        // стенд гнало на максимуме.
        LoopControl.Out c1 = LoopControl.command(-19.86, 0.0, 11.881, K, SIGN, true);
        eq("синхронизация ОТВЕРГНУТА", c1.rejected, true);
        eq("и не применена", c1.synced, false);
        near("уставка по прежней формуле", c1.w, SIGN * K * Math.toRadians(-19.86));
        eq("уставка в разумных пределах", Math.abs(c1.w) < 1.0, true);

        System.out.println("== граница здравого смысла ==");
        // 89° проходит, 91° — нет.
        double th = 5.0;
        LoopControl.Out d1 = LoopControl.command(0, th + Math.toRadians(89), th, K, SIGN, true);
        eq("89 градусов принято", d1.synced, true);
        LoopControl.Out d2 = LoopControl.command(0, th + Math.toRadians(91), th, K, SIGN, true);
        eq("91 градус отвергнут", d2.rejected, true);

        System.out.println("== знак +1 остаётся согласованным ==");
        LoopControl.Out e1 = LoopControl.command(10.0, 5.0, 5.0, K, +1, true);
        near("уставка как в прежней формуле", e1.w, 1 * K * Math.toRadians(10.0));
        LoopControl.Out e2 = LoopControl.command(10.0, 5.0, 5.0 + 0.05, K, +1, true);
        eq("поворот к цели уменьшает уставку", Math.abs(e2.w) < Math.abs(e1.w), true);

        System.out.println();
        if (failed == 0) System.out.println("ИТОГ: расчёт уставки исправен");
        else { System.out.println("ИТОГ: " + failed + " ПРОВЕРОК ПРОВАЛЕНО"); System.exit(1); }
    }
}
