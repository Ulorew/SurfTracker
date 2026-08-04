"""Калман в углах (тикет "ночь", п.2.2).

Проверяется не "работает ли матричная арифметика" (её проверяет numpy), а
свойства, ради которых Калман и заводился вместо alpha-beta: ковариация
растёт на пропусках и падает на измерениях, гейт из-за этого дышит сам,
параметры выведены из физических чисел, log h ведёт себя мультипликативно.
"""
import math
import types

import numpy as np
import pytest

import tracking_config as base_cfg
from track_kalman import IDX_LOGH, KalmanAngularFilter


def make_cfg(**overrides):
    d = {k: getattr(base_cfg, k) for k in dir(base_cfg) if k.isupper()}
    d.update(overrides)
    return types.SimpleNamespace(**d)


SIZE = math.radians(2.0)   # угловой размер цели ~2 градуса


def seeded(**overrides):
    f = KalmanAngularFilter(make_cfg(**overrides))
    f.seed(0.0, 0.0, SIZE)
    return f


class TestPhysicalParameters:
    """Числа модели обязаны выводиться из физики, а не подбираться. Если
    кто-то поправит константу в конфиге, эти тесты покажут, во что она
    превратилась в угловых единицах."""

    def test_process_noise_is_accel_over_reference_distance(self):
        f = seeded()
        assert f.sigma_alpha == pytest.approx(
            base_cfg.KALMAN_SIGMA_ACCEL_MPS2 / base_cfg.KALMAN_REF_DISTANCE_M)
        assert f.sigma_alpha == pytest.approx(0.04)  # 2 м/с^2 на 50 м

    def test_initial_velocity_uncertainty_is_max_speed_over_min_distance(self):
        f = seeded()
        assert f.v_max_ang == pytest.approx(1.0)  # 20 м/с на 20 м -> 1 рад/с
        assert f.P[1, 1] == pytest.approx(1.0)
        assert f.P[3, 3] == pytest.approx(1.0)

    def test_position_noise_scales_with_target_size(self):
        """Крупная цель мерится хуже в абсолютных углах — R обязан расти
        вместе с размером, иначе фильтр переоценивает точность крупной."""
        f = seeded()
        small = f._R_pos(SIZE)[0, 0]
        big = f._R_pos(4 * SIZE)[0, 0]
        assert big == pytest.approx(16 * small)  # квадрат отношения размеров
        assert math.sqrt(small) == pytest.approx(base_cfg.KALMAN_R_POS_SIZE_FRAC * SIZE)

    def test_initial_position_uncertainty_matches_measurement(self):
        f = seeded()
        assert f.P[0, 0] == pytest.approx((base_cfg.KALMAN_R_POS_SIZE_FRAC * SIZE) ** 2)


class TestCovarianceBehaviour:
    def test_uncertainty_grows_on_missed_ticks(self):
        f = seeded()
        before = f.pos_covariance()[0, 0]
        f.advance(0.33)
        assert f.pos_covariance()[0, 0] > before

    def test_uncertainty_shrinks_on_measurement(self):
        f = seeded()
        f.advance(0.33)
        grown = f.pos_covariance()[0, 0]
        f.update(0.001, 0.0, 0.33, SIZE)
        assert f.pos_covariance()[0, 0] < grown

    def test_no_manual_inflation_growth_matches_Q_exactly(self):
        """Тикет прямо запрещает раздувать ковариацию руками: рост обязан
        объясняться ТОЛЬКО моделью процесса. Если кто-то добавит множитель
        "на всякий случай", числа разойдутся."""
        f = seeded()
        dt = 0.5
        P0 = f.P.copy()
        F = f._F(dt)
        expected = F @ P0 @ F.T + f._Q(dt)
        f.advance(dt)
        assert np.allclose(f.P, expected)

    def test_covariance_stays_symmetric_and_positive_definite(self):
        f = seeded()
        rng = np.random.default_rng(0)
        for i in range(300):
            if i % 7 == 0:
                f.advance(0.33)
            else:
                f.update(0.01 * i + rng.normal(0, 1e-4), rng.normal(0, 1e-4), 0.33, SIZE)
        assert np.allclose(f.P, f.P.T, atol=1e-15)
        assert np.all(np.linalg.eigvalsh(f.P) > 0), "P потеряла положительную определённость"


class TestMahalanobisGate:
    @staticmethod
    def _settled():
        f = seeded()
        for _ in range(15):
            f.update(0.0, 0.0, 0.33, SIZE)
        return f

    def test_gate_widens_after_misses(self):
        """Главное отличие от фиксированного радиуса: после пропусков фильтр
        знает меньше — и сам начинает принимать то, что раньше отверг."""
        far_th, far_ph = 0.06, 0.0
        confident = self._settled()
        assert confident.gate_distance2(far_th, far_ph, SIZE, 0.33) > base_cfg.KALMAN_GATE_CHI2

        unsure = self._settled()
        for _ in range(4):
            unsure.advance(0.33)
        assert unsure.gate_distance2(far_th, far_ph, SIZE, 0.33) < base_cfg.KALMAN_GATE_CHI2

    def test_gate_is_wide_open_right_after_seeding(self):
        """Следствие широкой P0, заданной тикетом ("цель может идти до 20 м/с
        на 20 м" -> sigma скорости 1 рад/с): на первом же такте после затравки
        или реакквизиции гейт принимает почти весь кадр, и от подмены на этом
        такте защищает не он, а размер окна.

        Тест фиксирует это как известное свойство, а не как сюрприз: если
        строка матрицы "Калман+гейт" даст подмену сразу после захвата,
        объяснение здесь.
        """
        f = seeded()
        assert f.gate_distance2(0.3, 0.0, SIZE, 0.33) < base_cfg.KALMAN_GATE_CHI2
        # ...и это НЕ вечно: несколько измерений — и тот же кандидат отвергнут
        for _ in range(6):
            f.update(0.0, 0.0, 0.33, SIZE)
        assert f.gate_distance2(0.3, 0.0, SIZE, 0.33) > base_cfg.KALMAN_GATE_CHI2

    def test_gate_narrows_on_a_well_tracked_target(self):
        """Обратная сторона: устоявшийся трек с нулевой скоростью обязан
        отсекать соседа, которого широкий стартовый гейт ещё пропускал."""
        f = seeded()
        cand = 0.06
        assert f.gate_distance2(cand, 0.0, SIZE, 0.33) < base_cfg.KALMAN_GATE_CHI2
        for _ in range(15):
            f.update(0.0, 0.0, 0.33, SIZE)
        assert f.gate_distance2(cand, 0.0, SIZE, 0.33) > base_cfg.KALMAN_GATE_CHI2

    def test_gate_threshold_is_the_chi2_quantile(self):
        """9.21 — это квантиль chi2 с 2 степенями свободы на уровне 0.01, а не
        круглое число. Проверяем по определению: доля выборок из настоящего
        распределения невязки, отсечённых гейтом, ~1%."""
        rng = np.random.default_rng(1)
        f = seeded()
        S = f.pos_covariance() + f._R_pos(SIZE)
        L = np.linalg.cholesky(S)
        n = 40000
        y = (L @ rng.standard_normal((2, n))).T
        d2 = np.einsum("ij,ij->i", y, np.linalg.solve(S, y.T).T)
        cut = float(np.mean(d2 > base_cfg.KALMAN_GATE_CHI2))
        assert cut == pytest.approx(0.01, abs=0.003), f"гейт режет {cut:.3%} вместо 1%"

    def test_alpha_beta_has_no_gate(self):
        from track_filters import make_filter
        f = make_filter(make_cfg(FILTER_LEVEL=1))
        assert f.HAS_GATE is False
        assert f.gate_distance2(0.0, 0.0, SIZE, 0.33) is None


class TestLogSizeState:
    def test_size_is_multiplicative_not_additive(self):
        """Смысл log h: приближение цели вдвое — это одинаковый по величине
        шаг состояния и на мелкой, и на крупной цели. У линейного размера так
        не выходит, и фильтр, настроенный на крупную, залипал бы на мелкой."""
        steps_small, steps_big = [], []
        for base, out in ((SIZE, steps_small), (10 * SIZE, steps_big)):
            f = KalmanAngularFilter(make_cfg())
            f.seed(0.0, 0.0, base)
            before = f.x[IDX_LOGH]
            f.update(0.0, 0.0, 0.33, 2 * base)
            out.append(f.x[IDX_LOGH] - before)
        assert steps_small[0] == pytest.approx(steps_big[0], rel=1e-9)

    def test_size_follows_measurements_but_smooths_single_outlier(self):
        f = seeded()
        for _ in range(10):
            f.update(0.0, 0.0, 0.33, SIZE)
        steady = f.size
        f.update(0.0, 0.0, 0.33, 3 * SIZE)   # один выброс детектора
        assert f.size > steady, "фильтр обязан реагировать"
        assert f.size < 2 * SIZE, "но не прыгать за одним измерением"

    def test_sustained_growth_is_followed(self):
        f = seeded()
        s = SIZE
        for _ in range(12):
            s *= 1.1
            f.update(0.0, 0.0, 0.33, s)
        assert f.size == pytest.approx(s, rel=0.15), "устойчивый рост обязан быть отслежен"

    def test_size_never_goes_nonpositive(self):
        """Ради этого размер и хранится в логарифме: сколько бы фильтр ни
        уводило вниз, exp остаётся положительным."""
        f = seeded()
        for _ in range(50):
            f.update(0.0, 0.0, 0.33, SIZE / 50)
        assert f.size > 0


class TestPredictIsPure:
    def test_predict_does_not_mutate_state(self):
        """plan_window зовёт predict каждый такт до принятия решения —
        мутировать оттуда нельзя, иначе состояние уезжает дважды за такт."""
        f = seeded()
        f.update(0.02, 0.01, 0.33, SIZE)
        x0, P0 = f.x.copy(), f.P.copy()
        for _ in range(5):
            f.predict(0.33)
        assert np.array_equal(f.x, x0)
        assert np.array_equal(f.P, P0)

    def test_predict_agrees_with_advance(self):
        f = seeded()
        f.update(0.02, 0.0, 0.33, SIZE)
        pred = f.predict(0.33)
        f.advance(0.33)
        assert (f.cx, f.cy) == pytest.approx(pred)

    def test_constant_velocity_is_extrapolated(self):
        f = seeded()
        for i in range(1, 12):
            f.update(0.01 * i, 0.0, 1.0, SIZE)
        assert f.vx == pytest.approx(0.01, rel=0.2), "скорость не сошлась к истинной"
        assert f.predict(1.0)[0] > f.cx
