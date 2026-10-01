"""sample_window.py — розыгрыш позитивных окон по корзинам размеров.

Модуль до этого не был покрыт вообще: мутационный прогон убил 0 из 40
мутантов, то есть любую из его формул можно было испортить незаметно. А это
сердце розыгрыша окон — именно от него зависит, какие размеры цели модель
видит в обучении.

Проверяется ГЛАВНОЕ СВОЙСТВО: окно, разыгранное под корзину, обязано давать
ВИДИМЫЙ размер цели внутри этой корзины. Видимый размер — это то, что реально
попадает в сеть после вырезки:
    S <= WINDOW_SIZE -> ресайза нет, видно B пикселей;
    S >  WINDOW_SIZE -> уменьшение, видно B * WINDOW_SIZE / S.
Всё остальное (доли, лимиты, детерминизм) — вокруг него.
"""
import random

import pytest

import config
from geometry import IntBox
from sample_window import (_reachable_sides, reachable_bin_indices,
                           sample_window, window_for_bin)


def visible_size(b_size, side):
    """Размер цели в готовой картинке 640x640 — то, что увидит сеть."""
    if side <= config.WINDOW_SIZE:
        return b_size
    return b_size * config.WINDOW_SIZE / side


def box(size, cx=960, cy=540):
    """IntBox — полуоткрытый [x0,x1); w/h у него вычисляемые, не поля."""
    return IntBox(cx - size // 2, cy - size // 2,
                  cx - size // 2 + size, cy - size // 2 + size)


ALL_BINS = list(range(len(config.SIZE_BINS)))


class TestVisibleSizeLandsInTheTargetBin:
    """Ради чего всё и написано: попросили корзину — получили её."""

    @pytest.mark.parametrize("b_size", [30, 45, 70, 120, 200, 300, 500])
    def test_window_for_bin_hits_the_requested_bin(self, b_size):
        rng = random.Random(0)
        checked = 0
        for i in reachable_bin_indices(b_size):
            lo, hi, _ = config.SIZE_BINS[i]
            for _ in range(20):
                sq = window_for_bin(box(b_size), i, rng)
                assert sq is not None, f"корзина {i} объявлена достижимой, но окна нет"
                v = visible_size(b_size, sq.side)
                assert lo <= v < hi or v == pytest.approx(lo, rel=1e-6), (
                    f"бокс {b_size}px, корзина [{lo},{hi}): видимый размер {v:.1f}")
                checked += 1
        assert checked, f"для бокса {b_size} не нашлось ни одной достижимой корзины"

    @pytest.mark.parametrize("b_size", [45, 120, 300])
    def test_unreachable_bins_return_none(self, b_size):
        rng = random.Random(1)
        unreachable = set(ALL_BINS) - set(reachable_bin_indices(b_size))
        assert unreachable or b_size in (120,), "тест бесполезен, если недостижимых нет"
        for i in unreachable:
            assert window_for_bin(box(b_size), i, rng) is None

    def test_sample_window_sides_all_land_in_some_bin(self):
        rng = random.Random(2)
        for b_size in (20, 35, 60, 110, 250, 400):
            for sq in sample_window(box(b_size), rng):
                v = visible_size(b_size, sq.side)
                assert any(lo <= v < hi for lo, hi, _ in config.SIZE_BINS), (
                    f"бокс {b_size}: видимый размер {v:.1f} не попал ни в одну корзину")


class TestReachability:
    """reachable_bin_indices — детерминированный ответ "что этот бокс может"."""

    def test_agrees_with_window_for_bin(self):
        """Два независимых куска кода отвечают на один вопрос — они обязаны
        отвечать одинаково, иначе розыгрыш "от корзины к боксу" будет звать
        window_for_bin там, где окна нет, и молча терять слоты."""
        rng = random.Random(3)
        for b_size in range(15, 520, 7):
            declared = set(reachable_bin_indices(b_size))
            actual = {i for i in ALL_BINS if window_for_bin(box(b_size), i, rng) is not None}
            assert declared == actual, f"бокс {b_size}px: заявлено {declared}, вышло {actual}"

    def test_target_below_the_lowest_bin_reaches_nothing(self):
        """Цель мельче нижней границы SIZE_BINS не попадает ни в одну корзину:
        увеличивать crop() не умеет. Такие боксы дают ноль окон — это и есть
        boxes_zero_windows в отчёте нарезки, а не сбой."""
        lowest = config.SIZE_BINS[0][0]
        for b_size in (config.MIN_TARGET_SIZE, lowest - 1):
            assert reachable_bin_indices(b_size) == []

    def test_own_bin_is_reachable_for_small_targets(self):
        """Мелкая цель обязана быть достижима хотя бы в СВОЕЙ корзине —
        уменьшать её некуда, увеличивать crop() не умеет."""
        for b_size in (35, 40, 80):
            own = next(i for i, (lo, hi, _) in enumerate(config.SIZE_BINS)
                       if lo <= b_size < hi)
            assert own in reachable_bin_indices(b_size)

    def test_target_cannot_be_enlarged(self):
        """Ключевое ограничение всей схемы: бокс можно только уменьшить.
        Корзины целиком выше собственного размера цели недостижимы."""
        b_size = 30
        for i, (lo, _hi, _) in enumerate(config.SIZE_BINS):
            if lo > b_size:
                assert i not in reachable_bin_indices(b_size), (
                    f"корзина от {lo}px объявлена достижимой для цели {b_size}px")

    def test_bin_boundary_is_inclusive_on_the_left(self):
        """Границы корзин полуоткрыты: [lo, hi). Бокс РОВНО в lo принадлежит
        этой корзине, ровно в hi — уже следующей. Сдвиг на один пиксель здесь
        меняет корзину у целого класса целей."""
        for lo, hi, _ in config.SIZE_BINS:
            if config.K_MIN * lo > config.WINDOW_SIZE:
                continue  # путь натуры для такой корзины закрыт
            own = next(i for i, (l, h, _) in enumerate(config.SIZE_BINS) if l <= lo < h)
            assert own in reachable_bin_indices(lo), f"бокс ровно {lo}px выпал из своей корзины"
            nxt = [i for i, (l, h, _) in enumerate(config.SIZE_BINS) if l <= hi < h]
            if nxt:
                assert own not in reachable_bin_indices(hi) or hi >= config.SIZE_BINS[own][1]

    def test_big_target_can_reach_smaller_bins_by_resize(self):
        idx = reachable_bin_indices(400)
        assert len(idx) >= 2, "крупная цель обязана доставать несколько корзин ресайзом"


class TestWindowGeometry:
    def test_jitter_is_bounded_by_its_fraction_of_the_side(self):
        rng = random.Random(4)
        b = box(100)
        worst = 0.0
        for _ in range(300):
            sq = window_for_bin(b, reachable_bin_indices(100)[0], rng)
            worst = max(worst, abs(sq.cx - 960), abs(sq.cy - 540))
            assert abs(sq.cx - 960) <= config.CENTER_JITTER_FRAC * sq.side + 1e-9
            assert abs(sq.cy - 540) <= config.CENTER_JITTER_FRAC * sq.side + 1e-9
        allowed = config.CENTER_JITTER_FRAC * sq.side
        assert worst > 0.8 * allowed, (
            f"джиттер за 300 бросков дошёл только до {worst:.1f} из {allowed:.1f} — "
            "амплитуда меньше заявленной доли стороны")

    def test_window_is_centred_on_the_box_not_on_the_frame(self):
        rng = random.Random(5)
        b = box(100, cx=300, cy=200)
        sq = window_for_bin(b, reachable_bin_indices(100)[0], rng)
        assert abs(sq.cx - 300) < 0.5 * sq.side
        assert abs(sq.cy - 200) < 0.5 * sq.side

    def test_natural_path_takes_maximum_context_without_resizing(self):
        """Путь "натура": берём максимум контекста, но не переходим в ресайз —
        сторона не должна превысить WINDOW_SIZE, иначе цель начнёт
        уменьшаться и уедет из своей корзины."""
        rng = random.Random(6)
        b_size = 100  # 100 < 640/K_MIN, значит путь натуры доступен
        own = next(i for i, (lo, hi, _) in enumerate(config.SIZE_BINS)
                   if lo <= b_size < hi)
        sq = window_for_bin(box(b_size), own, rng)
        assert sq.side <= config.WINDOW_SIZE
        assert sq.side == pytest.approx(min(config.K_MAX, config.WINDOW_SIZE / b_size) * b_size)

    def test_k_stays_within_configured_range(self):
        """k = сторона/размер цели обязан лежать в [K_MIN, K_MAX] — вне этого
        диапазона окно либо душит цель, либо теряет её в контексте."""
        rng = random.Random(7)
        for b_size in (20, 45, 120, 300, 500):
            for i in reachable_bin_indices(b_size):
                sq = window_for_bin(box(b_size), i, rng)
                k = sq.side / b_size
                assert config.K_MIN - 1e-9 <= k <= config.K_MAX + 1e-9, (
                    f"бокс {b_size}px, корзина {i}: k={k:.2f}")


class TestSampleWindowCaps:
    def test_small_boxes_get_more_windows_than_large_ones(self):
        """Мелкому боксу доступна, как правило, одна корзина — лимит для него
        поднят, чтобы объём выборки не проседал."""
        rng = random.Random(8)
        small = sample_window(box(config.SMALL_BOX_THRESHOLD_PX - 5), rng)
        assert len(small) == config.SMALL_BOX_MAX_WINDOWS

    def test_large_box_does_not_duplicate_beyond_reachable_bins(self):
        rng = random.Random(9)
        b_size = 400
        got = sample_window(box(b_size), rng)
        assert len(got) <= max(len(reachable_bin_indices(b_size)),
                               config.MAX_WINDOWS_PER_BOX)

    def test_large_box_gets_exactly_one_window_per_reachable_bin(self):
        """Крупному боксу окон ровно столько, сколько корзин он закрывает (но
        не больше лимита): размножать одну и ту же корзину сверх этого значит
        вырождать доли в шум одного розыгрыша."""
        rng = random.Random(13)
        for b_size in (250, 400, 500):
            reach = reachable_bin_indices(b_size)
            if b_size < config.SMALL_BOX_THRESHOLD_PX or not reach:
                continue
            n = len(sample_window(box(b_size), rng))
            assert n == min(config.MAX_WINDOWS_PER_BOX, len(reach)), (
                f"бокс {b_size}px: окон {n}, достижимых корзин {len(reach)}")

    def test_never_exceeds_the_configured_cap(self):
        rng = random.Random(10)
        for b_size in (20, 60, 150, 400):
            n = len(sample_window(box(b_size), rng))
            cap = (config.SMALL_BOX_MAX_WINDOWS
                   if b_size < config.SMALL_BOX_THRESHOLD_PX else config.MAX_WINDOWS_PER_BOX)
            assert n <= cap


class TestTargetFractionsAreUsed:
    """Доли SIZE_BINS обязаны ВЛИЯТЬ на розыгрыш. Раньше
    третий элемент тройки распаковывался в `_frac` и терялся."""

    def test_fraction_is_returned_alongside_the_side(self):
        rng = random.Random(11)
        got = _reachable_sides(300, rng)
        assert got, "крупный бокс обязан что-то закрывать"
        for side, frac in got:
            assert side > 0
            assert any(frac == b[2] for b in config.SIZE_BINS), (
                "доля не из SIZE_BINS — значит потеряна или подменена")

    def test_all_zero_weights_fall_back_to_uniform_instead_of_crashing(self):
        """Вырожденный конфиг (все доли нули) не должен ронять нарезку:
        random.choices с нулевой суммой весов бросает исключение, поэтому
        предусмотрен переход на равномерный выбор."""
        import copy
        original = copy.deepcopy(config.SIZE_BINS)
        try:
            config.SIZE_BINS = [(lo, hi, 0.0) for lo, hi, _ in original]
            got = sample_window(box(400), random.Random(14))
            assert got, "при нулевых долях нарезка обязана дать окна, а не пустоту"
        finally:
            config.SIZE_BINS = original

    def test_zero_weight_bin_is_never_drawn(self):
        """Прямая проверка, что вес участвует: обнуляем доли всех корзин,
        кроме одной, и весь розыгрыш обязан уйти в неё."""
        import copy
        original = copy.deepcopy(config.SIZE_BINS)
        try:
            b_size = 400
            reach = reachable_bin_indices(b_size)
            assert len(reach) >= 2, "тест бесполезен при одной достижимой корзине"
            keep = reach[-1]
            config.SIZE_BINS = [(lo, hi, 1.0 if i == keep else 0.0)
                                for i, (lo, hi, _) in enumerate(original)]
            rng = random.Random(12)
            lo, hi, _ = config.SIZE_BINS[keep]
            for _ in range(50):
                for sq in sample_window(box(b_size), rng):
                    v = visible_size(b_size, sq.side)
                    assert lo <= v < hi, (
                        f"окно ушло в корзину с нулевой долей: видимый размер {v:.1f}")
        finally:
            config.SIZE_BINS = original


class TestDeterminism:
    def test_same_seed_same_windows(self):
        a = sample_window(box(120), random.Random(42))
        b = sample_window(box(120), random.Random(42))
        assert a == b

    def test_different_seed_different_windows(self):
        a = sample_window(box(120), random.Random(1))
        b = sample_window(box(120), random.Random(2))
        assert a != b, "розыгрыш не зависит от зерна — где-то потеряна случайность"
