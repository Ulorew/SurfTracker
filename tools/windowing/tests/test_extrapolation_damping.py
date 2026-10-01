"""Демпфирование экстраполяции.

Смысл механизма — ограничение ПО ПОСТРОЕНИЮ: без него предсказание на вечной
потере уходит от точки исчезновения линейно и без предела, и окно уезжает
искать цель туда, где её заведомо нет. С затуханием суммарный уход конечен.

Мутация, которую эти тесты обязаны ловить: tau -> бесконечность (или просто
выключенное затухание) — тест улёта падает.
"""
import math
import types

import pytest

import tracking_config as base_cfg
from track_filters import AlphaBetaFilter, make_filter
from track_kalman import KalmanAngularFilter

TAU = base_cfg.EXTRAPOLATION_TAU_SEC
DT = 1.0 / 3.0
V0 = 0.2          # рад/с — примерно проезд поперёк кадра за пару секунд
SIZE = math.radians(2.0)


def cfg(**over):
    d = {k: getattr(base_cfg, k) for k in dir(base_cfg) if k.isupper()}
    d.update(over)
    return types.SimpleNamespace(**d)


def discrete_bound(v0=V0, dt=DT, tau=TAU):
    """Точный предел суммы смещений при затухании ПОСЛЕ шага:
    v0*dt*(1 + q + q^2 + ...) = v0*dt/(1-q), q = exp(-dt/tau).

    Это НЕ заявленный v0*tau: первый шаг делается полной скоростью. При dt=1/3
    и tau=1.5 расхождение +11%. Тест проверяет именно ту формулу, которая
    реализована, и отдельно — что она близка к заявленной.
    """
    q = math.exp(-dt / tau)
    return v0 * dt / (1.0 - q)


def moving_alpha_beta():
    f = AlphaBetaFilter(0.6, 0.3, cfg())
    f.seed(0.0, 0.0)
    f.vx, f.vy = V0, 0.0
    return f


def moving_kalman():
    f = KalmanAngularFilter(cfg())
    f.seed(0.0, 0.0, SIZE)
    f.x[1] = V0
    return f


class TestFlyAwayIsBounded:
    @pytest.mark.parametrize("make", [moving_alpha_beta, moving_kalman])
    def test_prediction_never_leaves_the_bound(self, make):
        f = make()
        for _ in range(400):        # заведомо "вечная" потеря
            f.advance(DT)
        assert f.cx <= discrete_bound() + 1e-12
        assert f.cx == pytest.approx(discrete_bound(), rel=1e-6)

    def test_bound_is_close_to_the_ticket_value(self):
        """Заявленный предел — v0*tau. Реализованная формула даёт
        на 11% больше из-за дискретности; расхождение должно быть именно
        таким, а не произвольным."""
        assert discrete_bound() / (V0 * TAU) == pytest.approx(1.11, abs=0.01)

    @pytest.mark.parametrize("make", [moving_alpha_beta, moving_kalman])
    def test_without_damping_it_flies_away(self, make):
        """Контроль: тот же прогон без затухания уходит в разы дальше. Без
        этого теста предел мог бы выполняться просто потому, что скорость и
        так мала."""
        f = make()
        f.cfg = cfg(EXTRAPOLATION_TAU_SEC=None)
        for _ in range(400):
            f.advance(DT)
        assert f.cx > 10 * discrete_bound()

    def test_direction_is_preserved_only_magnitude_decays(self):
        f = AlphaBetaFilter(0.6, 0.3, cfg())
        f.seed(0.0, 0.0)
        f.vx, f.vy = 0.3, -0.4        # модуль 0.5, направление 3:-4
        f.advance(DT)
        assert f.vy / f.vx == pytest.approx(-4.0 / 3.0, rel=1e-9)
        assert math.hypot(f.vx, f.vy) == pytest.approx(0.5 * math.exp(-DT / TAU), rel=1e-9)


class TestDampingIsOnlyForMissedTicks:
    def test_accepted_measurement_does_not_decay(self):
        """При принятом кандидате затухания нет — иначе фильтр систематически
        занижал бы скорость движущейся цели."""
        f = AlphaBetaFilter(1.0, 1.0, cfg())   # alpha=beta=1: измерение принимается целиком
        f.seed(0.0, 0.0)
        for i in range(1, 8):
            f.update(V0 * DT * i, 0.0, DT)
        assert f.vx == pytest.approx(V0, rel=1e-6)

    def test_decay_per_tick_matches_exp(self):
        f = moving_alpha_beta()
        f.advance(DT)
        assert f.vx == pytest.approx(V0 * math.exp(-DT / TAU), rel=1e-12)


class TestCovarianceIsNotTouched:
    """Прямое требование: затухание сжимает СКОРОСТЬ, а не
    неопределённость. Иначе гейт после долгого пропуска сужался бы ровно
    тогда, когда фильтр знает меньше всего."""

    def test_position_variance_still_grows_while_velocity_decays(self):
        f = moving_kalman()
        p_before = f.pos_covariance()[0, 0]
        v_before = f.vx
        for _ in range(5):
            f.advance(DT)
        assert f.pos_covariance()[0, 0] > p_before
        assert abs(f.vx) < abs(v_before)

    def test_velocity_variance_is_not_shrunk_by_damping(self):
        """Ковариация скорости от затухания не уменьшается: домножается
        состояние, а не P ("F не менять")."""
        f = moving_kalman()
        before = f.P[1, 1]
        f.advance(DT)
        assert f.P[1, 1] >= before


def test_filter_gets_cfg_through_the_factory():
    """Затухание берёт tau из cfg — фильтр обязан получить его при создании,
    иначе механизм молча выключен."""
    f = make_filter(cfg(FILTER_LEVEL=1))
    assert f.cfg is not None
    f.seed(0.0, 0.0)
    f.vx = V0
    f.advance(DT)
    assert f.vx < V0
