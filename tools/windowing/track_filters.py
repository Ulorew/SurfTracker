"""Фильтры положения/скорости (тикет "трекинг", п.3).

Оба уровня используют один и тот же шаг экстраполяции на пропуск
(predict(dt) = позиция + скорость*dt) — разница только в том, КАК
обновляется состояние при получении измерения (update). Уровень выше
(Калман) сюда сознательно не добавлен: "отдельное решение, не по умолчанию".
"""

import math


class PositionFilter:
    """Общий интерфейс. Не инстанцировать напрямую — Level0Filter/AlphaBetaFilter."""

    def __init__(self):
        self.cx = None
        self.cy = None
        self.vx = 0.0
        self.vy = 0.0
        self.initialized = False

    def seed(self, mx: float, my: float) -> None:
        """Первое измерение — нет ни предыдущей позиции, ни скорости, чтобы
        строить невязку. Просто якоримся, скорость 0."""
        self.cx, self.cy = mx, my
        self.vx, self.vy = 0.0, 0.0
        self.initialized = True

    def predict(self, dt: float) -> "tuple[float, float]":
        """Позиция через dt секунд БЕЗ нового измерения (пропуск/экстраполяция)."""
        assert self.initialized, "predict() до первого seed()/update()"
        return (self.cx + self.vx * dt, self.cy + self.vy * dt)

    def update(self, mx: float, my: float, dt: float) -> "tuple[float, float]":
        raise NotImplementedError


class Level0Filter(PositionFilter):
    """Тикет, уровень 0: позиция = последняя детекция как есть, скорость =
    разность двух последних детекций / dt. Никакого сглаживания."""

    def update(self, mx: float, my: float, dt: float) -> "tuple[float, float]":
        if not self.initialized:
            self.seed(mx, my)
            return (self.cx, self.cy)
        if dt > 0:
            self.vx = (mx - self.cx) / dt
            self.vy = (my - self.cy) / dt
        self.cx, self.cy = mx, my
        return (self.cx, self.cy)


class AlphaBetaFilter(PositionFilter):
    """Тикет, уровень 1: alpha по позиции, beta по скорости, поверх
    предсказанной (не последней) позиции — классический alpha-beta."""

    def __init__(self, alpha: float, beta: float):
        super().__init__()
        self.alpha = alpha
        self.beta = beta

    def update(self, mx: float, my: float, dt: float) -> "tuple[float, float]":
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


def make_filter(level: int, alpha: float, beta: float) -> PositionFilter:
    if level == 0:
        return Level0Filter()
    if level == 1:
        return AlphaBetaFilter(alpha, beta)
    raise ValueError(f"неизвестный уровень фильтра: {level} (0 или 1; Kalman — отдельное решение)")


def dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)
