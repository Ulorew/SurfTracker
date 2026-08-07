"""geometry.py — контракт для побитового Kotlin-порта (см. докстринг модуля):
round-half-up, а не банковское округление Python; тесты здесь фиксируют
именно граничные/половинные случаи, которые тише всего расходятся между
реализациями. Нет отдельного файла для этого модуля не было — эти функции
использует весь пайплайн (crop.py, sample_window.py, negatives.py,
dataset_gen.py, track_window.py) и не имели прямого юнит-покрытия."""
import pytest

from geometry import (
    IntBox, Placement, Square, boxes_intersect, box_center, box_size,
    clamp_box_to_frame, expand_box, resolve_placement, round_half_up,
    square_to_int_box,
)


class TestRoundHalfUp:
    """floor(x+0.5) — Kotlin/Java Math.round(Double), НЕ Python round()."""

    @pytest.mark.parametrize("x,expected", [
        (0.5, 1), (1.5, 2), (2.5, 3), (-0.5, 0), (-1.5, -1), (-2.5, -2),
    ])
    def test_half_integers_round_up_not_to_even(self, x, expected):
        # Python round() даёт 0,2,2,0,-2,-2 (банковское) — здесь д.б. иначе.
        assert round_half_up(x) == expected

    @pytest.mark.parametrize("x,expected", [
        (1.4, 1), (1.6, 2), (-1.4, -1), (-1.6, -2), (0.0, 0), (10.0, 10),
    ])
    def test_regular_values(self, x, expected):
        assert round_half_up(x) == expected

    def test_returns_int_type(self):
        assert isinstance(round_half_up(1.5), int)


class TestSquareToIntBox:
    def test_side_computed_once_yields_true_square(self):
        """Ширина/высота считаются из одного round(side), не из независимого
        round(x0)/round(x1) — иначе на нечётных side можно получить
        прямоугольник off-by-one вместо квадрата."""
        box = square_to_int_box(Square(cx=100.0, cy=100.0, side=101.0))
        assert box.w == box.h == round_half_up(101.0)

    def test_center_preserved_within_rounding(self):
        box = square_to_int_box(Square(cx=50.0, cy=50.0, side=20.0))
        assert box == IntBox(40, 40, 60, 60)

    def test_half_pixel_center_uses_round_half_up(self):
        # cx-half = 50.5 - 10 = 40.5 -> round_half_up -> 41 (не 40, не банковское)
        box = square_to_int_box(Square(cx=50.5, cy=50.5, side=20.0))
        assert box.x0 == 41 and box.y0 == 41


class TestClampBoxToFrame:
    def test_inside_frame_unchanged(self):
        box = IntBox(10, 10, 50, 50)
        assert clamp_box_to_frame(box, 100, 100) == box

    def test_shifts_inward_past_right_edge_keeps_side(self):
        box = IntBox(90, 10, 130, 50)  # w=40, вылезает за x=100
        out = clamp_box_to_frame(box, 100, 100)
        assert out.w == 40
        assert out.x1 == 100

    def test_shifts_inward_past_left_edge_keeps_side(self):
        box = IntBox(-20, 10, 20, 50)  # w=40
        out = clamp_box_to_frame(box, 100, 100)
        assert out.w == 40
        assert out.x0 == 0

    def test_shifts_inward_past_bottom_edge_keeps_side(self):
        """Зеркало горизонтальным случаям. Мутация `frame_h - h` -> `frame_h + h`
        переживала ВСЕ 427 тестов: вертикальный прижим к краю не был покрыт
        ничем, хотя горизонтальный двойник покрыт двумя тестами."""
        box = IntBox(10, 90, 50, 130)   # h=40, вылезает за y=100
        out = clamp_box_to_frame(box, 100, 100)
        assert out.h == 40
        assert out.y1 == 100

    def test_shifts_inward_past_top_edge_keeps_side(self):
        box = IntBox(10, -20, 50, 20)   # h=40
        out = clamp_box_to_frame(box, 100, 100)
        assert out.h == 40
        assert out.y0 == 0

    def test_side_larger_than_frame_degrades_to_full_frame(self):
        box = IntBox(-50, -50, 250, 250)
        out = clamp_box_to_frame(box, 100, 80)
        assert out == IntBox(0, 0, 100, 80)

    def test_negative_center_clamps_to_corner(self):
        box = square_to_int_box(Square(cx=-10000, cy=-10000, side=100))
        out = clamp_box_to_frame(box, 1000, 800)
        assert out.x0 == 0 and out.y0 == 0 and out.w == 100 and out.h == 100


class TestDegenerateBox:
    def test_zero_width_box_yields_neutral_placement(self):
        """Вырожденный бокс (нулевой по ОДНОЙ оси) обязан уходить в ту же
        безопасную ветку, что и нулевой по обеим: масштаб window/0 — это
        деление на ноль. Мутант `or` -> `and` на этой строке выживал."""
        pl = resolve_placement(Square(cx=0, cy=50, side=0), 100, 100, 640)
        if pl.src_box.w == 0 or pl.src_box.h == 0:
            assert (pl.scale_x, pl.scale_y) == (1.0, 1.0)


class TestExpandBox:
    def test_expands_symmetrically_by_fraction(self):
        box = IntBox(100, 100, 200, 150)  # w=100, h=50
        out = expand_box(box, 0.1)
        assert out == IntBox(90, 95, 210, 155)

    def test_zero_fraction_is_noop(self):
        box = IntBox(10, 10, 20, 20)
        assert expand_box(box, 0.0) == box


class TestBoxesIntersect:
    def test_overlapping(self):
        assert boxes_intersect(IntBox(0, 0, 10, 10), IntBox(5, 5, 15, 15))

    @pytest.mark.parametrize("other", [
        IntBox(10, 0, 20, 10),    # справа вплотную
        IntBox(-10, 0, 0, 10),    # слева вплотную
        IntBox(0, 10, 10, 20),    # снизу вплотную
        IntBox(0, -10, 10, 0),    # сверху вплотную
    ])
    def test_touching_edges_not_intersecting(self, other):
        """Полуоткрытый интервал [x0,x1). Проверяются ВСЕ четыре стороны:
        тест на одной убивал одно сравнение из четырёх, три мутанта
        `<` -> `<=` переживали прогон."""
        assert not boxes_intersect(IntBox(0, 0, 10, 10), other)

    def test_disjoint(self):
        assert not boxes_intersect(IntBox(0, 0, 10, 10), IntBox(20, 20, 30, 30))

    def test_one_inside_other(self):
        assert boxes_intersect(IntBox(0, 0, 100, 100), IntBox(40, 40, 60, 60))


class TestBoxCenterAndSize:
    def test_box_center(self):
        assert box_center(IntBox(0, 0, 10, 20)) == (5.0, 10.0)

    def test_box_size_returns_wh_tuple(self):
        assert box_size(IntBox(0, 0, 30, 10)) == (30, 10)


class TestResolvePlacement:
    def test_no_padding_expands_undersized_square_to_window(self):
        """Контракт "без паддинга" (тикет "пробные прогоны"): запрошенная
        сторона меньше window_size -> реально вырезаемая область всё равно
        ровно window_size (расширение захвата, не докраска)."""
        placement = resolve_placement(Square(cx=500, cy=500, side=50), 2000, 1600, window_size=640)
        assert placement.src_box.w == 640 and placement.src_box.h == 640
        assert placement.scale_x == pytest.approx(1.0)
        assert placement.scale_y == pytest.approx(1.0)

    def test_oversized_square_downscales(self):
        placement = resolve_placement(Square(cx=500, cy=500, side=1280), 2000, 1600, window_size=640)
        assert placement.src_box.w == 1280
        assert placement.scale_x == pytest.approx(640 / 1280)

    def test_edge_clamped_frame_smaller_than_window_degrades_gracefully(self):
        placement = resolve_placement(Square(cx=50, cy=50, side=100), 100, 80, window_size=640)
        assert placement.src_box == IntBox(0, 0, 100, 80)
        assert placement.scale_x == pytest.approx(640 / 100)
        assert placement.scale_y == pytest.approx(640 / 80)

    def test_default_window_size_from_config(self):
        import config
        p1 = resolve_placement(Square(cx=500, cy=500, side=200), 2000, 1600)
        p2 = resolve_placement(Square(cx=500, cy=500, side=200), 2000, 1600, window_size=config.WINDOW_SIZE)
        assert p1 == p2

    def test_returns_placement_namedtuple(self):
        placement = resolve_placement(Square(cx=500, cy=500, side=200), 2000, 1600, window_size=640)
        assert isinstance(placement, Placement)
        assert placement.off_x == 0 and placement.off_y == 0
