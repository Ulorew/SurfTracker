"""Общая арифметика координат, используемая crop() и семплерами окон.

Округление здесь — не деталь реализации, а часть контракта: результат должен
совпадать с тем, что даст `Math.round` в Kotlin (round-half-up), а не с
банковским округлением Python (`round()` округляет 0.5 к чётному).
"""

import math
from typing import NamedTuple, Optional

import config


class Square(NamedTuple):
    """Квадрат в координатах кадра, float. cx/cy — центр."""
    cx: float
    cy: float
    side: float


class IntBox(NamedTuple):
    """Целочисленный бокс в пикселях кадра, полуоткрытый интервал [x0,x1)x[y0,y1)."""
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0


def round_half_up(x: float) -> int:
    """floor(x + 0.5) — так же, как Kotlin/Java Math.round(Double/Float).

    Python-овский round() округляет 0.5 к чётному числу — не то, что нужно
    для побитового совпадения с реализацией на телефоне.
    """
    return math.floor(x + 0.5)


def square_to_int_box(square: Square) -> IntBox:
    """Целочисленный квадрат из float-центра/стороны.

    Ширина и высота считаются ОДИН раз (round(side)) и применяются к обеим
    осям — не через независимое округление x0 и x1 по отдельности, чтобы не
    получить прямоугольник off-by-one вместо квадрата.
    """
    side_int = round_half_up(square.side)
    half = square.side / 2.0
    x0 = round_half_up(square.cx - half)
    y0 = round_half_up(square.cy - half)
    return IntBox(x0, y0, x0 + side_int, y0 + side_int)


def clamp_box_to_frame(box: IntBox, frame_w: int, frame_h: int) -> IntBox:
    """Прижимает квадрат к границам кадра, не меняя его сторону (сдвигом).

    Если сторона больше самого кадра по какой-то оси — обрезается до размера
    кадра (это уже не сдвиг, а деградация; на практике не должно происходить,
    т.к. допустимые S ограничены K_MAX и разметкой, но не падать же).
    """
    w, h = box.w, box.h

    if w >= frame_w:
        x0, x1 = 0, frame_w
    else:
        x0 = min(max(box.x0, 0), frame_w - w)
        x1 = x0 + w

    if h >= frame_h:
        y0, y1 = 0, frame_h
    else:
        y0 = min(max(box.y0, 0), frame_h - h)
        y1 = y0 + h

    return IntBox(x0, y0, x1, y1)


def expand_box(box: IntBox, frac: float) -> IntBox:
    """Расширяет бокс на frac от каждой стороны (для проверки пересечений с негативами)."""
    dw = round_half_up(box.w * frac)
    dh = round_half_up(box.h * frac)
    return IntBox(box.x0 - dw, box.y0 - dh, box.x1 + dw, box.y1 + dh)


def boxes_intersect(a: IntBox, b: IntBox) -> bool:
    return a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1


def box_center(box: IntBox) -> "tuple[float, float]":
    return ((box.x0 + box.x1) / 2.0, (box.y0 + box.y1) / 2.0)


def box_size(box: IntBox) -> "tuple[int, int]":
    return (box.w, box.h)


class Placement(NamedTuple):
    """Как square ложится в холст WINDOW_SIZE^2 — общая математика для
    crop.py (рисует холст) и dataset_gen.py (пересчитывает координаты
    остальных боксов в тот же холст). Одна реализация, чтобы они не разошлись.
    """
    src_box: IntBox     # что вырезается из кадра (после клампа к границам)
    scale_x: float      # src_box.w -> WINDOW_SIZE
    scale_y: float      # src_box.h -> WINDOW_SIZE
    off_x: int          # смещение вставки в холсте по x (когда без ресайза)
    off_y: int          # смещение вставки в холсте по y (когда без ресайза)


def resolve_placement(square: Square, frame_w: int, frame_h: int,
                       window_size: "Optional[int]" = None) -> Placement:
    """Без паддинга (тикет "пробные прогоны", правка 1): никогда не берём
    меньше window_size РЕАЛЬНЫХ пикселей кадра — вместо докрашивания серым
    расширяем область захвата. Сдвигаем внутрь кадра (clamp_box_to_frame),
    сжимаем сторону до размера кадра только если сам запрошенный квадрат
    физически больше кадра. Та же логика понадобится на телефоне для цели
    у края.

    off_x/off_y всегда 0 теперь (поле оставлено для обратной совместимости
    с dataset_gen.py/eval_640.py — раньше означало сдвиг вставки в холст
    при паддинге).
    """
    if window_size is None:
        window_size = config.WINDOW_SIZE

    side = max(square.side, window_size)
    src_box = clamp_box_to_frame(square_to_int_box(Square(square.cx, square.cy, side)),
                                  frame_w, frame_h)
    w, h = src_box.w, src_box.h

    if w == 0 or h == 0:
        return Placement(src_box, 1.0, 1.0, 0, 0)

    return Placement(src_box, window_size / w, window_size / h, 0, 0)
