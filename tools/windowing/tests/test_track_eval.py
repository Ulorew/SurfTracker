"""track_eval.py — сплайн
(неравномерный Catmull-Rom по таймстампам) интерполяция GT, критерий
попадания, серии пропусков."""
import pytest

from track_eval import _catmull_rom_1d, compute_metrics, gt_box_at, is_hit, miss_streaks


class TestGtBoxAt:
    """Только 2 опорные точки — соседей для касательных нет, сплайн
    вырождается в линейную интерполяцию (см. _catmull_rom_1d)."""
    GT = [(0.0, 100.0, 100.0, 20.0, 20.0), (1.0, 200.0, 100.0, 40.0, 20.0)]

    def test_before_range_is_none(self):
        assert gt_box_at(self.GT, -0.1) is None

    def test_after_range_is_none(self):
        assert gt_box_at(self.GT, 1.1) is None

    def test_exact_keyframe(self):
        assert gt_box_at(self.GT, 0.0) == (100.0, 100.0, 20.0, 20.0)

    def test_midpoint_degenerates_to_linear_with_only_2_points(self):
        box = gt_box_at(self.GT, 0.5)
        assert box == pytest.approx((150.0, 100.0, 30.0, 20.0))

    def test_quarter_point(self):
        cx, cy, w, h = gt_box_at(self.GT, 0.25)
        assert cx == pytest.approx(125.0)
        assert w == pytest.approx(25.0)

    def test_empty_track_is_none(self):
        assert gt_box_at([], 0.5) is None


class TestCatmullRom1d:
    def test_two_points_only_matches_linear(self):
        # без t0/t3 (None) -> касательные = секущая p2-p1 -> ровно линейная
        v = _catmull_rom_1d(None, None, 0.0, 0.0, 10.0, 100.0, None, None, 3.0)
        assert v == pytest.approx(30.0)  # 0 + 0.3*100

    def test_passes_through_keyframes_exactly(self):
        # интерполяционное свойство сплайна: в самих узлах — точное значение
        assert _catmull_rom_1d(0.0, 0.0, 1.0, 10.0, 2.0, 20.0, 3.0, 40.0, 1.0) == pytest.approx(10.0)
        assert _catmull_rom_1d(0.0, 0.0, 1.0, 10.0, 2.0, 20.0, 3.0, 40.0, 2.0) == pytest.approx(20.0)

    def test_matches_hand_computed_value_with_both_neighbors(self):
        # t0=0,p0=0; t1=1,p1=10; t2=2,p2=20; t3=4,p3=40 — сверено вручную
        # m1=(p2-p0)/(t2-t0)*dt=(20-0)/(2-0)*1=10; m2=(p3-p1)/(t3-t1)*dt=(40-10)/(4-1)*1=10
        # u=0.5: h00=.5 h10=.125 h01=.5 h11=-.125 -> .5*10+.125*10+.5*20+(-.125)*10=5+1.25+10-1.25=15
        v = _catmull_rom_1d(0.0, 0.0, 1.0, 10.0, 2.0, 20.0, 4.0, 40.0, 1.5)
        assert v == pytest.approx(15.0)

    def test_uses_neighbors_differs_from_pure_pairwise_linear(self):
        """Не вырождается в линейную между p1,p2, когда соседи ЕСТЬ —
        подтверждает, что сплайн реально использует t0/t3, а не тихо падает
        обратно на линейную ("лучше интерполяцию посильнее")."""
        v_spline = _catmull_rom_1d(0.0, 0.0, 1.0, 10.0, 2.0, 20.0, 4.0, 40.0, 1.5)
        v_linear = 10.0 + 0.5 * (20.0 - 10.0)  # 15.0 — тут совпадает по симметрии, возьмём асимметричный случай
        v_spline_asym = _catmull_rom_1d(0.0, 0.0, 1.0, 10.0, 2.0, 20.0, 2.5, 15.0, 1.5)
        v_linear_asym = 10.0 + 0.5 * (20.0 - 10.0)
        assert v_spline_asym != pytest.approx(v_linear_asym)

    def test_zero_dt_returns_p1(self):
        assert _catmull_rom_1d(None, None, 1.0, 5.0, 1.0, 9.0, None, None, 1.0) == 5.0


class TestGtBoxAtWithThreeKeyframes:
    """3+ опорные точки — сплайн использует дальних соседей для касательных,
    траектория сглаживается вместо ломаной линии по манёврам."""
    # разворот на 90°: (0,0) -> (10,0) -> (10,10)
    GT = [(0.0, 0.0, 0.0, 10.0, 10.0), (1.0, 10.0, 0.0, 10.0, 10.0), (2.0, 10.0, 10.0, 10.0, 10.0)]

    def test_passes_through_all_keyframes_exactly(self):
        assert gt_box_at(self.GT, 0.0)[:2] == pytest.approx((0.0, 0.0))
        assert gt_box_at(self.GT, 1.0)[:2] == pytest.approx((10.0, 0.0))
        assert gt_box_at(self.GT, 2.0)[:2] == pytest.approx((10.0, 10.0))

    def test_midpoint_differs_from_naive_pairwise_linear(self):
        """На повороте сплайн (учитывает будущую точку через касательную)
        должен отличаться от чистой линейной между только двумя соседними
        точками — иначе апгрейд не имеет эффекта."""
        cx, cy, w, h = gt_box_at(self.GT, 0.5)
        naive_linear_cx, naive_linear_cy = 5.0, 0.0  # (0,0)-(10,0) пополам
        assert (cx, cy) != pytest.approx((naive_linear_cx, naive_linear_cy))


class TestIsHit:
    def test_center_dead_on_hits(self):
        assert is_hit(100, 100, (100, 100, 20, 20), scale=1.2)

    def test_center_just_outside_scaled_box_misses(self):
        # рамка 20x20 *1.2 -> полуширина 12; 13 снаружи
        assert not is_hit(113, 100, (100, 100, 20, 20), scale=1.2)

    def test_center_within_scaled_margin_hits(self):
        # ±12 по x при scale=1.2 (рамка 20x20, без scale допуск был бы ±10)
        # — 11 снаружи НЕ увеличенной рамки, но внутри увеличенной
        assert is_hit(111, 100, (100, 100, 20, 20), scale=1.2)

    def test_scale_1_uses_raw_box(self):
        assert is_hit(109, 100, (100, 100, 20, 20), scale=1.0)
        assert not is_hit(111, 100, (100, 100, 20, 20), scale=1.0)


class TestMissStreaks:
    def _rows(self, chosen_flags):
        return [{"chosen": (1, 1, 2, 2, 0.5) if c else None} for c in chosen_flags]

    def test_no_misses(self):
        assert miss_streaks(self._rows([True, True, True])) == []

    def test_single_streak_in_middle(self):
        assert miss_streaks(self._rows([True, False, False, True])) == [2]

    def test_streak_at_end_counted(self):
        assert miss_streaks(self._rows([True, False, False])) == [2]

    def test_multiple_streaks(self):
        flags = [True, False, True, False, False, False, True]
        assert miss_streaks(self._rows(flags)) == [1, 3]

    def test_all_misses_one_streak(self):
        assert miss_streaks(self._rows([False, False, False])) == [3]


class TestComputeMetrics:
    def test_margin_percentiles_computed_only_from_hits(self):
        rows = [
            {"chosen": (1, 1, 2, 2, 0.5), "chosen_dist": 10.0, "window_side": 100.0,
             "lost_transition": False, "reacquired": False, "predicted_cx": 0, "predicted_cy": 0,
             "timestamp_sec": 0.0},
            {"chosen": None, "chosen_dist": None, "window_side": 150.0,
             "lost_transition": False, "reacquired": False, "predicted_cx": 0, "predicted_cy": 0,
             "timestamp_sec": 0.33},
            {"chosen": (1, 1, 2, 2, 0.5), "chosen_dist": 20.0, "window_side": 100.0,
             "lost_transition": False, "reacquired": False, "predicted_cx": 0, "predicted_cy": 0,
             "timestamp_sec": 0.66},
        ]
        m = compute_metrics(rows, gt_track=[])
        assert m["n_margin_samples"] == 2  # только такты с chosen != None
        assert m["margin_frac_p50"] in (0.1, 0.2)  # 10/100 и 20/100

    def test_target_swaps_always_none(self):
        m = compute_metrics([], gt_track=[])
        assert m["n_target_swaps"] is None

    def test_no_gt_leaves_in_window_fraction_none(self):
        rows = [{"chosen": None, "chosen_dist": None, "window_side": 100.0,
                  "lost_transition": False, "reacquired": False,
                  "predicted_cx": 0, "predicted_cy": 0, "timestamp_sec": 0.0}]
        m = compute_metrics(rows, gt_track=[])
        assert m["in_window_fraction"] is None

    def test_with_gt_in_window_fraction_reflects_hits(self):
        gt = [(0.0, 100.0, 100.0, 20.0, 20.0), (1.0, 100.0, 100.0, 20.0, 20.0)]
        rows = [
            {"chosen": None, "chosen_dist": None, "window_side": 100.0,
             "lost_transition": False, "reacquired": False,
             "predicted_cx": 100, "predicted_cy": 100, "timestamp_sec": 0.0},  # hit
            {"chosen": None, "chosen_dist": None, "window_side": 100.0,
             "lost_transition": False, "reacquired": False,
             "predicted_cx": 500, "predicted_cy": 500, "timestamp_sec": 1.0},  # miss
        ]
        m = compute_metrics(rows, gt_track=gt)
        assert m["in_window_fraction"] == pytest.approx(0.5)
