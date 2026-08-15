package com.surftracker.camfps;

/**
 * Петля слежения: перенос замороженного офлайн-трекера (тег tracking-v1-frozen)
 * на телефон, ЭТАП 1 — ядро.
 *
 * Источник: tools/windowing/track_logic.py, track_filters.py,
 * tracking_config.py. Имена и константы сохранены, чтобы перенос можно было
 * сличать с оригиналом, а не сверять по памяти.
 *
 * ЗАЧЕМ ПЕРЕНОС, а не своя логика. Телефон выбирал СИЛЬНЕЙШУЮ детекцию и
 * держал окно в 2.5 размера цели. Прогон замороженного трекера по нашей
 * собственной записи (full24.mp4) показал разницу:
 *   - в окне медианно ЧЕТЫРЕ кандидата, максимум девять — то есть модель
 *     находит в комнате не только цель, и выбор по уверенности перепрыгивает
 *     на соседа при первом же случае;
 *   - замороженный не потерял цель ни разу за 280 тактов, телефон на том же
 *     материале потерял дважды;
 *   - медианная сторона окна у замороженного 2160 пикселей (вся высота
 *     кадра), у телефона 640-1440: при k=2.5 человек резался по пояс, и
 *     детектор фрагмент туловища не узнавал.
 *
 * ЧЕГО ЗДЕСЬ НЕТ и почему. Теневые треки, махаланобисов гейт и удержание при
 * перекрытии — второй этап. Они подняли офлайн-счёт с 0.734 до 0.841, но
 * несут задокументированный риск закрепления ошибки (вероятность возврата
 * после одной ошибки 0.50), и переносить их надо отдельно, с тем же
 * мутационным контролем, что был офлайн.
 *
 * Прод-фильтр офлайн — Калман (FILTER_LEVEL=2), но его ковариация нужна ТОЛЬКО
 * гейту. Здесь альфа-бета (уровень 1): предсказание то же, а разница вступает
 * в силу лишь вместе с гейтом второго этапа.
 *
 * Всё в ПИКСЕЛЯХ СЕНСОРА. Офлайн-петля живёт в углах, потому что там кадры
 * разных клипов с разной оптикой; здесь камера одна, и лишний перевод только
 * добавил бы места для ошибки масштаба — которая в этом проекте уже стоила
 * четверти коэффициента петли.
 */
public final class Tracker {

    // --- константы, перенесены из tracking_config.py ---
    public static final double WINDOW_K = 3.5;              // TRACK_WINDOW_K
    public static final double SIZE_GROW = 0.5;             // SIZE_FILTER_GROW_RATE
    public static final double SIZE_SHRINK = 0.1;           // SIZE_FILTER_SHRINK_RATE
    public static final double SELECT_MAX_DIST_FRAC = 0.30; // TARGET_SELECT_MAX_DIST_FRAC
    public static final double REACQ_MAX_DIST_FRAC = 0.30;  // REACQUIRE_MAX_DIST_FRAC
    public static final double TAU_SEC = 1.5;               // EXTRAPOLATION_TAU_SEC
    public static final double EXPAND_PER_MISS = 1.15;      // WINDOW_EXPAND_PER_MISS
    public static final int    MISS_TO_LOST = 5;            // MISS_TO_LOST_N
    public static final double ALPHA = 0.6, BETA = 0.3;     // ALPHA_BETA_*
    public static final int    MIN_WINDOW_PX = 640;         // DETECT_MIN_WINDOW_PX
    /** Столько подряд расширений хватает, чтобы упереться в кадр. Без предела
     *  затяжная потеря переполняет само возведение в степень. */
    public static final int MAX_EXPAND_STEPS = 64;

    /** Порог приёма детекции. НИЗКИЙ намеренно: уверенность в выборе цели не
     *  участвует, её дело — отсечь мусор, а не ранжировать кандидатов.
     *  Офлайн: DETECT_LOW_CONF. */
    public static final float DETECT_LOW_CONF = 0.08f;
    /** Порог подавления немаксимумов по пересечению. */
    public static final double NMS_IOU = 0.45;

    public static final int TRACKING = 0, LOST = 1;

    public int status = LOST;
    public double cx, cy, vx, vy;      // состояние фильтра, пиксели сенсора
    public double filteredSize;        // медленная EMA размера цели
    public int missCount;
    public boolean initialized;

    private final int frameW, frameH;

    public Tracker(int frameW, int frameH) {
        this.frameW = frameW;
        this.frameH = frameH;
    }

    /** Первое измерение: якоримся, скорость ноль. Строить невязку не из чего. */
    public void seed(double mx, double my, double size) {
        cx = mx; cy = my; vx = 0; vy = 0;
        filteredSize = size;
        initialized = true;
        status = TRACKING;
        missCount = 0;
    }

    /** Позиция через dt секунд БЕЗ нового измерения. */
    public double predX(double dt) { return cx + vx * dt; }
    public double predY(double dt) { return cy + vy * dt; }

    /**
     * Такт без измерения: предсказание становится состоянием, ПОТОМ затухает
     * модуль скорости.
     *
     * Порядок именно такой — сначала шаг полной скоростью, затем затухание.
     * Направление не меняется, меняется только модуль: без затухания
     * экстраполяция уносит окно тем дальше, чем дольше нет цели.
     */
    public void advance(double dt) {
        cx = predX(dt); cy = predY(dt);
        if (dt > 0 && TAU_SEC > 0) {
            double k = Math.exp(-dt / TAU_SEC);
            vx *= k; vy *= k;
        }
        missCount++;
        if (missCount >= MISS_TO_LOST) status = LOST;
    }

    /** Альфа-бета поверх ПРЕДСКАЗАННОЙ позиции, а не последней измеренной. */
    public void update(double mx, double my, double dt, double size) {
        if (!initialized) { seed(mx, my, size); return; }
        double px = predX(dt), py = predY(dt);
        double rx = mx - px, ry = my - py;
        cx = px + ALPHA * rx;
        cy = py + ALPHA * ry;
        if (dt > 0) {
            vx += (BETA / dt) * rx;
            vy += (BETA / dt) * ry;
        }
        filteredSize = updateSizeFilter(filteredSize, size);
        missCount = 0;
        status = TRACKING;
    }

    /**
     * Асимметричная EMA размера: растёт быстро, падает медленно.
     *
     * Несимметрия намеренная: большое окно стоит точности, маленькое — потери
     * цели. Ошибаться выгоднее в сторону «больше».
     */
    public static double updateSizeFilter(double filtered, double measured) {
        double diff = measured - filtered;
        double rate = diff > 0 ? SIZE_GROW : SIZE_SHRINK;
        return filtered + rate * diff;
    }

    /** Сторона окна на этот такт, с расширением на каждый подряд идущий промах. */
    public double windowSide() {
        double side = WINDOW_K * filteredSize;
        int steps = Math.min(missCount, MAX_EXPAND_STEPS);
        if (steps > 0) side *= Math.pow(EXPAND_PER_MISS, steps);
        // Пол пиксельный: кроп всё равно не берёт меньше входа модели, и окно
        // с меньшей номинальной стороной — фикция, из-за которой порог выбора
        // цели оказывался строже задуманного.
        side = Math.max(MIN_WINDOW_PX, side);
        return Math.min(side, Math.min(frameW, frameH));
    }

    /** Куда смотреть на этом такте: (cx, cy) предсказания, прижатые к кадру. */
    public double planCx(double dt, double side) {
        double x = (status == TRACKING) ? predX(dt) : cx;
        double half = side / 2;
        return Math.max(half, Math.min(frameW - half, x));
    }

    public double planCy(double dt, double side) {
        double y = (status == TRACKING) ? predY(dt) : cy;
        double half = side / 2;
        return Math.max(half, Math.min(frameH - half, y));
    }

    /**
     * Подавление немаксимумов: из 8400 якорей выхода сети собрать РАЗНЫЕ цели.
     *
     * Без него телефон брал максимум по всем якорям, то есть ровно одну
     * детекцию, и выбирать было не из чего — а прогон замороженного трекера по
     * нашей записи показал медианно четыре кандидата в окне и максимум девять.
     * Без подавления те же четыре пришли бы как четыре сотни якорей одной и
     * той же цели.
     *
     * Жадное, по убыванию уверенности. Сортировка вставками: кандидатов после
     * порога единицы, а не тысячи, и заводить ради них общий сорт незачем.
     *
     * @param out [max][3] — заполняется cx, cy, size в координатах ТЕНЗОРА
     * @return число найденных
     */
    public static int nms(float[][] raw, int nAnchors, float[][] out, int max) {
        int n = 0;
        // отбор по порогу
        float[][] cand = new float[max * 8][5];
        int m = 0;
        for (int a = 0; a < nAnchors && m < cand.length; a++) {
            float c = raw[4][a];
            if (c < DETECT_LOW_CONF) continue;
            cand[m][0] = raw[0][a]; cand[m][1] = raw[1][a];
            cand[m][2] = raw[2][a]; cand[m][3] = raw[3][a]; cand[m][4] = c;
            m++;
        }
        // по убыванию уверенности
        for (int i = 1; i < m; i++) {
            float[] key = cand[i];
            int j = i - 1;
            while (j >= 0 && cand[j][4] < key[4]) { cand[j + 1] = cand[j]; j--; }
            cand[j + 1] = key;
        }
        boolean[] dead = new boolean[m];
        for (int i = 0; i < m && n < max; i++) {
            if (dead[i]) continue;
            out[n][0] = cand[i][0]; out[n][1] = cand[i][1];
            out[n][2] = Math.max(cand[i][2], cand[i][3]);
            n++;
            for (int j = i + 1; j < m; j++) {
                if (!dead[j] && iou(cand[i], cand[j]) > NMS_IOU) dead[j] = true;
            }
        }
        return n;
    }

    private static double iou(float[] a, float[] b) {
        double ax0 = a[0] - a[2] / 2, ax1 = a[0] + a[2] / 2;
        double ay0 = a[1] - a[3] / 2, ay1 = a[1] + a[3] / 2;
        double bx0 = b[0] - b[2] / 2, bx1 = b[0] + b[2] / 2;
        double by0 = b[1] - b[3] / 2, by1 = b[1] + b[3] / 2;
        double ix = Math.max(0, Math.min(ax1, bx1) - Math.max(ax0, bx0));
        double iy = Math.max(0, Math.min(ay1, by1) - Math.max(ay0, by0));
        double inter = ix * iy;
        double uni = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter;
        return uni <= 0 ? 0 : inter / uni;
    }

    /**
     * Выбор цели: БЛИЖАЙШАЯ К ПРЕДСКАЗАНИЮ среди тех, что не дальше
     * порога от него. Уверенность в выборе НЕ участвует вовсе — она уже
     * сыграла свою роль на входе, отсеяв мусор.
     *
     * Это главное отличие от прежней телефонной логики. Выбор по уверенности
     * перепрыгивает на соседнюю цель, как только та окажется крупнее или
     * контрастнее, а на воде сёрферов несколько.
     *
     * @param dets [n][3]: cx, cy, size — уже в пикселях сенсора
     * @return индекс выбранной или -1
     */
    public int selectTarget(double[][] dets, int n, double predCx, double predCy,
                             double side) {
        double frac = (status == TRACKING) ? SELECT_MAX_DIST_FRAC : REACQ_MAX_DIST_FRAC;
        double maxDist = frac * side;
        int best = -1; double bestD = Double.MAX_VALUE;
        for (int i = 0; i < n; i++) {
            double d = Math.hypot(dets[i][0] - predCx, dets[i][1] - predCy);
            if (d <= maxDist && d < bestD) { bestD = d; best = i; }
        }
        return best;
    }
}
