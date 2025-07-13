import numpy as np
import matplotlib.pyplot as plt
from scipy.interpolate import CubicSpline, CubicHermiteSpline

from extrapol_tracker.movement_sim import sample_pts


def predict_movement(X, Y):
    if len(X) == 0:
        return lambda x: 0
    length = min(len(X), 3)
    spl = CubicSpline(X[-length:], Y[-length:])
    # spl = UnivariateSpline(X[-length:], Y[-length:], k=1)
    return spl


def join_traj(cur_traj, goal_traj, tl, tr):
    yl, yr = cur_traj(tl), goal_traj(tr)
    dl, dr = cur_traj.derivative()(tl), goal_traj.derivative()(tr)
    spl = CubicHermiteSpline([tl, tr], [yl, yr], [dl, dr])
    return spl


def track(join_timeout=2., min_delay=0.2, max_delay=0.5, noise_ampl=1., traj_dt=1., dur=30., grav=0.25, thd_ampl=1.,
          draw=False):
    X, Y = sample_pts(min_delay=min_delay, max_delay=max_delay, noise_ampl=noise_ampl, traj_dt=traj_dt, dur=dur,
                      grav=grav, thd_ampl=thd_ampl,
                      draw=draw)
    n = len(X)
    start_pos = 10
    cur_traj = CubicSpline([0, 1], [start_pos, start_pos])

    for i in range(2, n + 1):
        ct = X[i - 1]
        goal_traj = predict_movement(X[:i], Y[:i])
        cur_traj = join_traj(cur_traj, goal_traj, ct, ct + join_timeout)

        if draw:
            # xx = np.linspace(ct, ct + join_timeout, 10)
            nt = X[i] if i < n else dur
            xx = np.linspace(ct, nt, 10)
            plt.plot(xx, goal_traj(xx), linestyle='--')
            plt.plot(xx, cur_traj(xx), linestyle='-', color='red', linewidth=3)


if __name__ == '__main__':
    track(join_timeout=1, dur=10., noise_ampl=0.1, draw=True)
    plt.legend()
    plt.show()
