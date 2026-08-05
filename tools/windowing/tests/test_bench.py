"""Синтетический стенд (тикет "счёт кандидата", п.3 и п.5).

Здесь два разных класса тестов, и их не надо путать:

1. Тесты САМОГО стенда — что он воспроизводим, что цели действительно
   пересекаются, что окно эмулируется. Стенд — измерительный прибор, и если
   он врёт, всё, что им намерено, ничего не стоит.
2. Тесты ПЕТЛИ через стенд — те, что перечислены в тикете: теневой забирает
   соседа, предсказание при исчезновении ограничено, тройной махаланобис
   держит разноразмерное пересечение, а двухкоординатный контрольно нет,
   мусор не рождает теневых.
"""
import math
import random

import pytest

import tracking_config as base_cfg
from track_bench import (JUNK_TID, MIN_WINDOW, SCENARIOS, Target, cfg_with,
                         detect, run_scenario, scen_crossing, scen_crossing_gap,
                         scen_disappear, scen_head_on, scen_size_crossing)

DT = 1.0 / 3.0


# --- 1. сам прибор --------------------------------------------------------

class TestBenchItself:
    def test_same_seed_gives_identical_run(self):
        targets = scen_crossing(random.Random(7))
        cfg = cfg_with(SCORE_FORM="maha", FILTER_LEVEL=2)
        a = run_scenario(targets, cfg, seed=3)
        b = run_scenario(targets, cfg, seed=3)
        assert [t["chosen_tid"] for t in a.ticks] == [t["chosen_tid"] for t in b.ticks]
        assert [t["pred"] for t in a.ticks] == [t["pred"] for t in b.ticks]

    def test_different_seed_gives_different_noise(self):
        targets = scen_crossing(random.Random(7))
        cfg = cfg_with(SCORE_FORM="maha", FILTER_LEVEL=2)
        a = run_scenario(targets, cfg, seed=1)
        b = run_scenario(targets, cfg, seed=2)
        assert [t["pred"] for t in a.ticks] != [t["pred"] for t in b.ticks]

    def test_scenario_is_code_not_recorded_data(self):
        """Один и тот же rng-сид даёт одинаковый СЦЕНАРИЙ — иначе таблицу
        форм нельзя было бы сравнивать построчно."""
        a = scen_crossing(random.Random(11))
        b = scen_crossing(random.Random(11))
        assert (a[0].x0, a[0].vx, a[0].size) == (b[0].x0, b[0].vx, b[0].size)
        assert (a[1].x0, a[1].vy, a[1].size) == (b[1].x0, b[1].vy, b[1].size)

    def test_targets_actually_cross(self):
        """Сценарий обязан делать то, что обещает: цели должны сойтись ближе
        своего размера, иначе "пересечение" ничего не проверяет."""
        for i in range(50):
            tgt, other = scen_crossing(random.Random(i))
            gap = min(math.dist(tgt.position(t / 10), other.position(t / 10))
                      for t in range(0, 80))
            assert gap < tgt.size, f"сид {i}: цели разошлись на {gap:.4f}"

    def test_detection_noise_matches_the_declared_sigma(self):
        """sigma шума детекции = 0.3 * углового размера — то же R, что
        предполагает фильтр. Разошлись бы — стенд мерил бы рассогласование
        модели, а не форму счёта."""
        cfg = cfg_with()
        tgt = Target(0, 0.0, 0.0, 0.0, 0.0, math.radians(2.0))
        rng = random.Random(0)
        xs = []
        for _ in range(4000):
            d = detect(tgt, 1.0, rng, cfg)
            xs.append((d[0] + d[2]) / 2)
        sd = (sum(x * x for x in xs) / len(xs)) ** 0.5
        assert sd == pytest.approx(cfg.KALMAN_R_POS_SIZE_FRAC * tgt.size, rel=0.1)

    def test_window_is_emulated_targets_outside_are_invisible(self):
        """Петля обязана видеть только то, что попало в окно. Иначе стенд
        подаёт ей кадр целиком, чего на видео не бывает."""
        far = Target(1, 5.0, 5.0, 0.0, 0.0, math.radians(2.0))
        tgt = Target(0, 0.0, 0.0, 0.05, 0.0, math.radians(2.0))
        res = run_scenario([tgt, far], cfg_with(FILTER_LEVEL=2), seed=0)
        assert all(t["chosen_tid"] in (0, None) for t in res.ticks)
        assert max(t["n_dets"] for t in res.ticks) == 1

    def test_gap_really_hides_the_target(self):
        tgt = scen_disappear(random.Random(0))[0]
        t_in = (tgt.gap[0] + tgt.gap[1]) / 2
        assert not tgt.visible(t_in)
        assert tgt.visible(tgt.gap[0] - 0.5)
        assert tgt.visible(tgt.gap[1] + 0.5)


# --- 2. петля через стенд -------------------------------------------------

class TestShadowsTakeTheNeighbour:
    def test_crossing_neighbour_gets_occupied(self):
        """Требование тикета: на пересечении теневой забирает соседа. Меряем
        по счётчику занятых кандидатов, а не по метрике — так видно сам
        механизм, а не его последствия."""
        taken = 0
        for i in range(60):
            targets = scen_crossing(random.Random(i))
            cfg = cfg_with(SCORE_FORM="maha", FILTER_LEVEL=2, ENABLE_SHADOW_TRACKS=True)
            res = run_scenario(targets, cfg, seed=i)
            taken += sum(t["n_taken"] for t in res.ticks)
        assert taken > 0, "теневые не заняли ни одного кандидата за 60 пересечений"

    def test_no_shadows_no_taking(self):
        for i in range(10):
            targets = scen_crossing(random.Random(i))
            cfg = cfg_with(SCORE_FORM="maha", FILTER_LEVEL=2, ENABLE_SHADOW_TRACKS=False)
            res = run_scenario(targets, cfg, seed=i)
            assert sum(t["n_taken"] for t in res.ticks) == 0

    def test_shadows_help_on_crossing_with_a_dropout(self):
        """Именно тот отказ, ради которого механизм заведён: цель пропала на
        такт-другой, единственный кандидат в окне — сосед."""
        def rate(shadows):
            ok = 0
            for i in range(120):
                targets = scen_crossing_gap(random.Random(i))
                cfg = cfg_with(SCORE_FORM="maha", FILTER_LEVEL=2,
                               ENABLE_SHADOW_TRACKS=shadows)
                ok += run_scenario(targets, cfg, seed=i).survived
            return ok / 120
        assert rate(True) > rate(False) + 0.05


class TestJunkDoesNotSpawnShadows:
    def test_low_confidence_junk_never_gives_birth(self):
        """Мусор идёт с conf ниже порога рождения — теневых от него быть не
        должно ни одного, сколько бы его ни было."""
        tgt = Target(0, 0.0, 0.0, 0.08, 0.0, math.radians(2.0))
        cfg = cfg_with(SCORE_FORM="maha", FILTER_LEVEL=2, ENABLE_SHADOW_TRACKS=True)
        res = run_scenario([tgt], cfg, seed=0, junk_per_tick=6)
        assert max(t["n_dets"] for t in res.ticks) > 1, "мусор не дошёл до петли"
        assert all(t["n_shadows"] == 0 for t in res.ticks)


class TestDisappearanceIsBounded:
    def test_prediction_stays_within_the_damping_bound(self):
        """Тикет: финальное предсказание не дальше v0*tau от точки пропажи.
        Реализованная (дискретная) граница чуть больше — v0*dt/(1-exp(-dt/tau)),
        см. tracking_config; проверяем её, а не округлённую."""
        tau = base_cfg.EXTRAPOLATION_TAU_SEC
        q = math.exp(-DT / tau)
        for i in range(30):
            tgt = Target(0, 0.0, 0.0, 0.15, 0.0, math.radians(2.0),
                          gap=(1.0, 99.0))     # пропала навсегда
            cfg = cfg_with(SCORE_FORM="maha", FILTER_LEVEL=2, MISS_TO_LOST_N=99)
            res = run_scenario([tgt], cfg, seed=i, n_ticks=40)
            last_seen, v_at_loss = None, None
            for t in res.ticks:
                if t["chosen_tid"] == 0:
                    last_seen, v_at_loss = t["pos"], math.hypot(*t["vel"])
            assert last_seen is not None
            drift = math.dist(res.ticks[-1]["pred"], last_seen)
            # предел считается от ОЦЕНКИ скорости в момент потери: экстраполирует
            # петля тем, что оценила, а оценка шумит и бывает выше истинной
            bound = v_at_loss * DT / (1 - q)
            assert drift <= bound + 1e-9, (
                f"сид {i}: улёт {drift:.4f} при пределе {bound:.4f} "
                f"(оценка скорости {v_at_loss:.3f} против истинных 0.15)")


class TestSizeCoordinateHolds:
    def test_triple_mahalanobis_beats_the_two_coordinate_control(self):
        """Разноразмерное пересечение: форма 1 (theta, phi, log h) держит,
        контрольная двухкоординатная — нет. Это и есть проверка того, что
        третья координата не декоративна."""
        def rate(form):
            ok = 0
            for i in range(150):
                targets = scen_size_crossing(random.Random(i))
                cfg = cfg_with(SCORE_FORM=form, FILTER_LEVEL=2,
                               ENABLE_SHADOW_TRACKS=False)
                ok += run_scenario(targets, cfg, seed=i).survived
            return ok / 150
        full, control = rate("maha"), rate("maha_pos")
        assert full > control, f"тройной {full:.3f} не лучше двухкоординатного {control:.3f}"


class TestScenariosAreNotAllTrivial:
    def test_at_least_one_scenario_actually_breaks_the_loop(self):
        """Страховка от бесполезного стенда: если бы все сценарии проходились
        на 100%, таблица форм состояла бы из единиц и ничего не выбирала."""
        worst = 1.0
        for name, (maker, kw) in SCENARIOS.items():
            ok = 0
            for i in range(40):
                targets = maker(random.Random(i))
                cfg = cfg_with(SCORE_FORM="maha", FILTER_LEVEL=2,
                               ENABLE_SHADOW_TRACKS=True)
                ok += run_scenario(targets, cfg, seed=i, **kw).survived
            worst = min(worst, ok / 40)
        assert worst < 0.85, f"самый сложный сценарий проходится на {worst:.2f}"
