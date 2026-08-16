package com.surftracker.camfps;

import java.util.ArrayList;
import java.util.List;

/**
 * Разбор лога прогона и геометрия наложения — БЕЗ Android.
 *
 * Здесь живёт то, что легче всего сделать правдоподобно и неверно: разбор
 * столбцов по номеру, пересчёт координат и привязка такта ко времени видео.
 * Рамка, нарисованная не там, выглядит как «трекер ошибся», и разбираться
 * пойдут с алгоритмом, а не с разметкой. Поэтому всё это чистые функции, и на
 * них есть стенд tools/windowing/review_check.
 *
 * СТОЛБЦЫ ИЩУТСЯ ПО ИМЕНИ, а не по номеру. Лог уже дважды менял состав
 * колонок; разбор по номеру пережил бы это молча, сдвинув все значения на
 * одну позицию.
 */
public final class ReviewModel {

    /** Один такт лога — только то, что нужно для показа. */
    public static final class Tick {
        public int i;
        public long tMs;
        public boolean hit;
        public double cx, cy, size;     // рамка цели, пиксели СЕНСОРА
        public double bw = Double.NaN, bh = Double.NaN;  // стороны рамки; NaN — их нет в логе
        public int winCx, winCy, win;   // окно: центр и сторона, пиксели сенсора
        public boolean tracking;        // состояние трекера на этом такте
        public int misses;
        public int cand;                // сколько кандидатов было в окне
        public double errDeg, w;
    }

    public final List<Tick> ticks = new ArrayList<>();
    /** Индексы тактов, на которых трек перешёл в потерю. */
    public final List<Integer> losses = new ArrayList<>();
    public int sensorW, sensorH, videoW, videoH;
    public long videoOffsetMs;
    public String error;
    /** Лог снят до появления колонки winCy: вертикаль окна неизвестна. */
    public boolean noWinY;

    /** Есть ли чем размечать: без окна и рамок показывать нечего. */
    public boolean usable() { return !ticks.isEmpty(); }

    // ---- разбор -----------------------------------------------------------

    static int col(String[] head, String name) {
        for (int i = 0; i < head.length; i++) if (head[i].trim().equals(name)) return i;
        return -1;
    }

    static double num(String[] f, int c) {
        if (c < 0 || c >= f.length) return Double.NaN;
        String v = f[c].trim();
        if (v.isEmpty()) return Double.NaN;
        try { return Double.parseDouble(v.replace(',', '.')); }
        catch (Throwable t) { return Double.NaN; }
    }

    static int inum(String[] f, int c, int def) {
        double d = num(f, c);
        return Double.isNaN(d) ? def : (int) Math.round(d);
    }

    /**
     * Разбор CSV прогона.
     *
     * @param csv содержимое лог.csv
     * @param json содержимое прогон.json (для размеров и смещения видео)
     */
    public static ReviewModel parse(String csv, String json) {
        ReviewModel m = new ReviewModel();
        if (csv == null || csv.isEmpty()) { m.error = "лог пуст"; return m; }
        String[] lines = csv.split("\n");
        if (lines.length < 2) { m.error = "в логе нет тактов"; return m; }
        String[] head = lines[0].split(",");

        int cI = col(head, "i"), cT = col(head, "t_ms"), cHit = col(head, "есть_цель");
        int cBx = col(head, "bx"), cBy = col(head, "by"), cSz = col(head, "размер_детекции");
        int cBw = col(head, "ширина_детекции"), cBh = col(head, "высота_детекции");
        int cSc = col(head, "Sc"), cWin = col(head, "winCx"), cWinY = col(head, "winCy");
        int cSt = col(head, "состояние"), cMiss = col(head, "промахов");
        int cCand = col(head, "кандидатов");
        int cErr = col(head, "ошибка_град"), cW = col(head, "ω_уставка");
        if (cT < 0 || cSc < 0) {
            m.error = "в логе нет колонок t_ms/Sc — это не лог слежения";
            return m;
        }

        boolean prevTracking = false;
        for (int li = 1; li < lines.length; li++) {
            String line = lines[li];
            if (line.trim().isEmpty()) continue;
            String[] f = line.split(",", -1);
            Tick t = new Tick();
            t.i = inum(f, cI, li - 1);
            t.tMs = (long) num(f, cT);
            t.hit = inum(f, cHit, 0) == 1;
            t.cx = num(f, cBx); t.cy = num(f, cBy); t.size = num(f, cSz);
            // Стороны появились позже: в логах без них рамка рисуется
            // квадратом со стороной в наибольший размер — как и рисовалась.
            t.bw = (cBw >= 0) ? num(f, cBw) : Double.NaN;
            t.bh = (cBh >= 0) ? num(f, cBh) : Double.NaN;
            t.win = inum(f, cSc, 0);
            t.winCx = inum(f, cWin, 0);
            // Логи, снятые до появления winCy, разбираются дальше: окно у них
            // рисуется по середине кадра, и это ЧЕСТНЕЕ, чем отказ показать
            // запись целиком. Признак ставится, чтобы экран сказал об этом.
            if (cWinY < 0) { t.winCy = -1; m.noWinY = true; }
            else t.winCy = inum(f, cWinY, -1);
            t.misses = inum(f, cMiss, 0);
            t.cand = inum(f, cCand, 0);
            t.errDeg = num(f, cErr); t.w = num(f, cW);
            // Состояние пишется словом: «вед» или «потеря».
            t.tracking = cSt >= 0 && cSt < f.length && f[cSt].trim().startsWith("вед");
            // Момент ПЕРЕХОДА в потерю, а не каждый такт потери: иначе список
            // «где посмотреть» состоит из сотни соседних тактов одного события.
            if (prevTracking && !t.tracking) m.losses.add(m.ticks.size());
            prevTracking = t.tracking;
            m.ticks.add(t);
        }

        m.sensorW = 3840; m.sensorH = 2160;
        String sens = RunJson.str(json, "сенсор");
        int[] wh = wh(sens);
        if (wh != null) { m.sensorW = wh[0]; m.sensorH = wh[1]; }
        // Видео пишется своим профилем и может отличаться от потока обработки.
        int[] vwh = wh(RunJson.str(json, "запись"));
        if (vwh != null) { m.videoW = vwh[0]; m.videoH = vwh[1]; }
        else { m.videoW = m.sensorW; m.videoH = m.sensorH; }
        m.videoOffsetMs = (long) RunJson.num(json, "видео_смещение_мс");
        return m;
    }

    /** «3840x2160» или «3840x2160@24» -> {3840, 2160}; иначе null. */
    static int[] wh(String s) {
        if (s == null) return null;
        int at = s.indexOf('@');
        if (at > 0) s = s.substring(0, at);
        int x = s.indexOf('x');
        if (x <= 0) return null;
        try {
            return new int[]{ Integer.parseInt(s.substring(0, x).trim()),
                              Integer.parseInt(s.substring(x + 1).trim()) };
        } catch (Throwable t) { return null; }
    }

    // ---- привязка ко времени и координатам ---------------------------------

    /** Время в видео для такта, мс. Смещение — сколько записи прошло до нулевого такта. */
    public long videoMs(Tick t) { return videoOffsetMs + t.tMs; }

    /** Ближайший такт к моменту видео. Список отсортирован по времени. */
    public int tickAtVideoMs(long ms) {
        if (ticks.isEmpty()) return -1;
        int best = 0; long bd = Long.MAX_VALUE;
        for (int i = 0; i < ticks.size(); i++) {
            long d = Math.abs(videoMs(ticks.get(i)) - ms);
            if (d < bd) { bd = d; best = i; }
        }
        return best;
    }

    /**
     * Пересчёт сенсорных пикселей в пиксели показываемого кадра.
     *
     * Два масштаба, а не один: видео пишется своим профилем, и при записи
     * 1080p поверх сенсора 3840 рамка, нарисованная «как есть», уехала бы
     * вчетверо. Плюс масштаб самого показа (кадр ужат под экран).
     */
    public double toViewX(double sensorX, double viewW) {
        return sensorX * (viewW / (double) sensorW);
    }

    /**
     * Вертикаль: с поправкой на ВЫРЕЗКУ, если пропорции потока и записи разные.
     *
     * Поток обработки идёт 1920x1440 (4:3), а запись — 3840x2160 (16:9): видео
     * это вертикальная вырезка из той же картинки, а не другая картинка.
     * Чистое растяжение по каждой оси (как было) сжимает разметку к середине
     * кадра: у нижнего края рамка оказывается ВЫШЕ цели, у верхнего — ниже, и
     * промах доходит до 360 px из 2160. На настоящей записи 16 августа цель
     * стояла у середины, и ошибка вышла 44 px — то есть незаметная, и оттого
     * особенно опасная: разбор выглядел бы исправным.
     *
     * Горизонталь при этом общая: 3840 = 2 x 1920, поле зрения по ширине то же.
     * Если когда-нибудь совпадут и пропорции, поправка обнулится сама.
     */
    public double toViewY(double sensorY, double viewH) {
        double scale = (sensorW > 0) ? (videoW > 0 ? videoW / (double) sensorW : 1.0) : 1.0;
        double fullH = sensorH * scale;          // высота потока в пикселях видео
        double crop = (fullH - videoH) / 2.0;    // срезано сверху и снизу
        if (videoH <= 0 || Math.abs(crop) < 0.5)
            return sensorY * (viewH / (double) sensorH);
        // Приводим к пикселям видео, снимаем вырезку, затем к пикселям показа.
        return (sensorY * scale - crop) * (viewH / (double) videoH);
    }

    /** Подпись такта для экрана разбора. */
    public static String caption(Tick t, int idx, int total) {
        StringBuilder s = new StringBuilder();
        s.append("такт ").append(idx + 1).append('/').append(total)
         .append("   ").append(t.tMs / 1000).append(',')
         .append(String.format(java.util.Locale.US, "%02d", (t.tMs % 1000) / 10)).append(" с\n");
        s.append(t.tracking ? "ведёт" : "ПОТЕРЯ");
        if (!t.hit) s.append(", детекции нет");
        s.append("   кандидатов ").append(t.cand);
        if (t.misses > 0) s.append("   промахов подряд ").append(t.misses);
        s.append("\nокно ").append(t.win).append(" px");
        if (!Double.isNaN(t.size)) s.append("   цель ").append(Math.round(t.size)).append(" px");
        if (!Double.isNaN(t.errDeg))
            s.append("\nошибка ").append(String.format(java.util.Locale.US, "%.1f", t.errDeg))
             .append("°   уставка ").append(String.format(java.util.Locale.US, "%.2f", t.w));
        return s.toString();
    }
}
