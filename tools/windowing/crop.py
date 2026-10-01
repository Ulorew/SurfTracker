"""crop(frame, square) -> 640x640. Один примитив, никакой другой логики.

Эта функция — кандидат на побитовое повторение на Kotlin, поэтому
вся арифметика явная и вынесена в geometry.py: округление там же
(round_half_up — не встроенный round()), интерполяция задаётся явно и
используется только для уменьшения. Паддинга нет
(решено после пробных прогонов): если реальной области меньше WINDOW_SIZE, geometry.resolve_placement
сам расширяет область захвата вместо докрашивания серым — на телефоне у
края кадра ровно так же придётся сдвигать окно захвата, а не рисовать вымысел.
"""

import numpy as np
import cv2

import config
from geometry import Square, resolve_placement


def crop(frame: np.ndarray, square: Square) -> np.ndarray:
    """Вырезает окрестность `square` из `frame` и приводит к config.WINDOW_SIZE^2.

    Правила:
      - реальной области меньше WINDOW_SIZE -> расширить область захвата
        (никогда не увеличивать растяжением/паддингом отдельные пиксели);
      - выход за границы кадра -> сдвинуть внутрь (не менять размер);
      - сторона исходной вырезки больше WINDOW_SIZE -> уменьшить (INTER_AREA);
      - кадр физически меньше WINDOW_SIZE по какой-то оси -> вырождённый
        случай, сжимаем до размера кадра (то же, что и раньше для
        избыточно большого квадрата) и, если совсем не повезло, добираем
        ресайзом — это единственный путь остаться на контракте 640x640.
    """
    frame_h, frame_w = frame.shape[0], frame.shape[1]
    target = config.WINDOW_SIZE

    placement = resolve_placement(square, frame_w, frame_h, target)
    ib = placement.src_box
    sub = frame[ib.y0:ib.y1, ib.x0:ib.x1]

    if sub.size == 0:
        # вырожденный случай (кадр нулевого размера по какой-то оси) —
        # практически недостижимо, но не падать же
        return np.zeros((target, target, 3), dtype=np.uint8)

    h, w = sub.shape[0], sub.shape[1]
    if h == target and w == target:
        return sub.copy()

    interp = config.DOWNSCALE_INTERPOLATION if (w >= target and h >= target) else cv2.INTER_LINEAR
    return cv2.resize(sub, (target, target), interpolation=interp)
