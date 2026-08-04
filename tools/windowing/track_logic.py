"""Выбор цели + состояние трека (тикет "трекинг", п.2-4).

Один шаг петли — TrackState.step(dt, detections). Всё остальное (окно,
вызов модели, парсинг детекций из ultralytics-результата) — забота
track_run.py; здесь только геометрия/состояние, без ML/видео зависимостей —
легко тестировать и потенциально переносить.
"""

from typing import NamedTuple, Optional

from track_filters import dist, make_filter

STATUS_TRACKING = "tracking"
STATUS_LOST = "lost"


def select_target(pred_cx: float, pred_cy: float, detections: list, window_side: float,
                   max_dist_frac: float):
    """detections: список (x0,y0,x1,y1,conf) в координатах КАДРА.

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
    chosen: "Optional[tuple]"      # (x0,y0,x1,y1,conf) выбранной детекции, или None (промах)
    chosen_dist: "Optional[float]"  # расстояние предсказание<->выбранная детекция, px
    miss_count: int
    lost_transition: bool   # True на такте, где произошёл переход tracking->lost
    reacquired: bool        # True на такте повторного захвата (lost->tracking)


class TrackState:
    def __init__(self, cfg, init_cx: float, init_cy: float, init_size: float,
                 frame_w: int, frame_h: int):
        self.cfg = cfg
        self.filter = make_filter(cfg.FILTER_LEVEL, cfg.ALPHA_BETA_ALPHA, cfg.ALPHA_BETA_BETA)
        self.filter.seed(init_cx, init_cy)
        self.filtered_size = init_size
        self.miss_count = 0
        self.status = STATUS_TRACKING
        self.frame_w = frame_w
        self.frame_h = frame_h
        self.last_known_cx = init_cx
        self.last_known_cy = init_cy
        self.last_known_window_side = cfg.TRACK_WINDOW_K * init_size
        # точка заморозки окна в LOST — последнее ПРЕДСКАЗАНИЕ (тикет п.4), а
        # не последняя детекция: за N промахов до потери фильтр успевает
        # экстраполировать цель, и выбрасывать это движение — значит искать
        # там, где цель заведомо уже не находится.
        self.last_pred_cx = init_cx
        self.last_pred_cy = init_cy

    def current_window_side(self) -> float:
        """Сторона окна, которую РЕАЛЬНО увидит модель.

        Пол DETECT_MIN_WINDOW_PX — не косметика: crop() без паддинга всё
        равно вырежет столько реальных пикселей (см. tracking_config).
        Потолок — min(frame_w, frame_h), а не max: при стороне больше
        короткой стороны кадра resolve_placement выдаёт неквадратный src_box,
        и картинка приходит в модель анизотропно сплющенной — то есть в
        масштабе, которого не было в обучении.
        """
        if self.status == STATUS_TRACKING:
            base = max(self.cfg.TRACK_WINDOW_K * self.filtered_size,
                       self.cfg.DETECT_MIN_WINDOW_PX)
        else:
            base = max(self.last_known_window_side, self.cfg.DETECT_MIN_WINDOW_PX)
        grown = expand_window_side(base, self.miss_count, self.cfg.WINDOW_EXPAND_PER_MISS)
        return min(grown, min(self.frame_w, self.frame_h))

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
        """detections уже в координатах кадра (низкий conf, см. tracking_config).

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

        chosen, chosen_d = select_target(pred_cx, pred_cy, detections, ref_side, max_frac)
        lost_transition = False
        reacquired = False

        if chosen is not None:
            mx = (chosen[0] + chosen[2]) / 2.0
            my = (chosen[1] + chosen[3]) / 2.0
            m_size = max(chosen[2] - chosen[0], chosen[3] - chosen[1])

            if self.status == STATUS_LOST:
                # повторный захват — скорость/размер с нуля, старые не
                # отражают то, что было ВНЕ окна наблюдения всё это время.
                self.filter.seed(mx, my)
                self.filtered_size = m_size
                self.status = STATUS_TRACKING
                reacquired = True
            else:
                self.filter.update(mx, my, dt)
                self.filtered_size = update_size_filter(
                    self.filtered_size, m_size,
                    self.cfg.SIZE_FILTER_GROW_RATE, self.cfg.SIZE_FILTER_SHRINK_RATE)

            self.miss_count = 0
            self.last_known_cx, self.last_known_cy = self.filter.cx, self.filter.cy
            self.last_known_window_side = self.cfg.TRACK_WINDOW_K * self.filtered_size
            self.last_pred_cx, self.last_pred_cy = self.filter.cx, self.filter.cy
        else:
            self.miss_count += 1
            if self.status == STATUS_TRACKING:
                # экстраполяция по скорости (тикет п.3); в LOST — НЕ двигаем
                # (окно заморожено, п.4), только считаем пропуски дальше.
                self.filter.cx, self.filter.cy = pred_cx, pred_cy
                self.last_pred_cx, self.last_pred_cy = pred_cx, pred_cy
                if self.miss_count >= self.cfg.MISS_TO_LOST_N:
                    self.status = STATUS_LOST
                    lost_transition = True

        return TickResult(
            status=self.status if not lost_transition else STATUS_LOST,
            predicted_cx=pred_cx, predicted_cy=pred_cy, window_side=side,
            chosen=chosen, chosen_dist=chosen_d, miss_count=self.miss_count,
            lost_transition=lost_transition, reacquired=reacquired,
        )
