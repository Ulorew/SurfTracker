import random

import config
from geometry import IntBox, Square, boxes_intersect, expand_box, resolve_placement
from negatives import sample_negatives


def _actually_captured_box(sq, frame_w, frame_h):
    """То, что crop() реально прочитает из кадра — не sq.side напрямую:
    без паддинга область захвата расширяется минимум до
    WINDOW_SIZE. Проверять пересечение с боксами нужно по НЕЙ, иначе тест
    (как и раньше сам код) может пропустить цель, которая была за пределами
    маленького sq.side, но внутри реально вырезаемых 640."""
    return resolve_placement(sq, frame_w, frame_h).src_box


def test_negatives_do_not_intersect_expanded_boxes():
    frame_w, frame_h = 1920, 1080
    boxes = [
        IntBox(200, 300, 260, 360),
        IntBox(800, 500, 900, 600),
        IntBox(1500, 700, 1560, 740),
    ]
    rng = random.Random(0)

    negatives = sample_negatives(frame_w, frame_h, boxes, count=50, rng=rng)

    assert len(negatives) > 0, "не сгенерировалось ни одного негатива — подозрительно"

    expanded = [expand_box(b, config.NEGATIVE_BOX_EXPAND_FRAC) for b in boxes]
    for sq in negatives:
        cand = _actually_captured_box(sq, frame_w, frame_h)
        for eb in expanded:
            assert not boxes_intersect(cand, eb), (
                f"реально вырезаемая область для негатива {sq} пересекает расширенный бокс {eb}"
            )


def test_negatives_stay_reasonable_with_dense_boxes():
    """Плотно занятый кадр — часть негативов не находится, но код не виснет
    и не возвращает пересекающиеся окна."""
    frame_w, frame_h = 640, 480
    boxes = [IntBox(x, y, x + 80, y + 80) for x in range(0, 640, 90) for y in range(0, 480, 90)]
    rng = random.Random(1)

    negatives = sample_negatives(frame_w, frame_h, boxes, count=20, rng=rng)

    expanded = [expand_box(b, config.NEGATIVE_BOX_EXPAND_FRAC) for b in boxes]
    for sq in negatives:
        cand = _actually_captured_box(sq, frame_w, frame_h)
        for eb in expanded:
            assert not boxes_intersect(cand, eb)


def test_negatives_prefer_horizon_band_statistically():
    """С HORIZON_PREFERENCE_PROB > 0 центры негативов должны заметно чаще
    попадать в полосу горизонта, чем при равномерном распределении по кадру."""
    frame_w, frame_h = 1920, 1080
    boxes = []
    rng = random.Random(2)
    horizon_y = frame_h * 0.5

    negatives = sample_negatives(frame_w, frame_h, boxes, count=300, rng=rng, horizon_y=horizon_y)
    assert len(negatives) >= 250

    near_band = sum(1 for sq in negatives if abs(sq.cy - horizon_y) < 0.15 * frame_h)
    # при равномерном распределении в полосе 0.15*H*2 ожидалось бы ~30% из
    # 1080 высоты; с горизонт-предпочтением должно быть заметно больше
    assert near_band / len(negatives) > 0.35
