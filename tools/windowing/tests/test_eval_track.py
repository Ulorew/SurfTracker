"""Юнит-тесty для новой логики eval_track.py (тикет "подготовка ночи",
патчи 1/3/4) — классификация детекций и ignore-зона, без модели."""
import config
import pytest

from eval_track import (CRITERIA, LEGACY_CRITERION, MAIN_CRITERION, center_inside,
                         classify_detection, radial_match, split_target_ignore)
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
                                        thr=config.EVAL_IOU_MATCH_THR, criterion="iou")
    assert kind == "primary"
    assert iou_val >= config.EVAL_IOU_MATCH_THR


def test_classify_detection_other_target_not_fp():
    primary = (100, 100, 200, 200)
    other = (500, 500, 600, 600)
    pred = (505, 505, 595, 595)  # совпадает с другим боксом, не с primary
    kind, _ = classify_detection(pred, primary, other_targets=[other], ignore_boxes=[],
                                  thr=config.EVAL_IOU_MATCH_THR, criterion="iou")
    assert kind == "other_target"


def test_classify_detection_ignore_via_center_inside_not_fp():
    primary = (100, 100, 200, 200)
    # ignore-бокс мелкий, реальная детекция крупнее — IoU будет низким,
    # должен сработать center_inside, а не строгий IoU.
    ignore_box = (498, 498, 502, 502)
    pred = (480, 480, 520, 520)  # крупная детекция, накрывает ignore-бокс целиком
    assert center_inside(pred, ignore_box)
    kind, _ = classify_detection(pred, primary, other_targets=[], ignore_boxes=[ignore_box],
                                  thr=config.EVAL_IOU_MATCH_THR, criterion="iou")
    assert kind == "ignore"


def test_classify_detection_genuine_fp():
    primary = (100, 100, 200, 200)
    other = (500, 500, 600, 600)
    ignore_box = (700, 700, 710, 710)
    pred = (300, 300, 350, 350)  # нигде рядом ни с чем
    kind, _ = classify_detection(pred, primary, other_targets=[other],
                                  ignore_boxes=[ignore_box], thr=config.EVAL_IOU_MATCH_THR, criterion="iou")
    assert kind == "fp"


# --- радиальный критерий (тикет "камерное зрение", блок В) ------------------
#
# Смысл замены: задача — НАВЕДЕНИЕ. «Попали в цель» физически значит «центр
# достаточно близок», а не «площади совпали». На вытянутой рамке паруса эти
# два вопроса дают разные ответы, и ниже это проверяется явно — иначе замена
# критерия была бы косметической.

RAD = config.EVAL_CENTER_HIT_THRESHOLD


class TestRadialMatch:
    def test_exact_center_gives_quality_one(self):
        gt = (100, 100, 200, 140)
        ok, q = radial_match(gt, gt, RAD)
        assert ok and q == pytest.approx(1.0)

    def test_size_is_the_LONGER_side(self):
        """Размер рамки — большая сторона, как везде в проекте. Считайся
        меньшая, радиус приёма у вытянутого паруса схлопнулся бы втрое."""
        gt = (0, 0, 300, 100)              # 300x100: центр (150,50)
        d = 120                             # 120 < 0.5*300, но > 0.5*100
        pred = (150 + d - 5, 45, 150 + d + 5, 55)
        assert radial_match(pred, gt, RAD)[0], "радиус посчитан по МЕНЬШЕЙ стороне"
        assert not radial_match(pred, gt, RAD * 100 / 300)[0], "тест не различает"

    def test_beyond_the_radius_is_not_a_match(self):
        gt = (0, 0, 100, 100)              # центр (50,50), радиус 50
        far = (50 + RAD * 100 + 20, 45, 50 + RAD * 100 + 30, 55)
        assert not radial_match(far, gt, RAD)[0]

    def test_quality_falls_to_zero_at_the_boundary(self):
        gt = (0, 0, 100, 100)
        r = RAD * 100
        on_edge = (50 + r - 0.01, 45, 50 + r + 0.01, 55)
        ok, q = radial_match(on_edge, gt, RAD)
        assert ok and q < 0.01

    def test_degenerate_box_never_matches(self):
        assert radial_match((0, 0, 10, 10), (5, 5, 5, 5), RAD) == (False, 0.0)


class TestCriteriaActuallyDisagree:
    """Различающая сила. Если бы радиальный и площадной критерии сходились на
    всех правдоподобных случаях, LEGACY-колонка была бы украшением, а замена
    ничего бы не меняла."""

    # Парус: рамка вытянутая (w/h ~ 0.3), предсказание того же центра, но
    # заметно ниже по высоте — типичный случай «поймал парус, но не доску».
    GT = (100, 100, 160, 300)          # 60x200, размер 200
    PRED = (105, 170, 155, 230)        # тот же центр, площадь втрое меньше

    def test_radial_calls_it_a_hit(self):
        kind, q = classify_detection(self.PRED, self.GT, [], [], RAD, "radial")
        assert kind == "primary" and q > 0.9

    def test_iou_calls_the_same_detection_a_false_alarm(self):
        kind, _ = classify_detection(self.PRED, self.GT, [], [],
                                      config.EVAL_IOU_MATCH_THR, "iou")
        assert kind == "fp", "критерии совпали — тогда замена ничего не меняет"


class TestCriterionAppliesToAllThreeRoles:
    """Критерий применяется и к соседу, и к ignore, а не только к цели: иначе
    детекция, попавшая в соседа по центру, но не по площади, осталась бы
    «ложной», и колонки считали бы разное разным способом."""

    GT = (100, 100, 160, 300)
    OTHER = (600, 100, 660, 300)
    PRED_ON_OTHER = (605, 170, 655, 230)

    def test_neighbour_is_not_a_false_alarm_under_radial(self):
        kind, _ = classify_detection(self.PRED_ON_OTHER, self.GT, [self.OTHER], [],
                                      RAD, "radial")
        assert kind == "other_target"

    def test_under_iou_the_same_neighbour_would_be_a_false_alarm(self):
        kind, _ = classify_detection(self.PRED_ON_OTHER, self.GT, [self.OTHER], [],
                                      config.EVAL_IOU_MATCH_THR, "iou")
        assert kind == "fp"


class TestCriteriaTable:
    def test_main_is_radial_and_legacy_is_iou(self):
        assert CRITERIA[MAIN_CRITERION][0] == "radial"
        assert CRITERIA[LEGACY_CRITERION][0] == "iou"

    def test_sensitivity_radii_are_present(self):
        """Чувствительность считается в том же прогоне: отдельный прогон
        отличался бы ещё и недетерминизмом инференса."""
        for f in config.EVAL_RADIAL_SENSITIVITY:
            assert f"radial_{f:g}" in CRITERIA

    def test_main_radius_matches_config(self):
        assert CRITERIA[MAIN_CRITERION][1] == config.EVAL_CENTER_HIT_THRESHOLD
