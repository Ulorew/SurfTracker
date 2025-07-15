import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
from scipy.interpolate import CubicSpline, CubicHermiteSpline

from extrapol_tracker_sim.movement_sim import sample_pts

import numpy as np
import time


class Kalman1D:
    def __init__(self, pos0, vel0=0, process_var=1.0, meas_var=10.0):
        # Состояние: [позиция, скорость, ускорение]
        self.x = np.array([[pos0], [vel0]])

        # Начальная ковариация
        self.P = np.eye(2) * 100.0

        # Дисперсии
        self.process_var = process_var  # шум модели
        self.meas_var = meas_var  # шум измерения

        # Матрица наблюдения (мы наблюдаем только позицию)
        self.H = np.array([[1, 0]])

        # Дисперсия измерения
        self.R = np.array([[meas_var]])

        self.last_t = None

    def reset(self, pos0=None, t0=None):
        if pos0 is not None:
            self.x = np.array([[pos0], [0]])
        self.P = np.eye(2) * 100.0

        self.last_t = t0

    def predict(self, dt):
        # Модель перехода
        F = np.array([
            [1, dt],
            [0, 1],
        ])
        # Модель шумов (дискретизированная для постоянного ускорения)
        G = np.array([
            [dt],
            [1]
        ])
        # Q = self.process_var * (G @ G.T)
        dynamic_coef = (dt / 0.2)  # (dt ** 2)
        Q = self.process_var * dynamic_coef * (G @ G.T)
        # Предсказание
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q

    def update(self, z):
        # Ошибка
        y = np.array([[z]]) - self.H @ self.x
        S = self.H @ self.P @ self.H.T + self.R
        K = self.P @ self.H.T @ np.linalg.inv(S)  # Калмановское усиление

        # Коррекция
        self.x += K @ y
        I = np.eye(self.P.shape[0])
        self.P = (I - K @ self.H) @ self.P

    def step(self, z, t=None):
        now = t if t is not None else time.time()
        if self.last_t is None:
            self.last_t = now
            return self.x.copy()

        dt = now - self.last_t
        self.last_t = now

        self.predict(dt)
        self.update(z)

        return self.x.copy()

    def forecast(self, dt_future, steps=1):
        # Предсказание вперёд без измерений
        x_pred = self.x.copy()
        for _ in range(steps):
            F = np.array([
                [1, dt_future],
                [0, 1],
            ])
            x_pred = F @ x_pred
        return x_pred


def predict_movement(X, Y):
    if len(X) == 0:
        return lambda x: 0
    if len(X) == 1:
        return CubicSpline([0, 1], [Y[0], Y[0]])
    length = min(len(X), 3)
    spl = CubicSpline(X[-length:], Y[-length:])
    # spl = UnivariateSpline(X[-length:], Y[-length:], k=1)
    return spl


def join_traj(cur_traj, goal_traj, tl, tr):
    yl, yr = cur_traj(tl), goal_traj(tr)
    dl, dr = cur_traj.derivative()(tl), goal_traj.derivative()(tr)
    spl = CubicHermiteSpline([tl, tr], [yl, yr], [dl, dr])
    return spl


def track(join_timeout=2., fov_ampl=31., proc_var=100., meas_var=1., min_delay=0.2, max_delay=0.5, noise_scale=1.,
          traj_dt=2., dur=30., grav=0.2,
          thd_ampl=3., seed=None, draw=False):
    X, Y, real_traj = sample_pts(min_delay=min_delay, max_delay=max_delay, noise_scale=noise_scale, traj_dt=traj_dt,
                                 dur=dur,
                                 grav=grav, thd_ampl=thd_ampl, draw=draw, seed=seed)
    KX = []
    KY = []

    err_sum = 0
    n = len(X)
    start_pos = 5
    cur_traj = CubicSpline([0, 1], [start_pos, start_pos])

    kalman = Kalman1D(start_pos, process_var=proc_var, meas_var=meas_var * (noise_scale ** 2))

    for i in range(n):
        ct = X[i]
        cy = Y[i]

        if abs(cur_traj(ct) - Y[i]) <= fov_ampl or True:
            kalm_info = kalman.step(cy, ct)
            KX.append(ct)
            KY.append(kalm_info[0, 0])
            err_sum += (kalm_info[0, 0] - real_traj(ct)) ** 2
            # err_sum += (cy - real_traj(ct)) ** 2
            goal_traj = predict_movement(KX, KY)
            cur_traj = join_traj(cur_traj, goal_traj, ct, ct + join_timeout)

        if draw:
            nt = X[i + 1] if i + 1 < n else dur
            xx = np.linspace(ct, nt, 10)
            # plt.plot(xx, goal_traj(xx), linestyle='--')
            # plt.plot(xx, cur_traj(xx), linestyle='-', color='red', linewidth=3)
            if abs(cur_traj(ct) - Y[i]) > fov_ampl:
                plt.plot(ct, cur_traj(ct), 'x', color='black')

    if draw:
        plt.plot(KX, KY, '.', color='red')

    RMSE = np.sqrt(err_sum / len(KY))
    # print(f"Kalman RMSE: {RMSE:.3f}")
    # print(f"Kalman RMSE / NOISE_SCALE : {RMSE / noise_scale:.3f}")
    return RMSE / noise_scale


if __name__ == '__main__':

    num_iter = 25
    noise_scale = 10

    print(f"noise_scale: {noise_scale}")

    proc_vars = [60, 100, 250, 600, 1000, 2500]
    meas_vars = [0.1, 0.25, 0.6, 1, 3, 6, 10]
    num_iter = 25  # Количество итераций для усреднения

    # Создаем пустой DataFrame для результатов
    results = pd.DataFrame(
        index=proc_vars,
        columns=meas_vars
    )
    results.index.name = 'proc_var'
    results.columns.name = 'meas_var'

    # Заполняем таблицу результатами
    for proc_var in proc_vars:
        for meas_var in meas_vars:
            cerr = 0
            for seed in range(num_iter):
                # Вызов функции трекинга (замените на реальную реализацию)
                error = track(
                    join_timeout=1,
                    dur=20.0,
                    proc_var=proc_var,
                    meas_var=meas_var,
                    noise_scale=noise_scale,
                    draw=False,
                    fov_ampl=100000000,
                    seed=seed
                )
                cerr += error
            avg_error = cerr / num_iter
            results.loc[proc_var, meas_var] = avg_error
            print(f"proc_var {proc_var}, meas_var {meas_var}: {avg_error:.2f}")

    # Выводим отформатированную таблицу
    print("\nИтоговая таблица результатов:")
    print(results)

    # for seed in range(5):
    #     track(join_timeout=1, dur=10., proc_var=250, noise_scale=25, draw=True, fov_ampl=10000000, seed=seed)
    #     plt.legend()
    #     plt.show()
