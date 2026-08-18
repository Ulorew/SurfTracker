import java.io.*;
import java.util.*;
import com.surftracker.camfps.Tracker;

/**
 * Гоняет НАСТОЯЩИЙ Tracker (тот же файл, что собирается в apk) по сценариям и
 * печатает решения тактами.
 *
 * Прошлая проверка переноса сравнивала числа фильтра и объявила согласие, не
 * имея доступа ни к состоянию трека, ни к тому, какую детекцию он выбрал.
 * Здесь сличается ИМЕННО ВЫБОР: индекс детекции, состояние и сторона окна —
 * то, что определяет поведение камеры. Совпадение координат фильтра при
 * разном выборе цели ничего не значит, а расхождение в шестом знаке при
 * одинаковом выборе не значит ничего тем более.
 */
public class PortDrive {

    public static void main(String[] args) throws Exception {
        // Режим прижатия центра приёма приходит извне: сличение обязано
        // проходить в ОБОИХ, иначе флаг станет щелью, в которой версии
        // разъедутся молча.
        if (args.length > 2)
            Tracker.VIEW_CLAMP_KEEPS_WINDOW_INSIDE = Boolean.parseBoolean(args[2]);
        // Механизм А тоже приходит извне: сличение обязано идти по МАТРИЦЕ
        // режимов, иначе включённый механизм окажется вне проверки ровно так
        // же, как когда-то оказался режим прижатия центра.
        if (args.length > 3)
            Tracker.ENABLE_SIZE_SCORING = Boolean.parseBoolean(args[3]);
        BufferedReader in = new BufferedReader(new FileReader(args[0]));
        PrintWriter out = new PrintWriter(new FileWriter(args[1]));
        out.println("scenario,tick,chosen,status,miss,side,pred_cx,pred_cy");

        int W = 0, H = 0, minWin = 640;
        double dt = 0;
        String line;
        String scen = null;
        int nTicks = 0, tick = 0;
        Tracker trk = null;

        while ((line = in.readLine()) != null) {
            String[] p = line.trim().split("\\s+");
            if (p[0].equals("FRAME")) {
                W = Integer.parseInt(p[1]); H = Integer.parseInt(p[2]);
                dt = Double.parseDouble(p[3]); minWin = Integer.parseInt(p[4]);
                if (minWin != Tracker.MIN_WINDOW_PX)
                    throw new IllegalStateException("min_window сценария " + minWin
                            + " не равен MIN_WINDOW_PX " + Tracker.MIN_WINDOW_PX
                            + ": сличение мерило бы разные конфигурации");
                continue;
            }
            if (p[0].equals("SCENARIO")) {
                scen = p[1]; nTicks = Integer.parseInt(p[2]); tick = 0;
                trk = new Tracker(W, H);
                continue;
            }
            int n = Integer.parseInt(p[0]);
            double[][] dets = new double[Math.max(n, 1)][3];   // cx, cy, size
            for (int i = 0; i < n; i++) {
                dets[i][0] = Double.parseDouble(p[1 + i * 3]);
                dets[i][1] = Double.parseDouble(p[2 + i * 3]);
                dets[i][2] = Double.parseDouble(p[3 + i * 3]);
            }

            int chosen; double side, pcx, pcy;
            if (!trk.initialized) {
                // Затравка — вне сличаемой логики: у офлайнового TrackState её
                // делает конструктор, у телефона — политика приложения по
                // времени потери. Общее у них только правило «первая детекция
                // списка», поэтому здесь оно и применяется, на обеих сторонах.
                if (n == 0) { chosen = -1; side = 0; pcx = 0; pcy = 0; }
                else {
                    trk.seed(dets[0][0], dets[0][1], dets[0][2]);
                    chosen = 0; side = trk.windowSide();
                    pcx = trk.cx; pcy = trk.cy;
                }
                out.printf(Locale.US, "%s,%d,%d,%s,%d,%.4f,%.4f,%.4f%n",
                        scen, tick, chosen,
                        trk.initialized ? (trk.status == Tracker.TRACKING ? "T" : "L") : "-",
                        trk.missCount, side, pcx, pcy);
            } else {
                Tracker.Tick t = trk.step(dets, n, dt);
                out.printf(Locale.US, "%s,%d,%d,%s,%d,%.4f,%.4f,%.4f%n",
                        scen, tick, t.chosen, t.status == Tracker.TRACKING ? "T" : "L",
                        t.miss, t.side, t.predCx, t.predCy);
            }
            tick++;
            if (tick == nTicks) { scen = null; }
        }
        in.close(); out.close();
    }
}
