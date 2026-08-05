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

    def accel_covariance(self):
        """2x2 ковариация углового УСКОРЕНИЯ в мировых осях.

        Изотропная (форма 1) или вытянутая вдоль вектора скорости (форма 2,
        тикет п.4: "дисперсия манёвра вдоль вектора скорости в K раз больше,
        чем поперёк"). След сохраняется: иначе форма 2 отличалась бы от формы
        1 не только анизотропией, но и общим уровнем шума, и сравнение
        показывало бы неизвестно что.

        ВАЖНОЕ ограничение модели: гауссова Q симметрична, поэтому "назад"
        получает ту же увеличенную дисперсию, что и "вперёд". Отличить разворот
        от ускорения анизотропией НЕЛЬЗЯ в принципе — это умеет только
        направленный член (форма 3). Отсюда и содержательный смысл сравнения
        форм 2 и 3.
        """
        s2 = self.sigma_alpha ** 2
        if not getattr(self.cfg, "KALMAN_ANISOTROPIC_Q", False):
            return np.eye(2) * s2
        vx, vy = self.x[IDX_DTH], self.x[IDX_DPH]
        n = math.hypot(vx, vy)
        if n < 1e-12:
            return np.eye(2) * s2   # направления нет — анизотропии тоже
        K = float(self.cfg.KALMAN_ANISO_K)
        along = 2.0 * K / (K + 1.0) * s2
        across = 2.0 / (K + 1.0) * s2
        u = np.array([vx / n, vy / n])
        w = np.array([-u[1], u[0]])
        return along * np.outer(u, u) + across * np.outer(w, w)

    def _Q(self, dt):
        """Дискретный белый шум по ускорению (CWNA) по двум углам сразу +
        случайное блуждание по log h.

        Пишется через полную 2x2 ковариацию ускорения, а не двумя
        независимыми блоками: при анизотропии оси theta и phi связаны, и
        поблочная запись их связь потеряла бы. При изотропной Sigma формула
        сводится ровно к прежней поблочной (закреплено тестом).
        """
        S = self.accel_covariance()
        Q = np.zeros((5, 5))
        pos = (IDX_TH, IDX_PH)
        vel = (IDX_DTH, IDX_DPH)
        for i in range(2):
            for j in range(2):
                Q[pos[i], pos[j]] = dt ** 4 / 4.0 * S[i, j]
                Q[pos[i], vel[j]] = dt ** 3 / 2.0 * S[i, j]
                Q[vel[i], pos[j]] = dt ** 3 / 2.0 * S[i, j]
                Q[vel[i], vel[j]] = dt ** 2 * S[i, j]
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
        состоянием, неопределённость растёт на Q, модуль скорости затухает
        (тикет "счёт кандидата", п.1).

        Затухание — домножение состояния ПОСЛЕ predict, матрицу F не трогаем:
        иначе оно попало бы и в ковариацию, а тикет требует обратного —
        неопределённость должна расти, сжимается только скорость.
        """
        assert self.initialized, "advance() до первого seed()"
        self.x, self.P = self._predict_moments(dt)
        tau = getattr(self.cfg, "EXTRAPOLATION_TAU_SEC", None)
        if tau and tau > 0 and dt > 0:
            k = math.exp(-dt / tau)
            self.x[IDX_DTH] *= k
            self.x[IDX_DPH] *= k

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

    def score_distance2(self, mx, my, m_size, dt):
        """Махаланобис по ТРЁМ координатам (theta, phi, log h) — форма 1 счёта
        кандидата (тикет п.4).

        Отличие от gate_distance2 не только в размерности: там 2 степени
        свободы и порог chi2, здесь величина используется для УПОРЯДОЧИВАНИЯ
        кандидатов. Размер входит третьей координатой, а не отдельным
        слагаемым с подобранным весом: вес ему даёт ковариация, то есть та же
        физика, что и положению.

        -> (d^2, ln|S|). Второе нужно потому, что R зависит от размера САМОГО
        кандидата: крупная рамка объявляется измеренной грубее, её S больше, и
        одно лишь d^2 систематически её поощряет ("ему можно отклоняться"). В
        правдоподобии этот перекос снимает ln|S|; тикет задаёт форму 1 как
        чистое d^2, поэтому ln|S| отдаётся наружу отдельно, а не подмешивается
        молча.
        """
        xp, Pp = self._predict_moments(dt)
        H = np.zeros((3, 5))
        H[0, IDX_TH] = 1.0
        H[1, IDX_PH] = 1.0
        H[2, IDX_LOGH] = 1.0
        size = max(m_size, 1e-12)
        z = np.array([mx, my, math.log(size)])
        y = z - H @ xp
        # Допуск по ПОЛОЖЕНИЮ берётся из ПРЕДСКАЗАННОГО размера цели, а не из
        # размера кандидата. Иначе крупной рамке "позволено" отклоняться:
        # R ~ size^2, и при равном расстоянии кандидат вдвое крупнее получает
        # МЕНЬШИЙ счёт (11.98 против 17.82 на прямом замере), то есть счёт
        # поощряет ровно ту подмену, ради предотвращения которой заведён.
        # Размер кандидата остаётся там, где он и есть измерение, — в третьей
        # координате. Переключатель оставлен, чтобы разницу можно было мерить.
        r_size = size if not getattr(self.cfg, "MAHA_R_FROM_PREDICTED_SIZE", True) \
            else math.exp(xp[IDX_LOGH])
        R = np.diag([self._R_pos(r_size)[0, 0], self._R_pos(r_size)[1, 1],
                     self.cfg.KALMAN_R_LOGH ** 2])
        S = H @ Pp @ H.T + R
        d2 = float(y @ np.linalg.solve(S, y))
        return d2, float(np.linalg.slogdet(S)[1])

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
