"""Механизмы против подмены цели (тикет "подмены v2", п.2-4).

Каждый механизм проверяется в двух режимах: выключен — поведение обязано
совпадать с базой; включён — обязан менять решение на сценарии, ради
которого заведён. Без второй половины тест бесполезен: механизм можно
целиком закомментировать, и он останется зелёным."""
import types

import pytest

from track_logic import (
    STATUS_LOST, STATUS_TRACKING, TrackState, occlusion_triggered,
    score_candidate, select_target,
)


def make_cfg(**overrides):
    cfg = types.SimpleNamespace(
        TRACK_WINDOW_K=3.5, TARGET_SELECT_MAX_DIST_FRAC=0.30,
        REACQUIRE_MAX_DIST_FRAC=0.30, MISS_TO_LOST_N=5,
        WINDOW_EXPAND_PER_MISS=1.15, SIZE_FILTER_GROW_RATE=0.5,
        SIZE_FILTER_SHRINK_RATE=0.1, FILTER_LEVEL=0,
        ALPHA_BETA_ALPHA=0.6, ALPHA_BETA_BETA=0.3,
        ENABLE_SIZE_SCORING=False, SIZE_LAMBDA=0.5, SIZE_VETO_RATIO=1.8,
        ENABLE_OCCLUSION_HOLD=False, OCCLUSION_PROXIMITY_FRAC=0.25,
        OCCLUSION_HOLD_TICKS=4,
        ENABLE_VELOCITY_GATE=False, VELOCITY_GATE_FACTOR=2.0,
        VELOCITY_GATE_NOISE_ANG_PER_SEC=60.0, VELOCITY_LAMBDA=0.5,
        VELOCITY_VETO_MULT=3.0,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def box(cx, cy, size, conf=0.5):
    h = size / 2.0
    return (cx - h, cy - h, cx + h, cy + h, conf)


class TestMechanismA_Size:
    """А: размер кандидата участвует в счёте, дикое расхождение — вето."""

    def test_disabled_picks_nearest_regardless_of_size(self):
        cfg = make_cfg(ENABLE_SIZE_SCORING=False)
        ts = TrackState(cfg, 500, 500, 100, 640, 1080)
        near_wrong_size = box(540, 500, 400)   # ближе, но вчетверо крупнее
        far_right_size = box(650, 500, 100)
        r = ts.step(0.33, [near_wrong_size, far_right_size])
        assert r.chosen == near_wrong_size

    def test_enabled_prefers_size_consistent_candidate(self):
        """Решает именно СЛАГАЕМОЕ счёта, а не вето: оба кандидата внутри
        полосы вето (1.5 < 1.8), так что отбросить ближнего можно только
        штрафом за размер. Первая версия этого теста брала кандидата с
        отношением 4.0, который отсекался вето, и снятие слагаемого из
        счёта тест не ловил вовсе."""
        cfg = make_cfg(ENABLE_SIZE_SCORING=True, SIZE_LAMBDA=0.5, SIZE_VETO_RATIO=1.8)
        ts = TrackState(cfg, 500, 500, 100, 640, 1080)
        near_wrong_size = box(540, 500, 150)   # 40px, но в 1.5 раза крупнее
        far_right_size = box(650, 500, 100)    # 150px, размер точный
        r = ts.step(0.33, [near_wrong_size, far_right_size])
        assert r.n_vetoed == 0, "оба обязаны пройти вето — решает счёт"
        assert r.chosen == far_right_size, "штраф за размер обязан перевесить близость"

    def test_veto_rejects_candidate_outside_ratio_band(self):
        cfg = make_cfg(ENABLE_SIZE_SCORING=True, SIZE_VETO_RATIO=1.8)
        ts = TrackState(cfg, 500, 500, 100, 640, 1080)
        r = ts.step(0.33, [box(505, 500, 400)])  # отношение 4.0 > 1.8
        assert r.chosen is None
        assert r.n_vetoed == 1

    def test_veto_keeps_candidate_inside_band(self):
        cfg = make_cfg(ENABLE_SIZE_SCORING=True, SIZE_VETO_RATIO=1.8)
        ts = TrackState(cfg, 500, 500, 100, 640, 1080)
        r = ts.step(0.33, [box(505, 500, 150)])  # отношение 1.5 < 1.8
        assert r.chosen is not None

    def test_score_is_in_pixels_not_mixed_units(self):
        """Лог-член домножен на сторону окна: иначе безразмерная величина
        складывается с пикселями и вклад механизма зависит от разрешения.
        Кандидат смещён от предсказания, чтобы базовое расстояние было
        НЕнулевым: при нулевом слагаемое исчезает и тест проходит даже без
        механизма (0 == 0*10)."""
        cfg = make_cfg(ENABLE_SIZE_SCORING=True, SIZE_LAMBDA=1.0)
        d = box(550, 500, 200)   # 50px от предсказания, размер вдвое больше
        base = 50.0
        s_small, _ = score_candidate(d, 500, 500, 100, 500, 500, (0, 0), 0.33, 100, cfg)
        s_big, _ = score_candidate(d, 500, 500, 100, 500, 500, (0, 0), 0.33, 1000, cfg)
        assert s_big > s_small, "слагаемое размера обязано быть в счёте"
        assert (s_big - base) == pytest.approx(10 * (s_small - base), rel=1e-6)


class TestMechanismB_Occlusion:
    """Б: два кандидата сошлись — не выбирать никого M тактов."""

    def test_trigger_requires_two_close_candidates(self):
        assert occlusion_triggered([box(500, 500, 50), box(520, 500, 50)], 640, 0.25)
        assert not occlusion_triggered([box(500, 500, 50), box(1100, 500, 50)], 640, 0.25)
        assert not occlusion_triggered([box(500, 500, 50)], 640, 0.25)

    def test_disabled_picks_one_of_the_two(self):
        cfg = make_cfg(ENABLE_OCCLUSION_HOLD=False)
        ts = TrackState(cfg, 500, 500, 100, 640, 1080)
        r = ts.step(0.33, [box(505, 500, 100), box(525, 500, 100)])
        assert r.chosen is not None
        assert r.occluded is False

    def test_enabled_holds_and_picks_nobody(self):
        cfg = make_cfg(ENABLE_OCCLUSION_HOLD=True)
        ts = TrackState(cfg, 500, 500, 100, 640, 1080)
        r = ts.step(0.33, [box(505, 500, 100), box(525, 500, 100)])
        assert r.occluded is True
        assert r.chosen is None

    def test_hold_does_not_count_as_miss_and_does_not_grow_window(self):
        """Пауза — не пропуск: иначе M тактов окклюзии уводят в потерю и
        раздувают окно ровно там, где нужно сидеть тихо."""
        cfg = make_cfg(ENABLE_OCCLUSION_HOLD=True, MISS_TO_LOST_N=3)
        ts = TrackState(cfg, 500, 500, 100, 640, 1080)
        side0 = ts.current_window_side()
        pair = [box(505, 500, 100), box(525, 500, 100)]
        for _ in range(4):
            r = ts.step(0.33, pair)
            assert r.status == STATUS_TRACKING
            assert r.miss_count == 0
        assert ts.current_window_side() == pytest.approx(side0)

    def test_hold_expires_after_m_ticks(self):
        cfg = make_cfg(ENABLE_OCCLUSION_HOLD=True, OCCLUSION_HOLD_TICKS=2)
        ts = TrackState(cfg, 500, 500, 100, 640, 1080)
        pair = [box(505, 500, 100), box(525, 500, 100)]
        assert ts.step(0.33, pair).occluded is True
        assert ts.step(0.33, pair).occluded is True
        r = ts.step(0.33, pair)
        assert r.occluded is False, "пауза обязана истечь через M тактов"
        assert r.chosen is not None

    def test_hold_exits_early_when_candidates_diverge(self):
        cfg = make_cfg(ENABLE_OCCLUSION_HOLD=True, OCCLUSION_HOLD_TICKS=4)
        ts = TrackState(cfg, 500, 500, 100, 640, 1080)
        assert ts.step(0.33, [box(505, 500, 100), box(525, 500, 100)]).occluded is True
        r = ts.step(0.33, [box(505, 500, 100)])  # сосед ушёл
        assert r.occluded is False
        assert r.chosen is not None

    def test_trigger_seen_before_size_veto_removes_second_candidate(self):
        """Явное требование тикета: Б проверяется ДО вето А. Иначе вето
        съедает второго кандидата и пересечение становится невидимым."""
        cfg = make_cfg(ENABLE_OCCLUSION_HOLD=True, ENABLE_SIZE_SCORING=True,
                        SIZE_VETO_RATIO=1.8)
        ts = TrackState(cfg, 500, 500, 100, 640, 1080)
        # сосед вчетверо крупнее — вето А его бы отбросило
        r = ts.step(0.33, [box(505, 500, 100), box(525, 500, 400)])
        assert r.occluded is True


class TestMechanismC_Velocity:
    """В: кандидат, требующий скачка скорости, штрафуется и вето."""

    def _moving(self, cfg):
        ts = TrackState(cfg, 100, 500, 100, 640, 1080)
        ts.step(1.0, [box(200, 500, 100)])   # vx = +100 px/сек
        return ts

    def test_disabled_accepts_backward_jumping_candidate(self):
        cfg = make_cfg(ENABLE_VELOCITY_GATE=False)
        ts = self._moving(cfg)
        r = ts.step(1.0, [box(150, 500, 100)])  # рывок назад
        assert r.chosen is not None

    def test_enabled_vetoes_candidate_requiring_wild_velocity(self):
        """Кандидат обязан быть ВНУТРИ дистанционного гейта, иначе его
        отсеет ещё до вето и механизм не при чём. Именно это и есть сценарий
        подмены: отстающий сосед рядом с предсказанием, но движется не так."""
        cfg = make_cfg(ENABLE_VELOCITY_GATE=True, ENABLE_SIZE_SCORING=True,
                        VELOCITY_VETO_MULT=1.0, VELOCITY_GATE_FACTOR=0.5,
                        VELOCITY_GATE_NOISE_ANG_PER_SEC=10.0)
        ts = self._moving(cfg)          # позиция 200, скорость +100 px/сек
        # предсказание 300; кандидат на 150 — в 150px от него (гейт 0.3*640=192
        # пропускает), но требует -50 px/сек вместо +100, скачок 150 при
        # допуске 60
        r = ts.step(1.0, [box(150, 500, 100)])
        assert r.n_candidates == 1, "кандидат обязан пройти дистанционный гейт"
        assert r.n_vetoed == 1
        assert r.chosen is None

    def test_enabled_keeps_candidate_consistent_with_motion(self):
        cfg = make_cfg(ENABLE_VELOCITY_GATE=True, ENABLE_SIZE_SCORING=True)
        ts = self._moving(cfg)
        r = ts.step(1.0, [box(300, 500, 100)])  # ровно по скорости
        assert r.chosen is not None

    def test_velocity_deviation_is_algebraically_the_distance_term(self):
        """Механизм В в формулировке тикета ИЗБЫТОЧЕН, и это надо
        зафиксировать тестом, а не забыть.

        Предсказание строится как pred = prev + v*dt, поэтому
        |implied_v - v| = |cand - pred| / dt тождественно. Значит слагаемое
        и вето механизма В — это базовое расстояние, поделённое на dt, и
        никакой новой информации о кандидате они не несут. Пока тождество
        держится, включать В поверх А бессмысленно.
        """
        prev, v, dt = 200.0, 100.0, 0.7
        pred = prev + v * dt
        for cand in (150.0, 260.0, 305.0, 420.0):
            implied = (cand - prev) / dt
            assert abs(implied - v) == pytest.approx(abs(cand - pred) / dt, rel=1e-9)

    def test_veto_is_symmetric_forward_and_backward(self):
        """Следствие избыточности: рывок ВПЕРЁД и такой же рывок НАЗАД
        штрафуются одинаково, хотя подмену отстающим соседом выдаёт именно
        разворот. Настоящий механизм В должен быть направленным."""
        cfg = make_cfg(ENABLE_VELOCITY_GATE=True, VELOCITY_GATE_FACTOR=0.5,
                        VELOCITY_GATE_NOISE_ANG_PER_SEC=10.0)
        prev, v, dt = 200.0, 100.0, 1.0
        pred = prev + v * dt
        ahead, _ = score_candidate(box(pred + 80, 500, 100), pred, 500, 100,
                                    prev, 500, (v, 0.0), dt, 640, cfg)
        behind, _ = score_candidate(box(pred - 80, 500, 100), pred, 500, 100,
                                     prev, 500, (v, 0.0), dt, 640, cfg)
        assert ahead == pytest.approx(behind)
