"""Выбор цели + состояние трека (тикет "трекинг", п.2-4).

Один шаг петли — TrackState.step(dt, detections). Всё остальное (окно,
вызов модели, парсинг детекций из ultralytics-результата) — забота
track_run.py; здесь только геометрия/состояние, без ML/видео зависимостей —
легко тестировать и потенциально переносить.
"""

import math
from typing import NamedTuple, Optional

from track_filters import dist, make_filter
from track_score import FORM_DISTANCE, candidate_score
from track_shadows import ShadowSet

STATUS_TRACKING = "tracking"
STATUS_LOST = "lost"


def _det_center(det):
    return ((det[0] + det[2]) / 2.0, (det[1] + det[3]) / 2.0)


def _det_size(det):
    return max(det[2] - det[0], det[3] - det[1])


def occlusion_triggered(candidates: list, window_side: float, proximity_frac: float) -> bool:
    """Механизм Б (тикет "подмены v2", п.3): два и более кандидата сошлись
    ближе proximity_frac стороны окна друг к другу.

    Проверяется по кандидатам, прошедшим ТОЛЬКО дистанционный отбор, ДО вето
    механизма А — иначе вето по размеру съедает второго кандидата, и само
    пересечение, ради обнаружения которого правило и заведено, становится
    невидимым (явное требование тикета).
    """
    if len(candidates) < 2:
        return False
    thr = proximity_frac * window_side
    for i in range(len(candidates)):
        cxi, cyi = _det_center(candidates[i])
        for j in range(i + 1, len(candidates)):
            cxj, cyj = _det_center(candidates[j])
            if dist(cxi, cyi, cxj, cyj) <= thr:
                return True
    return False


def score_candidate(det, pred_cx, pred_cy, pred_size, prev_cx, prev_cy,
                     vel, dt, window_side, cfg):
    """-> (счёт в угловых единицах, вето: bool). Меньший счёт лучше.

    Базовый счёт — угловое расстояние до предсказания (правило тикета п.2: НЕ
    по уверенности). Механизмы А и В добавляют слагаемые, приведённые к тем
    же радианам, иначе складывать их с расстоянием нельзя.
    """
    dcx, dcy = _det_center(det)
    score = dist(pred_cx, pred_cy, dcx, dcy)
    vetoed = False

    if getattr(cfg, "ENABLE_SIZE_SCORING", False) and pred_size > 0:
        ratio = _det_size(det) / pred_size
        if ratio > 0:
            r_veto = cfg.SIZE_VETO_RATIO
            if ratio > r_veto or ratio < 1.0 / r_veto:
                vetoed = True
            # безразмерный |log(отношение)| домножается на сторону окна,
            # чтобы слагаемое было в радианах, как и расстояние
            score += cfg.SIZE_LAMBDA * abs(math.log(ratio)) * window_side

    # ВНИМАНИЕ: механизм В в формулировке тикета ИЗБЫТОЧЕН. Предсказание
    # строится как pred = prev + v*dt, поэтому
    #   |implied_v - v| = |cand - prev - v*dt| / dt = |cand - pred| / dt,
    # то есть "отклонение подразумеваемой скорости" тождественно равно
    # расстоянию до предсказания с точностью до множителя 1/dt (проверено
    # численно, расхождение ~1e-12). Слагаемое и вето механизма В повторяют
    # базовое слагаемое счёта и дистанционный гейт, новой информации не
    # добавляя — поэтому по умолчанию он выключен, а решение о его настоящей
    # форме (например, штраф именно за РАЗВОРОТ движения, а не за модуль
    # отклонения) оставлено человеку.
    #
    # vel=None означает "оценки скорости ещё нет" (первый такт после захвата
    # или реакквизиции). Гейт в этот момент СЛЕП: допуск вырождается в один
    # шум, и первая же честно сдвинувшаяся детекция получает вето — цель
    # теряется на ровном месте. Поэтому механизм включается только с такта,
    # когда скорость уже оценена по принятому измерению.
    if getattr(cfg, "ENABLE_VELOCITY_GATE", False) and dt > 0 and vel is not None:
        # какая мгновенная скорость потребовалась бы, прими мы этого кандидата
        implied_vx = (dcx - prev_cx) / dt
        implied_vy = (dcy - prev_cy) / dt
        delta = dist(implied_vx, implied_vy, vel[0], vel[1])
        allowed = (cfg.VELOCITY_GATE_FACTOR * math.hypot(vel[0], vel[1])
                   + cfg.VELOCITY_GATE_NOISE_ANG_PER_SEC)
        if delta > cfg.VELOCITY_VETO_MULT * allowed:
            vetoed = True
        excess = max(0.0, delta - allowed)
        # скорость * dt = угол, снова приводим к единицам расстояния
        score += cfg.VELOCITY_LAMBDA * excess * dt

    # В-НАПРАВЛЕННЫЙ: смотрим только на НАПРАВЛЕНИЕ, не на модуль. Именно
    # этим он не повторяет дистанционное слагаемое: расстояние ничего не
    # знает о том, вперёд кандидат или назад, а разворот за один такт для
    # сёрфера физически невозможен.
    if getattr(cfg, "ENABLE_VELOCITY_DIRECTION", False) and vel is not None and dt > 0:
        vmag = math.hypot(vel[0], vel[1])
        ux, uy = dcx - prev_cx, dcy - prev_cy
        umag = math.hypot(ux, uy)
        # порог "движение вообще видно" — сигма измерения положения, а не
        # подобранное число: ниже неё направление это шум детектора
        min_move = cfg.VDIR_MIN_MOVE_SIZE_FRAC * pred_size
        if vmag * dt > min_move and umag > min_move:
            cos = (ux * vel[0] + uy * vel[1]) / (umag * vmag)
            if cos < cfg.VDIR_COS_VETO:
                vetoed = True
            # (1-cos)/2 из [0,1] -> в стороны окна, как и слагаемое А
            score += cfg.VDIR_LAMBDA * (1.0 - cos) / 2.0 * window_side

    return score, vetoed


def select_target(pred_cx: float, pred_cy: float, detections: list, window_side: float,
                   max_dist_frac: float):
    """detections: список (theta0,phi0,theta1,phi1,conf,...) в УГЛАХ.

    Ближайшая к предсказанию по центру, среди тех, что не дальше
    max_dist_frac*window_side. НЕ по уверенности (тикет п.2) — уверенность
    вообще не участвует в выборе, только в том, что детекция дошла с
    инференса (низкий conf там, см. tracking_config.DETECT_LOW_CONF).

    -> (детекция|None, расстояние|None)
    """
    max_dist = max_dist_frac * window_side
    best, best_d = None, None
    for det in detections:
        x0, y0, x1, y1 = det[0], det[1], det[2], det[3]
        dcx, dcy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        d = dist(pred_cx, pred_cy, dcx, dcy)
        if d <= max_dist and (best_d is None or d < best_d):
            best, best_d = det, d
    return best, best_d


def update_size_filter(filtered_size: float, measured_size: float,
                        grow_rate: float, shrink_rate: float) -> float:
    """Асимметричная EMA (тикет п.1): растёт быстро, падает медленно."""
    diff = measured_size - filtered_size
    rate = grow_rate if diff > 0 else shrink_rate
    return filtered_size + rate * diff


MAX_EXPAND_STEPS = 64  # хватает, чтобы упереться в кадр при любом разумном
                        # множителе; без этого затяжная потеря (сотни тактов
                        # подряд) переполняет само возведение в степень ещё
                        # до того, как сработает ограничение кадром.


def expand_window_side(base_side: float, miss_count: int, expand_mult: float) -> float:
    """Множитель на КАЖДЫЙ подряд идущий пропуск (тикет п.3): base*(mult^miss)."""
    return base_side * (expand_mult ** min(miss_count, MAX_EXPAND_STEPS))


class TickResult(NamedTuple):
    status: str
    predicted_cx: float
    predicted_cy: float
    window_side: float
    chosen: "Optional[tuple]"      # выбранная угловая детекция, или None (промах)
    chosen_dist: "Optional[float]"  # угловое расстояние предсказание<->выбранная детекция
    miss_count: int
    lost_transition: bool   # True на такте, где произошёл переход tracking->lost
    reacquired: bool        # True на такте повторного захвата (lost->tracking)
    occluded: bool = False  # такт прошёл в режиме окклюзии (механизм Б)
    n_candidates: int = 0   # кандидатов, прошедших отбор (радиус ИЛИ гейт)
    n_vetoed: int = 0       # из них отвергнуто вето механизмов А/В
    n_candidates_radius: int = 0        # сколько прошло бы фиксированный радиус
    n_candidates_gate: "Optional[int]" = None  # сколько прошло гейт (None = гейт не работал)
    n_taken_by_shadows: int = 0         # кандидатов отброшено как занятые чужим треком
    n_shadows: int = 0                  # теневых треков живо после такта


class TrackState:
    """Состояние трека ЦЕЛИКОМ в угловых единицах (тикет "ночь", п.2.0).

    Позиция, скорость, размер цели, сторона окна и все гейты — радианы.
    Пиксели остаются снаружи: track_run переводит детекции в углы на входе и
    угол окна обратно в пиксели на выходе (вырезка и оверлей).

    min_window/max_window тоже угловые: прежние DETECT_MIN_WINDOW_PX и
    размеры кадра — величины пиксельные, и оставить их здесь значило бы
    протащить пиксели внутрь.
    """

    def __init__(self, cfg, init_cx: float, init_cy: float, init_size: float,
                 min_window: float, max_window: float):
        self.cfg = cfg
        self.filter = make_filter(cfg)
        self.filter.seed(init_cx, init_cy, init_size)
        # Медленная EMA размера — только для фильтров, которые сами размер не
        # ведут (уровни 0/1). У Калмана размер живёт в состоянии (log h), и
        # дублирующая EMA поверх него была бы вторым, несогласованным
        # источником той же величины.
        self._ema_size = init_size
        self.miss_count = 0
        self.status = STATUS_TRACKING
        self.min_window = min_window
        self.max_window = max_window
        self.last_known_cx = init_cx
        self.last_known_cy = init_cy
        self.last_known_window_side = cfg.TRACK_WINDOW_K * init_size
        # точка заморозки окна в LOST — последнее ПРЕДСКАЗАНИЕ (тикет п.4), а
        # не последняя детекция: за N промахов до потери фильтр успевает
        # экстраполировать цель, и выбрасывать это движение — значит искать
        # там, где цель заведомо уже не находится.
        self.last_pred_cx = init_cx
        self.last_pred_cy = init_cy
        # сколько тактов ещё держать окклюзионную паузу (механизм Б)
        self.occlusion_hold = 0
        # пауза истекла, а кандидаты всё ещё вместе: не даём ей перезапуститься
        # немедленно, иначе трекер зависает в паузе навсегда, пока сёрферы
        # идут рядом. Снимается, как только кандидаты разошлись хоть раз.
        self.occlusion_latched = False
        # оценка скорости появляется только после ПРИНЯТОГО измерения; до
        # этого механизм В обязан молчать (см. score_candidate)
        self.velocity_ready = False
        # теневые треки: заняты соседями, чтобы те не притягивали цель
        self.shadows = ShadowSet(cfg) if getattr(cfg, "ENABLE_SHADOW_TRACKS", False) else None

    @property
    def filtered_size(self) -> float:
        """Угловой размер цели: из состояния фильтра, если он его ведёт
        (Калман, log h), иначе из медленной EMA."""
        own = self.filter.size
        return self._ema_size if own is None else own

    def uses_mahalanobis_gate(self) -> bool:
        """Гейт — инструмент режима ВЕДЕНИЯ. В потере (п.4 тикета) окно
        заморожено и растёт по явному правилу, а ковариация фильтра не
        обновляется; отбор там идёт по радиусу окна, как и раньше."""
        return (self.status == STATUS_TRACKING
                and getattr(self.cfg, "ENABLE_MAHALANOBIS_GATE", False)
                and getattr(self.filter, "HAS_GATE", False))

    def current_window_side(self) -> float:
        """Угловая сторона окна, которую РЕАЛЬНО увидит модель.

        Пол min_window — угловой эквивалент того, что crop() без паддинга
        всё равно вырежет не меньше WINDOW_SIZE пикселей. Потолок
        max_window — угловой эквивалент КОРОТКОЙ стороны кадра: при большей
        стороне вырезка становится неквадратной и картинка приходит в
        модель анизотропно сплющенной.
        """
        if self.status == STATUS_TRACKING:
            base = max(self.cfg.TRACK_WINDOW_K * self.filtered_size, self.min_window)
        else:
            base = max(self.last_known_window_side, self.min_window)
        grown = expand_window_side(base, self.miss_count, self.cfg.WINDOW_EXPAND_PER_MISS)
        return min(grown, self.max_window)

    def plan_window(self, dt: float) -> "tuple[float, float, float]":
        """Куда смотреть на ЭТОМ такте -> (cx, cy, сторона).

        Центр в TRACKING — экстраполяция фильтра на dt вперёд: это шаг 5
        петли из тикета ("предсказание позиции на следующий такт"). Вызывающий
        (track_run) обязан строить окно именно отсюда, иначе окно отстаёт на
        целый такт движения цели, а на быстрой цели этого хватает, чтобы
        она вышла из зоны приёма и захват уехал на соседнего сёрфера.
        """
        side = self.current_window_side()
        if self.status == STATUS_TRACKING:
            cx, cy = self.filter.predict(dt)
        else:
            cx, cy = self.last_pred_cx, self.last_pred_cy
        return cx, cy, side

    def step(self, dt: float, detections: list) -> TickResult:
        """detections уже переведены в углы (низкий conf, см. tracking_config).

        dt тот же, что был передан в plan_window для этого такта — окно и
        решение о цели обязаны считаться от одной и той же точки.
        """
        pred_cx, pred_cy, side = self.plan_window(dt)

        if self.status == STATUS_TRACKING:
            max_frac = self.cfg.TARGET_SELECT_MAX_DIST_FRAC
        else:
            max_frac = self.cfg.REACQUIRE_MAX_DIST_FRAC
        # Порог считается от ТЕКУЩЕЙ стороны окна — в том числе раздувшейся в
        # LOST. Иначе (от дотрековой стороны) радиус приёма 0.3*S всегда лежит
        # внутри полуширины 0.5*S НЕрасширенного окна, и расширение окна в
        # LOST не может принять ничего нового — весь механизм п.4 мёртв.
        ref_side = side

        # --- отбор кандидатов: дистанция/гейт -> окклюзия -> счёт с вето ----
        max_dist = max_frac * ref_side
        by_radius = [d for d in detections
                     if dist(pred_cx, pred_cy, *_det_center(d)) <= max_dist]
        gated = None
        if self.uses_mahalanobis_gate():
            gated = []
            for d in detections:
                dcx, dcy = _det_center(d)
                d2 = self.filter.gate_distance2(dcx, dcy, _det_size(d), dt)
                if d2 <= self.cfg.KALMAN_GATE_CHI2:
                    gated.append(d)
        # Оба списка считаются ВСЕГДА, когда гейт включён: тикет требует
        # показать, сколько кандидатов режет гейт против фиксированного
        # радиуса, а задним числом по логу это не восстановить.
        candidates = by_radius if gated is None else gated

        # Теневые: кандидат, до которого чужому треку ближе, чем 0.7 от
        # расстояния до цели, выбывает — он уже занят (тикет п.2). Считается
        # ДО окклюзии и вето: занятый кандидат не должен ни выбираться, ни
        # создавать видимость пересечения.
        n_taken = 0
        if self.shadows is not None and self.shadows.tracks:
            kept = []
            for d in candidates:
                if self.shadows.is_taken(d, pred_cx, pred_cy, dt):
                    n_taken += 1
                else:
                    kept.append(d)
            candidates = kept

        occluded = False
        n_vetoed = 0
        if getattr(self.cfg, "ENABLE_OCCLUSION_HOLD", False):
            triggered = occlusion_triggered(candidates, ref_side,
                                             self.cfg.OCCLUSION_PROXIMITY_FRAC)
            if not triggered:
                # кандидаты разошлись — и пауза, и запрет на неё сняты
                self.occlusion_hold = 0
                self.occlusion_latched = False
            elif self.occlusion_hold > 0:
                occluded = True
                self.occlusion_hold -= 1
                if self.occlusion_hold == 0:
                    # пауза истекла, а они всё ещё вместе: дальше выбираем,
                    # иначе зависнем в паузе на весь совместный проход
                    self.occlusion_latched = True
            elif not self.occlusion_latched:
                # вход в паузу: на пересечении любой выбор — монетка, а
                # ошибка необратима, поэтому M тактов идём экстраполяцией
                occluded = True
                self.occlusion_hold = self.cfg.OCCLUSION_HOLD_TICKS - 1
                if self.occlusion_hold == 0:
                    self.occlusion_latched = True

        if occluded:
            chosen, chosen_d = None, None
        elif getattr(self.cfg, "SCORE_FORM", FORM_DISTANCE) != FORM_DISTANCE:
            # Махаланобисовы формы (тикет "счёт кандидата", п.4): выбор по
            # МИНИМУМУ счёта, а не по минимуму расстояния. Механизмы А/В сюда
            # не подмешиваются — их роль в этих формах играют ковариация
            # (размер как третья координата) и направленный член формы 3.
            best, best_score = None, None
            for d in candidates:
                sc = candidate_score(d, self.filter, pred_cx, pred_cy,
                                      self.filtered_size, dt, self.cfg,
                                      velocity_ready=self.velocity_ready)
                if best_score is None or sc < best_score:
                    best, best_score = d, sc
            chosen = best
            chosen_d = None if best is None else dist(pred_cx, pred_cy, *_det_center(best))
        elif (getattr(self.cfg, "ENABLE_SIZE_SCORING", False)
                or getattr(self.cfg, "ENABLE_VELOCITY_GATE", False)
                or getattr(self.cfg, "ENABLE_VELOCITY_DIRECTION", False)):
            pred_size = self.filtered_size
            vel = (self.filter.vx, self.filter.vy) if self.velocity_ready else None
            scored = []
            for d in candidates:
                sc, veto = score_candidate(d, pred_cx, pred_cy, pred_size,
                                            self.filter.cx, self.filter.cy,
                                            vel, dt, ref_side, self.cfg)
                if veto:
                    n_vetoed += 1
                else:
                    scored.append((sc, d))
            if scored:
                scored.sort(key=lambda x: x[0])
                chosen = scored[0][1]
                chosen_d = dist(pred_cx, pred_cy, *_det_center(chosen))
            else:
                chosen, chosen_d = None, None
        else:
            # Ближайший к предсказанию из УЖЕ отобранных (радиусом или
            # гейтом). Для радиуса это ровно select_target; отдельным вызовом
            # по всем детекциям он был бы обходом гейта.
            chosen, chosen_d = None, None
            for d in candidates:
                dd = dist(pred_cx, pred_cy, *_det_center(d))
                if chosen_d is None or dd < chosen_d:
                    chosen, chosen_d = d, dd

        lost_transition = False
        reacquired = False

        if chosen is not None:
            mx = (chosen[0] + chosen[2]) / 2.0
            my = (chosen[1] + chosen[3]) / 2.0
            m_size = max(chosen[2] - chosen[0], chosen[3] - chosen[1])

            if self.status == STATUS_LOST:
                # повторный захват — скорость/размер с нуля, старые не
                # отражают то, что было ВНЕ окна наблюдения всё это время.
                self.filter.seed(mx, my, m_size)
                self._ema_size = m_size
                self.status = STATUS_TRACKING
                reacquired = True
            else:
                self.filter.update(mx, my, dt, m_size)
                if self.filter.size is None:
                    self._ema_size = update_size_filter(
                        self._ema_size, m_size,
                        self.cfg.SIZE_FILTER_GROW_RATE, self.cfg.SIZE_FILTER_SHRINK_RATE)

            self.miss_count = 0
            self.velocity_ready = self.status == STATUS_TRACKING and not reacquired
            self.last_known_cx, self.last_known_cy = self.filter.cx, self.filter.cy
            self.last_known_window_side = self.cfg.TRACK_WINDOW_K * self.filtered_size
            self.last_pred_cx, self.last_pred_cy = self.filter.cx, self.filter.cy
        elif occluded:
            # Пауза окклюзии — это НЕ пропуск: цель видна, просто неотличима
            # от соседа. Счётчик промахов не трогаем (иначе пауза уводит в
            # потерю), окно не расширяем (тикет п.3) — только экстраполируем.
            if self.status == STATUS_TRACKING:
                self.filter.advance(dt)
                self.last_pred_cx, self.last_pred_cy = pred_cx, pred_cy
        else:
            self.miss_count += 1
            if self.status == STATUS_TRACKING:
                # экстраполяция по скорости (тикет п.3); в LOST — НЕ двигаем
                # (окно заморожено, п.4), только считаем пропуски дальше.
                # advance, а не присваивание координат: у Калмана такт без
                # измерения обязан ещё и нарастить ковариацию на Q, иначе
                # гейт остаётся узким там, где фильтр уже ничего не знает.
                self.filter.advance(dt)
                self.last_pred_cx, self.last_pred_cy = pred_cx, pred_cy
                if self.miss_count >= self.cfg.MISS_TO_LOST_N:
                    self.status = STATUS_LOST
                    lost_transition = True

        if self.shadows is not None:
            # Питание и рождение — ПОСЛЕ выбора цели: иначе теневой
            # сопоставился бы с той самой детекцией, которую цель только
            # собирается взять, и тут же её у себя занял.
            vel = (self.filter.vx, self.filter.vy) if self.velocity_ready else None
            self.shadows.step(dt, detections, chosen, target_vel=vel)

        return TickResult(
            status=self.status if not lost_transition else STATUS_LOST,
            predicted_cx=pred_cx, predicted_cy=pred_cy, window_side=side,
            chosen=chosen, chosen_dist=chosen_d, miss_count=self.miss_count,
            lost_transition=lost_transition, reacquired=reacquired,
            occluded=occluded, n_candidates=len(candidates), n_vetoed=n_vetoed,
            n_candidates_radius=len(by_radius),
            n_candidates_gate=None if gated is None else len(gated),
            n_taken_by_shadows=n_taken,
            n_shadows=0 if self.shadows is None else len(self.shadows),
        )
