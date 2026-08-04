"""Калман в угловых единицах (тикет "ночь", п.2.2).

Состояние: [theta, theta', phi, phi', log h] — положение и скорость по двум
углам плюс ЛОГАРИФМ углового размера. Логарифм, а не размер: размер меняется
мультипликативно (вдвое ближе — вдвое крупнее), в логарифме это линейный
сдвиг, и гауссова модель на него ложится, а на сам размер — нет (у него
жёсткая граница в нуле и правый хвост).

Все параметры выведены из ФИЗИЧЕСКИХ величин, а не подобраны:
  - шум процесса: манёвр цели sigma_a м/с^2, приведённый к угловому
    ускорению делением на опорную дальность (sigma_alpha = sigma_a / D);
  - начальная неопределённость скорости: "цель может идти v_max м/с на
    расстоянии D_min" -> v_max / D_min рад/с;
  - шум измерения положения: доля углового размера цели (детектор ошибается
    пропорционально размеру рамки, а не абсолютно).
Ковариация НЕ раздувается вручную ни на пропуске, ни на окклюзии: рост
неопределённости обязан идти только из Q — иначе гейт перестаёт означать то,
что означает, и сравнивать его с фиксированным радиусом бессмысленно.
"""

import math

import numpy as np

IDX_TH, IDX_DTH, IDX_PH, IDX_DPH, IDX_LOGH = range(5)
POS_IDX = [IDX_TH, IDX_PH]


class KalmanAngularFilter:
    """Интерфейс тот же, что у alpha-beta (см. track_filters.PositionFilter):
    seed / predict (чистая) / advance (закоммитить предсказание) / update.
    Дополнительно отдаёт size (из log h) и gate_distance2 — квадрат
    махаланобисова расстояния кандидата до предсказания.
    """

    HAS_GATE = True

    def __init__(self, cfg):
        self.cfg = cfg
        # м/с^2 на опорной дальности -> рад/с^2
        self.sigma_alpha = cfg.KALMAN_SIGMA_ACCEL_MPS2 / cfg.KALMAN_REF_DISTANCE_M
        # "до v_max м/с на D_min метрах" -> рад/с
        self.v_max_ang = cfg.KALMAN_MAX_SPEED_MPS / cfg.KALMAN_MIN_DISTANCE_M
        # скорость изменения log h = -(лучевая скорость)/дальность; лучевая
        # составляющая — доля полной (проезды в основном поперечные)
        self.sigma_logh_rate = cfg.KALMAN_LOGH_RADIAL_FRAC * self.v_max_ang
        self.x = None
        self.P = None
        self.initialized = False

    # --- матрицы модели ---------------------------------------------------
    def _F(self, dt):
        F = np.eye(5)
        F[IDX_TH, IDX_DTH] = dt
        F[IDX_PH, IDX_DPH] = dt
        return F

    def _Q(self, dt):
        """Дискретный белый шум по ускорению (CWNA) на каждую ось + случайное
        блуждание по log h."""
        q = self.sigma_alpha ** 2
        blk = np.array([[dt ** 4 / 4.0, dt ** 3 / 2.0],
                        [dt ** 3 / 2.0, dt ** 2]]) * q
        Q = np.zeros((5, 5))
        Q[0:2, 0:2] = blk
        Q[2:4, 2:4] = blk
        Q[IDX_LOGH, IDX_LOGH] = (self.sigma_logh_rate ** 2) * dt
        return Q

    def _R_pos(self, m_size):
        """sigma положения — доля углового размера ЦЕЛИ. Мелкая цель мерится
        точнее в абсолютных углах, крупная — хуже; фиксированная сигма
        завышала бы доверие к крупной и занижала к мелкой."""
        s = self.cfg.KALMAN_R_POS_SIZE_FRAC * max(m_size, 1e-9)
        return np.eye(2) * (s ** 2)

    def _predict_moments(self, dt):
        F = self._F(dt)
        return F @ self.x, F @ self.P @ F.T + self._Q(dt)

    # --- интерфейс фильтра ------------------------------------------------
    def seed(self, mx, my, m_size=None):
        h = m_size if (m_size is not None and m_size > 0) else self.cfg.KALMAN_SEED_SIZE_FALLBACK
        r = (self.cfg.KALMAN_R_POS_SIZE_FRAC * h) ** 2
        self.x = np.array([mx, 0.0, my, 0.0, math.log(h)], dtype=float)
        # P0: положение — с точностью измерения; скорость — ШИРОКО (нулевая
        # начальная скорость это не знание, а отсутствие знания); размер — с
        # точностью измерения размера.
        self.P = np.diag([r, self.v_max_ang ** 2,
                          r, self.v_max_ang ** 2,
                          self.cfg.KALMAN_R_LOGH ** 2])
        self.initialized = True

    def predict(self, dt):
        """Чистая: состояние не трогает (её зовёт планирование окна каждый
        такт, и мутировать оттуда нельзя)."""
        assert self.initialized, "predict() до первого seed()"
        xp, _ = self._predict_moments(dt)
        return (xp[IDX_TH], xp[IDX_PH])

    def advance(self, dt):
        """Такт без измерения (пропуск/окклюзия): предсказание становится
        состоянием, неопределённость растёт на Q."""
        assert self.initialized, "advance() до первого seed()"
        self.x, self.P = self._predict_moments(dt)

    def gate_distance2(self, mx, my, m_size, dt):
        """Квадрат махаланобисова расстояния кандидата до предсказания, 2 dof.

        Сравнивается с cfg.KALMAN_GATE_CHI2 (9.21 = chi2, 2 степени свободы,
        p=0.01). В отличие от фиксированного радиуса, гейт сам расширяется,
        когда фильтр не уверен (долгий пропуск), и сам сужается, когда трек
        плотный, — вручную ничего подкручивать не нужно.
        """
        xp, Pp = self._predict_moments(dt)
        y = np.array([mx - xp[IDX_TH], my - xp[IDX_PH]])
        S = Pp[np.ix_(POS_IDX, POS_IDX)] + self._R_pos(m_size)
        return float(y @ np.linalg.solve(S, y))

    def update(self, mx, my, dt, m_size=None):
        if not self.initialized:
            self.seed(mx, my, m_size)
            return (self.cx, self.cy)

        xp, Pp = self._predict_moments(dt)
        if m_size is not None and m_size > 0:
            H = np.zeros((3, 5))
            H[0, IDX_TH] = 1.0
            H[1, IDX_PH] = 1.0
            H[2, IDX_LOGH] = 1.0
            z = np.array([mx, my, math.log(m_size)])
            R = np.diag([self._R_pos(m_size)[0, 0], self._R_pos(m_size)[1, 1],
                         self.cfg.KALMAN_R_LOGH ** 2])
        else:
            H = np.zeros((2, 5))
            H[0, IDX_TH] = 1.0
            H[1, IDX_PH] = 1.0
            z = np.array([mx, my])
            R = self._R_pos(math.exp(xp[IDX_LOGH]))

        y = z - H @ xp
        S = H @ Pp @ H.T + R
        K = Pp @ H.T @ np.linalg.inv(S)
        self.x = xp + K @ y
        # форма Джозефа: симметричность и положительная определённость P не
        # теряются от накопления ошибок округления за сотни тактов
        I_KH = np.eye(5) - K @ H
        self.P = I_KH @ Pp @ I_KH.T + K @ R @ K.T
        return (self.cx, self.cy)

    # --- то, что читает петля --------------------------------------------
    @property
    def cx(self):
        return float(self.x[IDX_TH])

    @cx.setter
    def cx(self, value):
        self.x[IDX_TH] = value

    @property
    def cy(self):
        return float(self.x[IDX_PH])

    @cy.setter
    def cy(self, value):
        self.x[IDX_PH] = value

    @property
    def vx(self):
        return float(self.x[IDX_DTH])

    @property
    def vy(self):
        return float(self.x[IDX_DPH])

    @property
    def size(self):
        """Угловой размер цели — из состояния, а не из отдельной EMA
        (в Калман-ветке медленный фильтр размера не нужен: его роль играет
        log h с собственным шумом процесса)."""
        return float(math.exp(self.x[IDX_LOGH]))

    def pos_covariance(self):
        return self.P[np.ix_(POS_IDX, POS_IDX)].copy()
