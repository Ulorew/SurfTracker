package com.surftracker.camfps;

/**
 * Расчёт уставки — БЕЗ Android, чтобы его можно было проверить.
 *
 * Вынесено потому, что проверки не было вовсе: ни один стенд не компилирует
 * TrackActivity (2080 строк), и выключение синхронизации целиком не меняло
 * вывод ни одного из пяти стендов. То есть у правки, ради которой чинили
 * качание вокруг неподвижной цели, не было способа отличить её наличие от
 * отсутствия.
 *
 * ЧТО ЗДЕСЬ. Ровно одно решение: какую угловую скорость просить, зная
 * ошибку на кадре, угол вала В МОМЕНТ СЪЁМКИ и угол вала СЕЙЧАС.
 *
 *   цель_в_мире = вал_при_захвате + знак * ошибка
 *   команда     = K * (цель_в_мире - вал_сейчас)
 *
 * Когда угол между съёмкой и командой не изменился, это тождественно прежней
 * формуле sign*K*ошибка — поэтому неподвижный вал ничего не проверяет, и
 * приёмка «медианы на стоящем стенде» ничего не значила.
 */
public final class LoopControl {

    private LoopControl() {}

    /** Больше этого свежая ошибка быть не может: это рассинхрон, а не цель. */
    public static final double SANE_DEG = 90.0;

    public static final class Out {
        public double w;              // уставка, рад/с
        public double errFreshDeg;    // свежая ошибка, градусы
        public double tgtWorldRad;    // угол цели в мире, рад; NaN — синхронизации не было
        public boolean synced;        // применена ли синхронизация
        public boolean rejected;      // синхронизация отвергнута как несуразная
    }

    /**
     * @param errDeg     ошибка на кадре, градусы
     * @param thetaCap   угол вала в момент СЪЁМКИ, рад
     * @param thetaNow   угол вала СЕЙЧАС, рад
     * @param syncOk     годен ли снимок угла (телеметрия была и свежа)
     */
    public static Out command(double errDeg, double thetaCap, double thetaNow,
                              double K, int sign, boolean syncOk) {
        Out o = new Out();
        o.tgtWorldRad = Double.NaN;
        if (syncOk) {
            double tgt = thetaCap + sign * Math.toRadians(errDeg);
            double fresh = tgt - thetaNow;
            if (Math.abs(Math.toDegrees(fresh)) > SANE_DEG) {
                // Первый такт прогона 17 августа: телеметрии ещё не было,
                // снимок угла оказался нулём, а к моменту команды угол уже
                // пришёл — свежая ошибка вышла -661°, уставка -13.8 рад/с.
                // Прошивка обрезала до 1.2, но четверть секунды стенд гнало на
                // максимуме.
                o.rejected = true;
            } else {
                o.synced = true;
                o.errFreshDeg = Math.toDegrees(fresh);
                o.tgtWorldRad = tgt;
                o.w = K * fresh;
                return o;
            }
        }
        // Откат на прежнюю формулу: без энкодера синхронизировать нечего.
        o.errFreshDeg = errDeg * sign;
        o.w = sign * K * Math.toRadians(errDeg);
        return o;
    }
}
