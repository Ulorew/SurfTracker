"""Негативные окна.

Список кадров с неполной разметкой ("не все паруса размечены") — забота
вызывающего кода (dataset_gen.py): такие кадры сюда просто не должны
передаваться. Здесь только геометрия одного кадра с полным списком боксов.
"""

import random

import config
from geometry import IntBox, Square, boxes_intersect, expand_box, resolve_placement


def _pick_negative_side(rng: random.Random) -> float:
    """Сторона окна-негатива — из того же распределения корзин, что и позитивы.

    Здесь нет цели, поэтому k неприменим: сторона берётся напрямую как
    случайное значение внутри выбранной (по долям) корзины — так фон получает
    то же разнообразие масштаба, что и позитивные окна.
    """
    lo, hi, _frac = rng.choices(
        config.SIZE_BINS, weights=[b[2] for b in config.SIZE_BINS], k=1
    )[0]
    return rng.uniform(lo, hi)


def _pick_center(frame_w: int, frame_h: int, side: float, horizon_y: float, rng: random.Random):
    cx = rng.uniform(0, frame_w)
    if rng.random() < config.HORIZON_PREFERENCE_PROB:
        band_half = config.HORIZON_BAND_HALF_HEIGHT_FRAC_OF_S * side
        cy = rng.uniform(horizon_y - band_half, horizon_y + band_half)
    else:
        cy = rng.uniform(0, frame_h)
    return cx, cy


def sample_negatives(
    frame_w: int,
    frame_h: int,
    boxes: "list[IntBox]",
    count: int,
    rng: random.Random,
    horizon_y: "float | None" = None,
) -> "list[Square]":
    """До `count` окон-негативов для кадра размера (frame_w, frame_h).

    `boxes` — ВСЕ размеченные рамки этого кадра (не только целевая). Окно
    отвергается, если задевает хотя бы одну рамку, расширенную на
    config.NEGATIVE_BOX_EXPAND_FRAC.
    """
    if horizon_y is None:
        horizon_y = config.DEFAULT_HORIZON_Y_FRAC * frame_h

    expanded = [expand_box(b, config.NEGATIVE_BOX_EXPAND_FRAC) for b in boxes]

    result = []
    for _ in range(count):
        for _attempt in range(config.NEGATIVE_MAX_ATTEMPTS_PER_SAMPLE):
            side = _pick_negative_side(rng)
            cx, cy = _pick_center(frame_w, frame_h, side, horizon_y, rng)
            # crop.py больше не паддинг, а реальные пиксели
            # (после пробных прогонов): реально вырезаемая область — это
            # placement.src_box (>= WINDOW_SIZE и прижата к границам кадра),
            # а не наивный квадрат (cx,cy,side). Проверять пересечение нужно
            # ровно по ней же — иначе "негатив" может утащить в кадр цель,
            # которая была за пределами side или появилась из-за сдвига при
            # прижатии к краю.
            candidate = resolve_placement(Square(cx=cx, cy=cy, side=side), frame_w, frame_h).src_box
            if not any(boxes_intersect(candidate, b) for b in expanded):
                result.append(Square(cx=cx, cy=cy, side=side))
                break
        # не нашли за отведённые попытки — просто недодаём этот негатив,
        # не зависаем в бесконечном цикле на плотно занятых кадрах
    return result
