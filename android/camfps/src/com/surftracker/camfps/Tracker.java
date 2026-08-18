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
 * держал окно в 2.5 размера цели. Выбор по уверенности перепрыгивает на
 * соседа при первом же случае, когда в окне не одна детекция, а k=2.5 режет
 * человека по пояс — детектор фрагмент туловища узнаёт хуже целого. Обе
 * причины видны из самой логики и не требуют ссылки на замер.
 *
 * ЧИСЛА ИЗ ПРОГОНА ПО full24.mp4 ОТСЮДА УБРАНЫ. Тот прогон шёл БЕЗ фильтра
 * классов и 56% тактов уверенно вёл мебель. Сравнивать «замороженный не терял
 * цель, телефон терял дважды» на материале, где «цель» — тумбочка, нельзя:
 * не терять неподвижный предмет легко, и к слежению за человеком это ничего
 * не говорит. Ссылки удалены, а не поправлены, потому что честного числа за
 * ними нет.
 *
 * ЧТО ЭТОТ ЭТАП ВОСПРОИЗВОДИТ. ПРАВИЛА, а не РЕШЕНИЯ. Правила сличены с
 * оригиналом потактово стендом tools/windowing/port_check: 328 тактов на
 * восьми сценариях, ноль расхождений по выбранной детекции, состоянию,
 * счётчику промахов и стороне окна, в обоих режимах прижатия центра. Это
 * означает «та же логика», а НЕ «тот же результат на воде»: офлайн-счёт
 * 0.841 снят с уровня 2 (Калман, гейт, теневые треки) и к этому файлу не
 * относится.
 *
 * ЧЕГО ЗДЕСЬ НЕТ и почему. Второй этап — это Калман и махаланобисов гейт, и
 * ТОЛЬКО они.
 *
 * ТЕНЕВЫЕ ТРЕКИ В ПЕРЕНОС НЕ ИДУТ. Они выключены в проде решением владельца
 * (коммит 3304aed, конфигурация tracking-v2) после пересчёта честной линейкой
 * с фиксированным знаменателем: состав БЕЗ теневых даёт долю тактов на цели
 * 0.736 против 0.604 и ведёт цель 96.4% тактов против 73.5%. Прежняя метрика
 * не включала отказ в знаменатель и показывала теневые в плюсе — отсюда и
 * число 0.841, которое здесь раньше стояло.
 *
 * Помимо счёта у них есть свойство конструкции: правило «чужие детекции
 * заняты» исходит из того, что текущий выбор верен, а когда он неверен —
 * цементирует ошибку. Именной сценарий error_lockin (1350 прогонов): после
 * одной ошибки петля возвращается к цели с вероятностью 0.50, и P(K=5) равно
 * P(K=15) до третьего знака — то есть ловушка поглощающая, время не помогает.
 * Хуже того, в 40% прогонов она захлопывалась ДО инжекции ошибки: обычный
 * пропуск отдаёт истинную детекцию в «свободные», и она заводит собственный
 * теневой.
 *
 * Удержание при перекрытии (ENABLE_OCCLUSION_HOLD) в проде тоже выключено.
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

    // --- МЕХАНИЗМ А: штраф и вето по несоответствию размера ---------------
    //
    // Перенос из офлайнового прода (ENABLE_SIZE_SCORING в tracking-v2). Здесь
    // ВЫКЛЮЧЕН по умолчанию: включение меняет решения петли, и оно должно быть
    // отдельным, измеренным шагом, а не побочным следствием переноса.
    //
    // Зачем он. Выбор идёт по близости к предсказанию, и размер в нём не
    // участвует вовсе. На прогоне 18 августа это дало две подмены цели:
    // такт 433 — выбрана полоска 45x320 (блик в стекле) при ведомом размере
    // 774; такт 719 — предмет 103x267 при ведомом 1244. Человек в обоих
    // случаях был найден моделью и стоял в кадре.
    //
    //   ratio = размер_кандидата / размер_фильтра
    //   ratio вне [1/1.8, 1.8]        -> ВЕТО, кандидат выбывает
    //   счёт += 0.5 * |log(ratio)| * сторона_окна
    //
    // Домножение на сторону окна — чтобы слагаемое было в тех же единицах,
    // что и расстояние; складывать безразмерный логарифм с пикселями нельзя.
    // На тактах 433 и 719 отношения 0.41 и 0.21, то есть оба отсекаются вето,
    // не доходя до счёта.
    public static boolean ENABLE_SIZE_SCORING = false;
    public static final double SIZE_LAMBDA = 0.5;      // SIZE_LAMBDA
    public static final double SIZE_VETO_RATIO = 1.8;  // SIZE_VETO_RATIO

    // --- УРОВЕНЬ 2: Калман и махаланобисов гейт ---------------------------
    //
    // Оба ВЫКЛЮЧЕНЫ по умолчанию и включаются раздельно: гейт без Калмана
    // бессмыслен (ковариацию брать неоткуда), а Калман без гейта — законная
    // конфигурация, и её надо уметь мерить отдельно.
    //
    // Состояние Калмана ЗЕРКАЛИТСЯ в cx/cy/vx/vy после каждой операции: эти
    // поля читают и петля, и стенд, и менять их смысл значило бы переписать
    // всё вокруг ради одной ветки.
    public static boolean ENABLE_KALMAN = false;
    public static boolean ENABLE_GATE = false;

    private final KalmanTracker kf = new KalmanTracker();

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
        if (ENABLE_KALMAN) {
            kf.seed(mx, my, size);
            mirror();
            initialized = true; status = TRACKING; missCount = 0;
            return;
        }
        cx = mx; cy = my; vx = 0; vy = 0;
        filteredSize = size;
        initialized = true;
        status = TRACKING;
        missCount = 0;
    }

    /** Позиция через dt секунд БЕЗ нового измерения. */
    public double predX(double dt) { return ENABLE_KALMAN ? kf.predX(dt) : cx + vx * dt; }
    public double predY(double dt) { return ENABLE_KALMAN ? kf.predY(dt) : cy + vy * dt; }

    /** Состояние Калмана -> публичные поля. Размер в этой ветке берётся ИЗ
     *  СОСТОЯНИЯ (log h), а не из медленной EMA: два источника одной величины
     *  рано или поздно разойдутся. */
    private void mirror() {
        cx = kf.cx(); cy = kf.cy(); vx = kf.vx(); vy = kf.vy();
        filteredSize = kf.size();
    }

    /**
     * Такт без измерения: предсказание становится состоянием, ПОТОМ затухает
     * модуль скорости.
     *
     * Порядок именно такой — сначала шаг полной скоростью, затем затухание.
     * Направление не меняется, меняется только модуль: без затухания
     * экстраполяция уносит окно тем дальше, чем дольше нет цели.
     */
    public void advance(double dt) {
        // Убеждение двигается ТОЛЬКО в ведении. В потере экстраполировать
        // некуда: последнее известное положение уже устарело, и слепой шаг
        // уносит окно тем дальше, чем дольше нет цели. В оригинале шаг стоит
        // внутри ветки TRACKING.
        if (status == TRACKING) {
            if (ENABLE_KALMAN) {
                kf.advance(dt, TAU_SEC);
                mirror();
            } else {
                cx = predX(dt); cy = predY(dt);
                if (dt > 0 && TAU_SEC > 0) {
                    double k = Math.exp(-dt / TAU_SEC);
                    vx *= k; vy *= k;
                }
            }
        }
        missCount++;
        if (missCount >= MISS_TO_LOST) status = LOST;
        clampBeliefToView();
    }

    /** Альфа-бета поверх ПРЕДСКАЗАННОЙ позиции, а не последней измеренной. */
    public void update(double mx, double my, double dt, double size) {
        // ПЕРЕ-ЗАТРАВКА при повторном захвате из потери — так в оригинале
        // (track_logic.py: при chosen != None и STATUS_LOST зовётся seed).
        //
        // Без неё альфа-бета строит невязку против устаревшего предсказания,
        // которое всё это время экстраполировалось вслепую. Проверено
        // запуском обеих реализаций: скорость получала выброс -931 пикс/с, а
        // через четыре такта цель терялась снова — то есть дефект
        // самоподдерживающийся.
        if (!initialized || status == LOST) { seed(mx, my, size); return; }
        if (ENABLE_KALMAN) {
            kf.update(mx, my, dt, size);
            mirror();
            missCount = 0;
            status = TRACKING;
            clampBeliefToView();
            return;
        }
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
        clampBeliefToView();
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
        // Пол применяется ДО расширения, как в оригинале
        // (current_window_side: max(k*size, min_window), затем expand).
        // В первой редакции переноса пол стоял ПОСЛЕ, и мелкая цель после
        // пяти промахов получала окно 704 вместо 1287, то есть радиус приёма
        // 211 вместо 386 — расширение при потере почти не работало.
        double base = Math.max(WINDOW_K * filteredSize, MIN_WINDOW_PX);
        int steps = Math.min(missCount, MAX_EXPAND_STEPS);
        double grown = (steps > 0) ? base * Math.pow(EXPAND_PER_MISS, steps) : base;
        return Math.min(grown, Math.min(frameW, frameH));
    }

    /**
     * Проекция ПОЗИЦИИ на видимую область; скорость не трогается.
     *
     * Перенесено из clamp_belief_to_view. В первой редакции переноса не было
     * вовсе, и прижималось лишь ВОЗВРАЩАЕМОЕ значение плана — а само
     * убеждение уезжало за кадр. Расхождение проявлялось уже в чистом
     * ведении, без всякой потери, и маскировалось тем, что план обеих версий
     * прижимался к одному числу.
     *
     * Почему позиция, а не только вырезка: приём кандидатов меряется от
     * предсказания, и центр, уехавший за кадр, отвергает всё, что реально
     * видно.
     */
    /**
     * Куда пускать ЦЕНТР ПРИЁМА (он же центр предсказания).
     *
     * true  — центр отходит от края на полокна, то есть вырезка целиком лежит
     *         в кадре. Это режим офлайнового трекера по умолчанию.
     * false — центр пускается до самого края кадра.
     *
     * Разница не косметическая, и она измерена. Приём кандидатов меряется от
     * центра радиусом 0.3 стороны, а полокна — это 0.5 стороны. При окне
     * 2160 (крупная цель рядом с камерой) центр насильно уезжает в середину
     * кадра, и цель у верхнего края оказывается вне радиуса приёма, будучи
     * полностью видимой. На стенде это 45 тактов промахов ПРИ ВИДИМОЙ цели и
     * две потери из восьми сценариев; в режиме false — ноль и ноль.
     *
     * Похоже, это и есть тот отказ в комнате, когда человек подошёл слишком
     * близко и был потерян: близко — значит крупная рамка, значит окно во всю
     * короткую сторону кадра.
     *
     * САМА ВЫРЕЗКА прижимается отдельно (clamp по cropX/cropY), поэтому в
     * режиме false картинка в модель по-прежнему приходит целиком из кадра —
     * освобождается только центр приёма.
     *
     * ПОЧЕМУ ЗДЕСЬ false, А В ОФЛАЙНЕ true. Офлайновый прогон по 8 роликам в
     * обоих режимах (output/matrix_clamp) развёл их на ОДИН такт из 165 по
     * on_target — то есть там выбор не решает ничего, и трогать замороженный
     * трекер ради шума незачем. Не решает он там ровно потому, что на воде
     * сёрфер далеко: рамка мелкая, окно мелкое, полокна — небольшой отступ.
     * Телефон же работает и вблизи, где окно упирается в короткую сторону
     * кадра, и там разница становится потерей цели.
     *
     * Расхождение версий тут не молчаливое: стенд tools/windowing/port_check
     * сличает перенос с офлайном в ОБОИХ режимах и обязан сходиться в обоих.
     */
    public static boolean VIEW_CLAMP_KEEPS_WINDOW_INSIDE = false;

    public void clampBeliefToView() { clampBeliefToView(windowSide()); }

    /** То же, но стороной ЭТОГО такта — как plan_window(side) в оригинале. */
    public void clampBeliefToView(double side) {
        cx = clampX(cx, side);
        cy = clampY(cy, side);
        // В ветке Калмана прижимается САМО состояние фильтра: офлайн делает
        // именно так, и без этого убеждение фильтра расходится с офлайновым.
        if (ENABLE_KALMAN && kf.initialized) kf.setPos(cx, cy);
    }

    // Отступ считается ОТ ЦЕНТРА кадра, а не от края — так же, как
    // _view_margins в оригинале. На краю это одно и то же, но окно шире
    // кадра развело бы версии: прижатие «от края» вытолкнуло бы центр
    // наружу, прижатие «от центра» ставит его ровно в середину.
    private double clampX(double x, double side) {
        double half = frameW / 2.0;
        double m = VIEW_CLAMP_KEEPS_WINDOW_INSIDE ? Math.max(0, half - side / 2) : half;
        return half + Math.max(-m, Math.min(m, x - half));
    }

    private double clampY(double y, double side) {
        double half = frameH / 2.0;
        double m = VIEW_CLAMP_KEEPS_WINDOW_INSIDE ? Math.max(0, half - side / 2) : half;
        return half + Math.max(-m, Math.min(m, y - half));
    }

    /** Куда смотреть на этом такте: (cx, cy) предсказания, прижатые к кадру. */
    public double planCx(double dt, double side) {
        return clampX((status == TRACKING) ? predX(dt) : cx, side);
    }

    public double planCy(double dt, double side) {
        return clampY((status == TRACKING) ? predY(dt) : cy, side);
    }

    /**
     * Подавление немаксимумов: из 8400 якорей выхода сети собрать РАЗНЫЕ цели.
     *
     * Без него телефон брал максимум по всем якорям, то есть ровно одну
     * детекцию, и выбирать было НЕ ИЗ ЧЕГО — а вся логика выбора построена на
     * том, что кандидатов несколько. Прежде здесь стояло «медианно четыре
     * кандидата в окне» со ссылкой на прогон по full24.mp4; число снято, потому
     * что тот прогон шёл без фильтра классов и считал кандидатами в том числе
     * мебель. Довод от этого не слабеет: с двумя людьми в кадре кандидатов
     * заведомо больше одного, и это видно глазами на записи комнатного теста.
     *
     * Жадное, по убыванию уверенности. Сортировка вставками: кандидатов после
     * порога единицы, а не тысячи, и заводить ради них общий сорт незачем.
     *
     * @param out [max][6] — cx, cy, size, ширина, высота, уверенность
     *            (координаты ТЕНЗОРА; уверенность безразмерна).
     *            Размер (наибольшая сторона) ведёт трекер: окно квадратное, и
     *            мерить его по одной стороне правильно. Ширина и высота нужны
     *            только разбору записи — нарисовать настоящую рамку человека,
     *            а не квадрат со стороной в рост. Обе, а не одна: «размер» это
     *            максимум, и по нему вторая сторона не восстанавливается.
     * @return число найденных
     */
    public static int nms(float[][] raw, int nAnchors, float[][] out, int max) {
        int n = 0;
        // отбор по порогу
        float[][] cand = new float[max * 8][5];
        int m = 0;
        // Переполнение ВЫТЕСНЯЕТ слабейшего, а не обрывает проход.
        //
        // Обрыв означал бы отбор по порядку якорей, а он растровый — то есть
        // предпочтение верху кропа. Наблюдённый максимум на 1632 реальных
        // изображениях — 144 надпороговых якоря при пределе 128, и там цель
        // уцелела; но запас тонкий и обнулится при снижении порога или смене
        // модели. Линейный поиск слабейшего срабатывает реже одного кадра из
        // тысячи и на такт ничего не стоит.
        for (int a = 0; a < nAnchors; a++) {
            float c = raw[4][a];
            if (c < DETECT_LOW_CONF) continue;
            int slot;
            if (m < cand.length) slot = m++;
            else {
                int weak = 0;
                for (int i = 1; i < m; i++) if (cand[i][4] < cand[weak][4]) weak = i;
                if (cand[weak][4] >= c) continue;
                slot = weak;
            }
            cand[slot][0] = raw[0][a]; cand[slot][1] = raw[1][a];
            cand[slot][2] = raw[2][a]; cand[slot][3] = raw[3][a]; cand[slot][4] = c;
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
            if (out[n].length > 4) { out[n][3] = cand[i][2]; out[n][4] = cand[i][3]; }
            // Уверенность — ШЕСТЫМ столбцом, если вызывающий его завёл.
            // Выбору цели она не нужна и в нём не участвует (правило переноса:
            // выбор по близости, а не по уверенности), но в лог идут ВСЕ
            // кандидаты такта, и без уверенности их потом не рассудить.
            if (out[n].length > 5) out[n][5] = cand[i][4];
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
    /** dt такта нужен гейту: ковариация предсказания зависит от него. */
    private double gateDt = 0.0;

    public int selectTarget(double[][] dets, int n, double predCx, double predCy,
                             double side) {
        double frac = (status == TRACKING) ? SELECT_MAX_DIST_FRAC : REACQ_MAX_DIST_FRAC;
        double maxDist = frac * side;
        int best = -1; double bestScore = Double.MAX_VALUE;
        for (int i = 0; i < n; i++) {
            double d = Math.hypot(dets[i][0] - predCx, dets[i][1] - predCy);
            // ГЕЙТ — ИНСТРУМЕНТ РЕЖИМА ВЕДЕНИЯ. В потере окно заморожено и
            // растёт по явному правилу, ковариация фильтра не обновляется, и
            // отбор там идёт по радиусу — офлайн делает ровно так
            // (uses_mahalanobis_gate требует STATUS_TRACKING).
            if (ENABLE_GATE && ENABLE_KALMAN && status == TRACKING) {
                // МАХАЛАНОБИСОВ ГЕЙТ вместо фиксированного радиуса: он сам
                // расширяется, когда фильтр не уверен (долгий пропуск), и сам
                // сужается на плотном треке. Ровно то, чего не хватило на
                // тактах 409 и 419 прогона 18 августа: уверенные детекции
                // (0.90 и 0.92) были отвергнуты радиусом, пока убеждение
                // уезжало.
                if (kf.gateDistance2(dets[i][0], dets[i][1], dets[i][2], gateDt)
                        > KalmanTracker.GATE_CHI2) continue;
            } else if (d > maxDist) continue;          // радиус приёма — как был
            double score = d;
            if (ENABLE_SIZE_SCORING && filteredSize > 0 && dets[i][2] > 0) {
                double ratio = dets[i][2] / filteredSize;
                if (ratio > SIZE_VETO_RATIO || ratio < 1.0 / SIZE_VETO_RATIO) continue;
                score += SIZE_LAMBDA * Math.abs(Math.log(ratio)) * side;
            }
            if (score < bestScore) { bestScore = score; best = i; }
        }
        return best;
    }

    /** Что случилось за такт. Поля — ровно те, по которым сличается перенос. */
    public static final class Tick {
        public int chosen = -1;      // индекс выбранной детекции или -1
        public double side;          // сторона окна, от которой мерился приём
        public double predCx, predCy;// центр плана (он же центр приёма)
        public double dist = Double.NaN;  // до предсказания, ДО обновления фильтра
        public double gate;          // радиус приёма на этом такте
        public int status;           // состояние ПОСЛЕ такта
        public int miss;             // промахов подряд ПОСЛЕ такта
    }

    /**
     * Такт целиком: план окна -> приём -> обновление или промах.
     *
     * Собран в один метод не для красоты. Пока эти четыре шага стояли в
     * TrackActivity, стенд сличения был вынужден повторять их у себя — и
     * сличал собственную копию логики с питоном, а боевой порядок вызовов не
     * проверял никто. Именно так прошлая проверка объявила «сошёлся», не имея
     * доступа ни к состоянию, ни к порядку.
     *
     * Порядок повторяет plan_window + step офлайнового TrackState: сторона
     * окна считается ПЕРВОЙ, убеждение прижимается к кадру ИМЕННО ЭТОЙ
     * стороной, и только потом строится предсказание и меряется приём.
     */
    public Tick step(double[][] dets, int n, double dt) {
        Tick t = new Tick();
        t.side = windowSide();
        clampBeliefToView(t.side);          // как plan_window: прижать, потом предсказывать
        t.predCx = planCx(dt, t.side);
        t.predCy = planCy(dt, t.side);
        t.gate = ((status == TRACKING) ? SELECT_MAX_DIST_FRAC : REACQ_MAX_DIST_FRAC) * t.side;
        gateDt = dt;
        t.chosen = selectTarget(dets, n, t.predCx, t.predCy, t.side);
        if (t.chosen >= 0) {
            t.dist = Math.hypot(dets[t.chosen][0] - t.predCx, dets[t.chosen][1] - t.predCy);
            update(dets[t.chosen][0], dets[t.chosen][1], dt, dets[t.chosen][2]);
        } else if (initialized) {
            advance(dt);
        }
        t.status = status;
        t.miss = missCount;
        return t;
    }
}
