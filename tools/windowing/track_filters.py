"""Фильтры положения/скорости.

Уровни 0 и 1 используют один и тот же шаг экстраполяции на пропуск
(predict(dt) = позиция + скорость*dt) — разница только в том, КАК
обновляется состояние при получении измерения (update). Уровень 2 — Калман
(track_kalman.py), у него та же четвёрка методов, но
он дополнительно ведёт размер цели (log h) и умеет считать махаланобисов
гейт; здесь эти два метода — заглушки, чтобы петля не разбиралась, какой
фильтр ей достался.

Все величины — УГЛОВЫЕ (радианы): петля переведена в углы, см. angles.py.
"""

import math


class PositionFilter:
    """Общий интерфейс. Не инстанцировать напрямую — Level0Filter/AlphaBetaFilter."""

    def __init__(self, cfg=None):
        self.cfg = cfg
        self.cx = None
        self.cy = None
        self.vx = 0.0
        self.vy = 0.0
        self.initialized = False

    # размер цели этот фильтр не ведёт — им занимается медленная EMA в
    # TrackState (log h — только в Калман-ветке)
    size = None
    # ковариации нет — значит нет и махаланобисова гейта
    HAS_GATE = False

    def seed(self, mx: float, my: float, m_size: float = None) -> None:
        """Первое измерение — нет ни предыдущей позиции, ни скорости, чтобы
        строить невязку. Просто якоримся, скорость 0. m_size принимается для
        единого интерфейса с Калманом и не используется."""
        self.cx, self.cy = mx, my
        self.vx, self.vy = 0.0, 0.0
        self.initialized = True

    def predict(self, dt: float) -> "tuple[float, float]":
        """Позиция через dt секунд БЕЗ нового измерения (пропуск/экстраполяция)."""
        assert self.initialized, "predict() до первого seed()/update()"
        return (self.cx + self.vx * dt, self.cy + self.vy * dt)

    def advance(self, dt: float) -> None:
        """Такт без измерения: предсказание становится состоянием, после чего
        модуль скорости затухает.

        Порядок именно такой — сначала шаг полной скоростью, потом затухание:
        так задано постановкой ("домножение после predict"). Направление
        скорости не меняется, меняется только модуль.
        """
        self.cx, self.cy = self.predict(dt)
        self.decay_velocity(dt)

    def decay_velocity(self, dt: float) -> None:
        tau = getattr(self.cfg, "EXTRAPOLATION_TAU_SEC", None) if self.cfg else None
        if not tau or tau <= 0 or dt <= 0:
            return
        k = math.exp(-dt / tau)
        self.vx *= k
        self.vy *= k

    def gate_distance2(self, mx, my, m_size, dt):
        """У безковариационных фильтров махаланобисова гейта нет — петля
        падает обратно на фиксированный радиус."""
        return None

    def update(self, mx: float, my: float, dt: float,
                m_size: float = None) -> "tuple[float, float]":
        raise NotImplementedError


class Level0Filter(PositionFilter):
    """Уровень 0: позиция = последняя детекция как есть, скорость =
    разность двух последних детекций / dt. Никакого сглаживания."""

    def update(self, mx: float, my: float, dt: float,
                m_size: float = None) -> "tuple[float, float]":
        if not self.initialized:
            self.seed(mx, my)
            return (self.cx, self.cy)
        if dt > 0:
            self.vx = (mx - self.cx) / dt
            self.vy = (my - self.cy) / dt
        self.cx, self.cy = mx, my
        return (self.cx, self.cy)


class AlphaBetaFilter(PositionFilter):
    """Уровень 1: alpha по позиции, beta по скорости, поверх
    предсказанной (не последней) позиции — классический alpha-beta."""

    def __init__(self, alpha: float, beta: float, cfg=None):
        super().__init__(cfg)
        self.alpha = alpha
        self.beta = beta

    def update(self, mx: float, my: float, dt: float,
                m_size: float = None) -> "tuple[float, float]":
        if not self.initialized:
            self.seed(mx, my)
            return (self.cx, self.cy)

        pred_cx, pred_cy = self.predict(dt)
        rx, ry = mx - pred_cx, my - pred_cy

        self.cx = pred_cx + self.alpha * rx
        self.cy = pred_cy + self.alpha * ry
        if dt > 0:
            self.vx += (self.beta / dt) * rx
            self.vy += (self.beta / dt) * ry
        return (self.cx, self.cy)


def make_filter(cfg):
    """cfg — модуль/объект с FILTER_LEVEL и параметрами выбранного уровня.

    Принимаем cfg целиком, а не тройку чисел: у Калмана параметров десяток,
    и перечислять их в сигнатуре значит менять её при каждой правке модели.
    """
    level = cfg.FILTER_LEVEL
    if level == 0:
        return Level0Filter(cfg)
    if level == 1:
        return AlphaBetaFilter(cfg.ALPHA_BETA_ALPHA, cfg.ALPHA_BETA_BETA, cfg)
    if level == 2:
        from track_kalman import KalmanAngularFilter
        return KalmanAngularFilter(cfg)
    raise ValueError(f"неизвестный уровень фильтра: {level} (0, 1 или 2)")


def dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)
