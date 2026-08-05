"""clip_box_to_window — перевод рамки из кадра в координаты холста окна.

Мутационный прогон убил 2 мутанта из 32: почти любую формулу здесь можно было
испортить незаметно. Цена ошибки максимальная из всего пайплайна — это
функция, которая делает РАЗМЕТКУ обучающей выборки. Сдвинутая на несколько
процентов рамка не роняет ничего и не видна ни в одном отчёте: модель просто
учится на смещённых целях.

Проверяется главное свойство: рамка в холсте обязана указывать на то же
место изображения, что и рамка в кадре. Проверка идёт через независимый
пересчёт (пиксель кадра -> пиксель холста), а не повторением той же формулы.
"""
import pytest

import config
from dataset_gen import clip_box_to_window
from geometry import IntBox, Square, resolve_placement

WS = config.WINDOW_SIZE
FRAME_W, FRAME_H = 1920, 1080


def place(cx, cy, side):
    return resolve_placement(Square(cx, cy, side), FRAME_W, FRAME_H, WS)


def frame_px_to_canvas(x, y, pl):
    """Независимый перевод точки кадра в холст — по тем же placement-полям,
    но без обрезок и нормировок, которые делает сама функция."""
    return ((x - pl.src_box.x0) * pl.scale_x + pl.off_x,
            (y - pl.src_box.y0) * pl.scale_y + pl.off_y)


def box(cx, cy, w, h=None):
    h = w if h is None else h
    return IntBox(int(cx - w / 2), int(cy - h / 2), int(cx + w / 2), int(cy + h / 2))


class TestBoxLandsWhereItShould:
    @pytest.mark.parametrize("side", [640, 800, 1080])
    @pytest.mark.parametrize("off", [(0, 0), (120, -80), (-200, 150)])
    def test_centre_matches_independent_projection(self, side, off):
        """Центр рамки в холсте обязан совпасть с проекцией центра рамки
        кадра. Это ловит и перепутанные оси, и потерянный off, и масштаб."""
        pl = place(960, 540, side)
        b = box(960 + off[0], 540 + off[1], 100)
        got = clip_box_to_window(b, pl, WS)
        assert got is not None
        cx, cy, _, _ = got
        ux, uy = frame_px_to_canvas((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2, pl)
        assert cx * WS == pytest.approx(ux, abs=0.5)
        assert cy * WS == pytest.approx(uy, abs=0.5)

    def test_size_scales_with_the_window(self):
        """Ширина в холсте = ширина в кадре * масштаб. Окно вдвое больше —
        цель вдвое мельче."""
        b = box(960, 540, 100)
        small = clip_box_to_window(b, place(960, 540, 640), WS)
        large = clip_box_to_window(b, place(960, 540, 1280), WS)
        assert small[2] == pytest.approx(2 * large[2], rel=1e-6)

    def test_unresized_window_keeps_pixel_sizes(self):
        """Окно ровно в WINDOW_SIZE не масштабируется (crop.py не увеличивает):
        рамка 100px обязана остаться 100px из 640."""
        got = clip_box_to_window(box(960, 540, 100), place(960, 540, WS), WS)
        assert got[2] == pytest.approx(100 / WS, rel=1e-6)
        assert got[3] == pytest.approx(100 / WS, rel=1e-6)

    def test_non_square_box_keeps_its_aspect(self):
        """У паруса рамка вытянутая — перепутанные оси здесь дали бы
        повёрнутую разметку, и на картинке это почти незаметно."""
        got = clip_box_to_window(box(960, 540, 60, 180), place(960, 540, 640), WS)
        assert got[3] == pytest.approx(3 * got[2], rel=1e-6)

    def test_coordinates_are_normalised_to_zero_one(self):
        for cx, cy in ((960, 540), (700, 400), (1200, 700)):
            got = clip_box_to_window(box(cx, cy, 80), place(960, 540, 640), WS)
            if got is None:
                continue
            for v in got:
                assert 0.0 <= v <= 1.0


class TestClipping:
    def test_box_partially_outside_is_clipped_not_dropped(self):
        """Цель на краю окна обязана попасть в разметку обрезанной, а не
        исчезнуть: иначе модель учится, что на краю окна целей не бывает."""
        pl = place(960, 540, 640)
        b = box(960 + 300, 540, 200)   # половина рамки за правым краем
        got = clip_box_to_window(b, pl, WS)
        assert got is not None
        cx, _, w, _ = got
        assert w < 200 / 640, "рамка не обрезана"
        assert cx + w / 2 <= 1.0 + 1e-9, "рамка вылезла за холст"

    def test_box_fully_outside_returns_none(self):
        pl = place(300, 300, 640)
        assert clip_box_to_window(box(1700, 900, 100), pl, WS) is None

    def test_touching_edge_exactly_is_not_a_box(self):
        """Соприкосновение по границе — нулевая площадь, не цель."""
        pl = place(960, 540, 640)
        ib = pl.src_box
        assert clip_box_to_window(IntBox(ib.x1, ib.y0, ib.x1 + 50, ib.y0 + 50), pl, WS) is None

    def test_sliver_below_min_visible_is_dropped(self):
        """Полоска тоньше MIN_VISIBLE_BOX_PX — не цель, а артефакт обрезки:
        учить на ней значит учить находить край окна."""
        pl = place(960, 540, 640)
        ib = pl.src_box
        sliver = IntBox(ib.x1 - 1, 500, ib.x1 + 100, 580)  # 1px видимой ширины
        assert clip_box_to_window(sliver, pl, WS) is None

    def test_just_above_min_visible_is_kept(self):
        """Обратная сторона порога: чуть шире — уже цель. Без этого теста
        порог можно было бы поднять до бесконечности незаметно."""
        pl = place(960, 540, WS)  # масштаб 1:1, пиксели кадра = пиксели холста
        ib = pl.src_box
        need = int(config.MIN_VISIBLE_BOX_PX) + 2
        b = IntBox(ib.x1 - need, 500, ib.x1 + 100, 500 + 4 * need)
        assert clip_box_to_window(b, pl, WS) is not None


class TestBoxOutsideStaysOutside:
    def test_neighbour_target_in_the_same_window_is_included(self):
        """В окно попадают ВСЕ рамки, а не только та, вокруг которой оно
        построено (тикет п.3) — иначе соседний сёрфер оказывается
        неразмеченным позитивом, то есть учит модель его не видеть."""
        pl = place(960, 540, 640)
        neighbour = box(1100, 600, 60)
        assert clip_box_to_window(neighbour, pl, WS) is not None

    def test_window_near_frame_edge_shifts_inside(self):
        """resolve_placement сдвигает окно внутрь кадра; разметка обязана
        считаться от СДВИНУТОГО окна, а не от запрошенного."""
        pl = place(50, 50, 640)          # окно уехало бы за левый верхний угол
        b = box(100, 100, 60)
        got = clip_box_to_window(b, pl, WS)
        assert got is not None
        ux, uy = frame_px_to_canvas(100, 100, pl)
        assert got[0] * WS == pytest.approx(ux, abs=0.5)
        assert got[1] * WS == pytest.approx(uy, abs=0.5)
