"""Юнит-тесty для новой логики eval_track.py (тикет "подготовка ночи",
патчи 1/3/4) — классификация детекций и ignore-зона, без модели."""
import config
from eval_track import center_inside, classify_detection, split_target_ignore
from geometry import IntBox


def test_split_target_ignore_by_min_target_size():
    small = IntBox(0, 0, config.MIN_TARGET_SIZE - 1, config.MIN_TARGET_SIZE - 1)
    big = IntBox(0, 0, config.MIN_TARGET_SIZE, config.MIN_TARGET_SIZE)
    targets, ignore = split_target_ignore([small, big])
    assert targets == [big]
    assert ignore == [small]


def test_classify_detection_primary_match():
    primary = (100, 100, 200, 200)
    pred = (105, 105, 195, 195)  # почти то же самое -> высокий IoU
    kind, iou_val = classify_detection(pred, primary, other_targets=[], ignore_boxes=[],
                                        iou_thr=config.EVAL_IOU_MATCH_THR)
    assert kind == "primary"
    assert iou_val >= config.EVAL_IOU_MATCH_THR


def test_classify_detection_other_target_not_fp():
    primary = (100, 100, 200, 200)
    other = (500, 500, 600, 600)
    pred = (505, 505, 595, 595)  # совпадает с другим боксом, не с primary
    kind, _ = classify_detection(pred, primary, other_targets=[other], ignore_boxes=[],
                                  iou_thr=config.EVAL_IOU_MATCH_THR)
    assert kind == "other_target"


def test_classify_detection_ignore_via_center_inside_not_fp():
    primary = (100, 100, 200, 200)
    # ignore-бокс мелкий, реальная детекция крупнее — IoU будет низким,
    # должен сработать center_inside, а не строгий IoU.
    ignore_box = (498, 498, 502, 502)
    pred = (480, 480, 520, 520)  # крупная детекция, накрывает ignore-бокс целиком
    assert center_inside(pred, ignore_box)
    kind, _ = classify_detection(pred, primary, other_targets=[], ignore_boxes=[ignore_box],
                                  iou_thr=config.EVAL_IOU_MATCH_THR)
    assert kind == "ignore"


def test_classify_detection_genuine_fp():
    primary = (100, 100, 200, 200)
    other = (500, 500, 600, 600)
    ignore_box = (700, 700, 710, 710)
    pred = (300, 300, 350, 350)  # нигде рядом ни с чем
    kind, _ = classify_detection(pred, primary, other_targets=[other],
                                  ignore_boxes=[ignore_box], iou_thr=config.EVAL_IOU_MATCH_THR)
    assert kind == "fp"
