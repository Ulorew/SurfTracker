import com.surftracker.camfps.Tracker;

/**
 * Прогон переноса на синтетическом сценарии. Печатает состояние по тактам,
 * чтобы питонский оригинал мог сличить числа своими.
 *
 * Сценарий детерминированный и один и тот же с обеих сторон: сверять
 * реализации на СЛУЧАЙНЫХ входах бессмысленно, а на одном такте — недостаточно,
 * потому что расхождение фильтра накапливается.
 */
public class PortCheck {
    public static void main(String[] a) {
        Tracker t = new Tracker(1920, 1440);
        double dt = 0.25;
        t.seed(960, 720, 200);
        // цель едет вправо, размер медленно растёт, на тактах 6-8 пропуски
        for (int k = 0; k < 16; k++) {
            double side = t.windowSide();
            double pcx = t.planCx(dt, side), pcy = t.planCy(dt, side);
            boolean miss = (k >= 6 && k <= 8);
            if (miss) {
                t.advance(dt);
            } else {
                double mx = 960 + 40.0 * k, my = 720 + 5.0 * k;
                double sz = 200 + 6.0 * k;
                // два кандидата: настоящий и ложный поодаль
                double[][] dets = { {mx, my, sz}, {mx + 300, my - 120, sz * 1.4} };
                int pick = t.selectTarget(dets, 2, pcx, pcy, side);
                if (pick >= 0) t.update(dets[pick][0], dets[pick][1], dt, dets[pick][2]);
                else t.advance(dt);
                System.out.printf("%d pick=%d%n", k, pick);
            }
            // Девять знаков, а не четыре: сличение упирается в точность ПЕЧАТИ
            // раньше, чем в точность переноса. Первая редакция печатала
            // четыре и объявляла расхождение 1e-5 при пороге 1e-6.
            System.out.printf("%d %.9f %.9f %.9f %.9f %.9f %.9f %d %d%n",
                    k, t.cx, t.cy, t.vx, t.vy, t.filteredSize, side, t.missCount, t.status);
        }
    }
}
