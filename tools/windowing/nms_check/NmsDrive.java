import com.surftracker.camfps.Tracker;
import java.io.*;
import java.util.*;

/**
 * Гонит НАСТОЯЩИЙ Tracker.nms по сгенерированным сырым тензорам.
 *
 * Зачем отдельный стенд. port_check читает готовые cx,cy,size из текста и nms
 * не вызывает вовсе, java-тестов в проекте нет — то есть весь отбор кандидатов
 * (порог, вытеснение слабейшего при переполнении, сортировка, подавление по
 * IoU, размер как наибольшая сторона, шестой столбец уверенности) не был
 * покрыт ничем. Четыре внесённые в него порчи проходили и check.sh, и pytest.
 */
public class NmsDrive {
    public static void main(String[] args) throws Exception {
        BufferedReader r = new BufferedReader(new FileReader(args[0]));
        PrintWriter w = new PrintWriter(new BufferedWriter(new FileWriter(args[1])));
        w.println("case,idx,cx,cy,size,bw,bh,conf");
        String line;
        while ((line = r.readLine()) != null) {
            line = line.trim();
            if (line.isEmpty()) continue;
            String[] h = line.split(" ");          // CASE имя якорей max
            String name = h[1];
            int nA = Integer.parseInt(h[2]);
            int max = Integer.parseInt(h[3]);
            float[][] raw = new float[5][nA];
            for (int row = 0; row < 5; row++) {
                String[] v = r.readLine().trim().split(" ");
                for (int a = 0; a < nA; a++) raw[row][a] = Float.parseFloat(v[a]);
            }
            float[][] out = new float[Math.max(max, 1)][6];
            int n = Tracker.nms(raw, nA, out, max);
            for (int i = 0; i < n; i++)
                w.printf(Locale.US, "%s,%d,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f%n",
                         name, i, out[i][0], out[i][1], out[i][2],
                         out[i][3], out[i][4], out[i][5]);
            if (n == 0) w.printf("%s,-1,,,,,,%n", name);
        }
        r.close(); w.close();
    }
}
