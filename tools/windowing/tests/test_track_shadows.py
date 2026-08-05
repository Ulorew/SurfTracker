"""Теневые треки (тикет "счёт кандидата", п.2).

Назначение механизма одно: чужие детекции ЗАНЯТЫ и не притягивают цель. Все
тесты ниже — про это, а не про то, что структура данных работает.

Мутации, которые они обязаны ловить (прямое требование тикета):
  - бессмертный теневой (SHADOW_MAX_MISSES -> бесконечность) роняет
    test_dead_shadow_stops_taking_candidates;
  - 0.7 -> 1/0.7 роняет test_candidate_closer_to_target_is_not_taken.
"""
import math
import types

import pytest

import tracking_config as base_cfg
from track_shadows import ShadowSet


def cfg(**over):
    d = {k: getattr(base_cfg, k) for k in dir(base_cfg) if k.isupper()}
    d.update(over)
    return types.SimpleNamespace(**d)


SIZE = math.radians(2.0)
DT = 1.0 / 3.0


def det(x, y, size=SIZE, conf=0.9):
    return (x - size / 2, y - size / 2, x + size / 2, y + size / 2, conf)


class TestBirth:
    def test_confident_unchosen_detection_becomes_a_shadow(self):
        s = ShadowSet(cfg())
        s.step(DT, [det(0.1, 0.0, conf=0.9)], chosen=None)
        assert len(s) == 1

    def test_chosen_detection_never_becomes_a_shadow(self):
        """Цель не занимает сама себя — иначе на следующем такте она окажется
        "занята" собственным теневым и петля её отбросит."""
        d = det(0.1, 0.0, conf=0.9)
        s = ShadowSet(cfg())
        s.step(DT, [d], chosen=d)
        assert len(s) == 0

    def test_low_confidence_detection_does_not_give_birth(self):
        """Мусор детектора не должен плодить теневых: он бы занял пол-окна и
        отобрал у цели её собственные детекции."""
        s = ShadowSet(cfg())
        s.step(DT, [det(0.1, 0.0, conf=base_cfg.SHADOW_BIRTH_CONF - 0.01)], chosen=None)
        assert len(s) == 0

    def test_birth_threshold_is_inclusive(self):
        s = ShadowSet(cfg())
        s.step(DT, [det(0.1, 0.0, conf=base_cfg.SHADOW_BIRTH_CONF)], chosen=None)
        assert len(s) == 1

    def test_detection_matching_an_existing_shadow_does_not_create_a_second(self):
        s = ShadowSet(cfg())
        s.step(DT, [det(0.1, 0.0)], chosen=None)
        s.step(DT, [det(0.1, 0.0)], chosen=None)
        assert len(s) == 1


class TestLifeAndDeath:
    def test_shadow_survives_while_it_is_fed(self):
        s = ShadowSet(cfg())
        for i in range(10):
            s.step(DT, [det(0.1 + 0.01 * i, 0.0)], chosen=None)
        assert len(s) == 1

    def test_shadow_dies_after_max_misses(self):
        s = ShadowSet(cfg())
        s.step(DT, [det(0.1, 0.0)], chosen=None)
        for _ in range(base_cfg.SHADOW_MAX_MISSES):
            s.step(DT, [], chosen=None)
        assert len(s) == 0

    def test_shadow_is_alive_one_tick_before_that(self):
        s = ShadowSet(cfg())
        s.step(DT, [det(0.1, 0.0)], chosen=None)
        for _ in range(base_cfg.SHADOW_MAX_MISSES - 1):
            s.step(DT, [], chosen=None)
        assert len(s) == 1

    def test_feed_threshold_equals_birth_threshold(self):
        """Мини-тикет "заморозка": порог питания поднят до порога рождения.

        ВНИМАНИЕ, прежняя редакция этого докстринга приписывала низкому порогу
        цену -0.210 на стенде. Это неверно: -0.210 — цена теневых треков
        ЦЕЛИКОМ (механизм выкл против вкл), а подъём порога 0.2 -> 0.4 не
        возвращает ничего (-0.001 +- 0.003 на 2500 парных прогонах). Порог
        поднят не ради выигрыша, а ради последовательности: детекция, которой
        не хватает уверенности родить теневой, не должна и кормить его."""
        assert base_cfg.SHADOW_FEED_CONF == base_cfg.SHADOW_BIRTH_CONF

    def test_detection_at_the_feed_threshold_still_feeds(self):
        s = ShadowSet(cfg())
        s.step(DT, [det(0.1, 0.0, conf=0.9)], chosen=None)
        for _ in range(base_cfg.SHADOW_MAX_MISSES + 2):
            s.step(DT, [det(0.1, 0.0, conf=base_cfg.SHADOW_FEED_CONF)], chosen=None)
        assert len(s) == 1

    def test_junk_below_the_threshold_cannot_keep_a_shadow_alive(self):
        """Та самая утечка, ради закрытия которой порог и поднят: слабые
        детекции больше не продлевают жизнь теневому. Тест падает при любом
        понижении порога питания."""
        s = ShadowSet(cfg())
        s.step(DT, [det(0.1, 0.0, conf=0.9)], chosen=None)
        weak = base_cfg.SHADOW_FEED_CONF - 0.01
        for _ in range(base_cfg.SHADOW_MAX_MISSES):
            s.step(DT, [det(0.1, 0.0, conf=weak)], chosen=None)
        assert len(s) == 0, f"детекция с conf={weak} продлила жизнь теневому"

    def test_junk_cannot_drag_a_shadow_onto_the_target(self):
        """Полная форма утечки: слабые детекции не только не продлевают жизнь,
        но и не двигают теневой. Именно сдвиг на цель делал её "занятой".

        Слабая детекция ставится ВНУТРИ радиуса сопоставления. Первая версия
        теста ставила её в x=0.05 при теневом в x=0.5 и радиусе 0.052, то есть
        в 8.6 радиуса — такая детекция не накормила бы теневой ни при какой
        уверенности, и тест проходил даже при полностью снятой проверке
        порога. Проверено мутацией в обе стороны: нынешняя версия падает при
        снятии проверки и проходит на чистом коде.
        """
        s = ShadowSet(cfg())
        s.step(DT, [det(0.5, 0.0, conf=0.9)], chosen=None, target_vel=(0.0, 0.0))
        before = s.debug_state()[0]["cx"]
        limit = s.match_limit(s.tracks[0], SIZE, DT)
        weak_x = 0.5 - 0.9 * limit          # в радиусе, но со стороны цели
        for _ in range(base_cfg.SHADOW_MAX_MISSES - 1):
            s.step(DT, [det(weak_x, 0.0, conf=base_cfg.SHADOW_FEED_CONF - 0.05)],
                   chosen=None, target_vel=(0.0, 0.0))
        assert s.debug_state()[0]["cx"] == pytest.approx(before, abs=1e-9), \
            "слабая детекция сдвинула теневой к цели"

    def test_cap_is_respected(self):
        s = ShadowSet(cfg())
        for i in range(base_cfg.SHADOW_MAX_COUNT + 3):
            s.step(DT, [det(0.1 + 0.2 * i, 0.0)], chosen=None)
        assert len(s) <= base_cfg.SHADOW_MAX_COUNT

    def test_overflow_kills_the_stalest_not_the_newest(self):
        """При переполнении умирает самый давно необновлявшийся. Если бы
        умирал новый, механизм терял бы именно того соседа, который только что
        появился рядом с целью."""
        c = cfg(SHADOW_MAX_COUNT=2)
        s = ShadowSet(c)
        s.step(DT, [det(0.5, 0.0)], chosen=None)          # A — дальше не кормим
        s.step(DT, [det(0.5, 0.0), det(-0.5, 0.0)], chosen=None)   # A, B
        s.step(DT, [det(-0.5, 0.0), det(0.0, 0.5)], chosen=None)   # кормим B, рождаем C
        xs = sorted(round(t["cx"], 2) for t in s.debug_state())
        assert 0.5 not in xs, "убит не самый давно необновлявшийся"
        assert len(s) == 2


class TestTakingCandidates:
    def _with_shadow_at(self, x, y, **over):
        s = ShadowSet(cfg(**over))
        s.step(DT, [det(x, y)], chosen=None)
        return s

    def test_candidate_on_the_shadow_is_taken(self):
        s = self._with_shadow_at(0.1, 0.0)
        assert s.is_taken(det(0.1, 0.0), pred_cx=0.0, pred_cy=0.0, dt=DT)

    def test_candidate_closer_to_target_is_not_taken(self):
        """Ключевое правило: занят только тот, до кого ТЕНЕВОМУ ближе, чем
        0.7 от расстояния до цели. Кандидат рядом с целью обязан остаться.

        Именно этот тест ловит мутацию 0.7 -> 1/0.7: при перевёрнутом
        коэффициенте занятым оказывается кандидат, который ближе к ЦЕЛИ.
        """
        s = self._with_shadow_at(1.0, 0.0)
        near_target = det(0.02, 0.0)     # почти на предсказании цели
        assert not s.is_taken(near_target, pred_cx=0.0, pred_cy=0.0, dt=DT)

    def test_ratio_boundary(self):
        """Проверка самого числа, а не только знака неравенства: кандидат на
        0.69 от расстояния до цели занят, на 0.71 — нет."""
        s = self._with_shadow_at(1.0, 0.0)
        d_target = 1.0
        for frac, taken in ((0.69, True), (0.71, False)):
            # цель в нуле, теневой в 1.0; ставим кандидата в точку x, где
            # (1-x)/x = frac, то есть до теневого ровно frac от расстояния
            # до цели
            x = 1.0 / (1.0 + frac)
            cand = det(x, 0.0)
            got = s.is_taken(cand, pred_cx=0.0, pred_cy=0.0, dt=DT)
            assert got is taken, f"доля {frac}: занят={got}"

    def test_dead_shadow_stops_taking_candidates(self):
        """Обратная сторона смертности: после SHADOW_MAX_MISSES тактов без
        обновления теневой обязан перестать занимать место — иначе сосед,
        которого давно нет, навсегда закрывает цели дорогу к захвату.

        Этот тест падает при бессмертном теневом.
        """
        s = self._with_shadow_at(0.1, 0.0)
        cand = det(0.1, 0.0)
        assert s.is_taken(cand, 0.0, 0.0, DT)
        for _ in range(base_cfg.SHADOW_MAX_MISSES):
            s.step(DT, [], chosen=None)
        assert not s.is_taken(cand, 0.0, 0.0, DT)

    def test_no_shadows_takes_nothing(self):
        s = ShadowSet(cfg())
        assert not s.is_taken(det(0.1, 0.0), 0.0, 0.0, DT)

    def test_taking_uses_the_shadow_PREDICTION_not_its_last_position(self):
        """Теневой экстраполируется, как и цель: сосед, уехавший за такт,
        обязан занимать своё НОВОЕ место, иначе механизм отстаёт ровно на
        такт — на быстром соседе это и есть момент подмены."""
        s = ShadowSet(cfg())
        step = 0.1
        for i in range(4):
            s.step(DT, [det(0.3 + step * i, 0.0)], chosen=None,
                   target_vel=(step / DT, 0.0))
        assert len(s) == 1, "сосед распался на цепочку однотактных теней"
        ahead = s.predictions(DT)[0][0]
        assert ahead > 0.6, "теневой не экстраполируется"


class TestLossDoesNotClearShadows:
    def test_shadows_persist_so_reacquisition_does_not_grab_a_neighbour(self):
        """Прямое требование тикета: при потере цели теневые не очищаются."""
        s = ShadowSet(cfg())
        s.step(DT, [det(0.1, 0.0)], chosen=None)
        for _ in range(base_cfg.SHADOW_MAX_MISSES - 1):
            s.step(DT, [det(0.1, 0.0, conf=0.5)], chosen=None)   # сосед на месте
        assert len(s) == 1, "теневой не пережил тактов без цели"


class TestOrderIndependence:
    def test_result_does_not_depend_on_detection_order(self):
        base = ShadowSet(cfg())
        dets = [det(0.3, 0.0), det(-0.3, 0.1), det(0.0, 0.4)]
        base.step(DT, dets, chosen=None)
        rev = ShadowSet(cfg())
        rev.step(DT, list(reversed(dets)), chosen=None)
        a = sorted((round(t["cx"], 6), round(t["cy"], 6)) for t in base.debug_state())
        b = sorted((round(t["cx"], 6), round(t["cy"], 6)) for t in rev.debug_state())
        assert a == b


class TestExactRules:
    """Мутационный прогон показал, что часть правил держалась только на знаках
    неравенств: замена <= на < ничего не роняла. Здесь — точные значения."""

    def test_det_helpers_on_an_asymmetric_box(self):
        from track_shadows import _det_center, _det_conf, _det_size
        box = (1.0, 2.0, 4.0, 10.0, 0.42)
        assert _det_center(box) == (2.5, 6.0)
        assert _det_size(box) == 8.0
        assert _det_conf(box) == 0.42
        assert _det_conf((1.0, 2.0, 4.0, 10.0)) == 1.0, "без conf детекция считается достоверной"

    def test_taking_is_strict_at_the_exact_ratio(self):
        """Правило тикета — СТРОГОЕ неравенство: ровно на пороге кандидат
        остаётся у цели. Иначе граничный случай отдаётся соседу."""
        s = ShadowSet(cfg())
        s.step(DT, [det(1.0, 0.0)], chosen=None)
        x = 1.0 / (1.0 + base_cfg.SHADOW_TAKEN_RATIO)   # ровно на пороге
        assert not s.is_taken(det(x, 0.0), 0.0, 0.0, DT)

    def test_match_limit_exact_value(self):
        """Радиус = доля размера + путь теневого за такт. Обе части нужны:
        без первой не переживается шум, без второй — движение соседа."""
        c = cfg()
        s = ShadowSet(c)
        s.step(DT, [det(0.5, 0.0)], chosen=None, target_vel=(0.3, 0.4))
        t = s.tracks[0]
        got = s.match_limit(t, SIZE, DT)
        want = c.SHADOW_MATCH_SIZE_FRAC * SIZE + 0.5 * DT   # |v| = 0.5
        assert got == pytest.approx(want, rel=1e-12)

    def test_bigger_of_the_two_sizes_is_used(self):
        c = cfg()
        s = ShadowSet(c)
        s.step(DT, [det(0.5, 0.0, size=SIZE)], chosen=None)
        t = s.tracks[0]
        assert s.match_limit(t, 4 * SIZE, DT) > s.match_limit(t, SIZE, DT)

    def test_nearest_detection_feeds_the_shadow_not_the_first_one(self):
        """При двух детекциях в радиусе кормит БЛИЖАЙШАЯ. Иначе теневой
        уползает на случайного соседа и перестаёт занимать того, кого должен."""
        s = ShadowSet(cfg())
        s.step(DT, [det(0.5, 0.0)], chosen=None)
        near, far = det(0.51, 0.0), det(0.5 + 1.4 * SIZE, 0.0)
        s.step(DT, [far, near], chosen=None, target_vel=(0.0, 0.0))
        assert len(s) == 2, "вторая детекция обязана была родить свой теневой"
        fed = min(s.tracks, key=lambda t: t.born_at)
        assert abs(fed.filter.cx - 0.51) < abs(fed.filter.cx - (0.5 + 1.4 * SIZE))

    def test_detection_exactly_at_the_limit_still_feeds(self):
        """Граница включительная: ровно на радиусе — это ещё он же. Иначе
        детекция на самом краю допуска рождает дубль того же соседа."""
        c = cfg()
        s = ShadowSet(c)
        s.step(DT, [det(0.5, 0.0)], chosen=None, target_vel=(0.0, 0.0))
        t = s.tracks[0]
        limit = s.match_limit(t, SIZE, DT)
        s.step(DT, [det(0.5 + limit, 0.0)], chosen=None, target_vel=(0.0, 0.0))
        assert len(s) == 1, "детекция ровно на радиусе не накормила теневой"

    def test_detection_outside_the_limit_does_not_feed(self):
        c = cfg()
        s = ShadowSet(c)
        s.step(DT, [det(0.5, 0.0)], chosen=None)
        far = 0.5 + 5 * c.SHADOW_MATCH_SIZE_FRAC * SIZE
        s.step(DT, [det(far, 0.0)], chosen=None, target_vel=(0.0, 0.0))
        assert len(s) == 2, "далёкая детекция накормила чужой теневой вместо рождения своего"
