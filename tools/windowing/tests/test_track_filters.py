"""track_filters.py — уровни 0 и 1."""
import pytest

from track_filters import AlphaBetaFilter, Level0Filter, dist, make_filter


class TestLevel0Filter:
    def test_first_update_seeds_no_velocity(self):
        f = Level0Filter()
        pos = f.update(10.0, 20.0, dt=1.0)
        assert pos == (10.0, 20.0)
        assert f.vx == 0.0 and f.vy == 0.0

    def test_position_is_exactly_last_detection(self):
        f = Level0Filter()
        f.update(10.0, 0.0, dt=1.0)
        pos = f.update(50.0, 0.0, dt=1.0)
        assert pos == (50.0, 0.0)  # позиция = последняя детекция как есть, без сглаживания

    def test_velocity_is_diff_over_dt(self):
        f = Level0Filter()
        f.update(10.0, 0.0, dt=1.0)
        f.update(50.0, 8.0, dt=2.0)
        assert f.vx == pytest.approx((50.0 - 10.0) / 2.0)
        assert f.vy == pytest.approx((8.0 - 0.0) / 2.0)

    def test_predict_extrapolates_by_current_velocity(self):
        f = Level0Filter()
        f.update(0.0, 0.0, dt=1.0)
        f.update(10.0, 0.0, dt=1.0)  # vx=10
        assert f.predict(dt=0.5) == pytest.approx((15.0, 0.0))

    def test_zero_dt_keeps_previous_velocity(self):
        f = Level0Filter()
        f.update(0.0, 0.0, dt=1.0)
        f.update(10.0, 0.0, dt=1.0)
        f.update(999.0, 999.0, dt=0.0)  # dt=0 -> не делить на 0, скорость не трогаем
        assert f.vx == 10.0


class TestAlphaBetaFilter:
    def test_first_update_seeds_no_velocity(self):
        f = AlphaBetaFilter(alpha=0.5, beta=0.5)
        pos = f.update(10.0, 0.0, dt=1.0)
        assert pos == (10.0, 0.0)
        assert f.vx == 0.0

    def test_matches_hand_computed_sequence(self):
        """alpha=beta=0.5, три измерения подряд по прямой — сверено вручную."""
        f = AlphaBetaFilter(alpha=0.5, beta=0.5)
        f.update(10.0, 0.0, dt=1.0)  # seed: cx=10, vx=0
        p2 = f.update(20.0, 0.0, dt=1.0)  # pred=10, resid=10 -> cx=15, vx=5
        assert p2 == pytest.approx((15.0, 0.0))
        assert f.vx == pytest.approx(5.0)
        p3 = f.update(30.0, 0.0, dt=1.0)  # pred=15+5=20, resid=10 -> cx=25, vx=10
        assert p3 == pytest.approx((25.0, 0.0))
        assert f.vx == pytest.approx(10.0)

    def test_smooths_a_noisy_measurement_less_than_raw_jump(self):
        """Скачок измерения сглаживается alpha<1 — не прыгает туда целиком,
        в отличие от Level0Filter."""
        f = AlphaBetaFilter(alpha=0.3, beta=0.3)
        f.update(0.0, 0.0, dt=1.0)
        f.update(0.0, 0.0, dt=1.0)  # стоит на месте, скорость=0
        pos = f.update(100.0, 0.0, dt=1.0)  # внезапный скачок на 100
        assert 0.0 < pos[0] < 100.0  # не долетел целиком, в отличие от Level0

    def test_predict_uses_current_velocity(self):
        f = AlphaBetaFilter(alpha=0.5, beta=0.5)
        f.update(10.0, 0.0, dt=1.0)
        f.update(20.0, 0.0, dt=1.0)  # vx=5 после этого шага
        assert f.predict(dt=2.0)[0] == pytest.approx(15.0 + 5.0 * 2.0)

    def test_velocity_is_per_second_not_per_tick(self):
        """Скорость обязана быть в px/СЕК, а не в px/такт.

        Раньше ВСЕ тесты этого класса шли с dt=1.0, где beta/dt, beta*dt и
        просто beta численно неразличимы — то есть единственное место в
        модуле с физическими единицами не проверялось вовсе. На рабочем
        такте (3 Гц, dt≈0.33) ошибка в единицах даёт систематическое
        отставание окна.
        """
        f = AlphaBetaFilter(alpha=0.5, beta=0.5)
        f.update(10.0, 0.0, dt=0.5)   # seed
        f.update(20.0, 0.0, dt=0.5)   # pred=10, невязка r=10 за dt=0.5
        # скорость невязки = r/dt = 20 px/сек, взвешенная beta=0.5 -> 10 px/сек.
        # при ошибочном beta*dt вышло бы 2.5, при beta без деления — 5.0.
        assert f.vx == pytest.approx(10.0), "vx должна быть в px/сек (beta*r/dt), не в px/такт"

    def test_same_spatial_step_at_half_dt_doubles_velocity(self):
        """Тот же пространственный шаг за вдвое меньшее время = вдвое
        большая скорость. Ловит и beta*dt, и beta без деления."""
        slow = AlphaBetaFilter(alpha=0.5, beta=0.5)
        slow.update(0.0, 0.0, dt=1.0)
        slow.update(10.0, 0.0, dt=1.0)

        fast = AlphaBetaFilter(alpha=0.5, beta=0.5)
        fast.update(0.0, 0.0, dt=0.5)
        fast.update(10.0, 0.0, dt=0.5)

        assert fast.vx == pytest.approx(2 * slow.vx)

    def test_predict_is_pure_does_not_mutate_state(self):
        """track_logic вызывает predict() до решения о цели и полагается на
        то, что состояние при этом не двигается."""
        f = AlphaBetaFilter(alpha=0.5, beta=0.5)
        f.update(10.0, 5.0, dt=1.0)
        f.update(20.0, 5.0, dt=1.0)
        before = (f.cx, f.cy, f.vx, f.vy)
        f.predict(0.33)
        f.predict(0.33)
        assert (f.cx, f.cy, f.vx, f.vy) == before

    def test_zero_dt_does_not_divide_by_zero(self):
        f = AlphaBetaFilter(alpha=0.5, beta=0.5)
        f.update(10.0, 0.0, dt=1.0)
        f.update(20.0, 0.0, dt=1.0)
        v_before = f.vx
        f.update(30.0, 0.0, dt=0.0)  # не должно падать
        assert f.vx == v_before


def _cfg(level):
    import types

    import tracking_config as tcfg
    d = {k: getattr(tcfg, k) for k in dir(tcfg) if k.isupper()}
    d["FILTER_LEVEL"] = level
    return types.SimpleNamespace(**d)


def test_make_filter_dispatch():
    from track_kalman import KalmanAngularFilter
    assert isinstance(make_filter(_cfg(0)), Level0Filter)
    assert isinstance(make_filter(_cfg(1)), AlphaBetaFilter)
    assert isinstance(make_filter(_cfg(2)), KalmanAngularFilter)
    with pytest.raises(ValueError):
        make_filter(_cfg(3))


def test_dist():
    assert dist(0, 0, 3, 4) == pytest.approx(5.0)
