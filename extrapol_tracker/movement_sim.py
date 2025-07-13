import numpy as np
import sympy as sp
import matplotlib.pyplot as plt
from scipy.interpolate import CubicSpline

x = sp.symbols('x')


def gen_traj(dt=1., dur=30., grav=0.25, thd_ampl=1., draw=False):
    ct, pos, vel, acc = 0., 0., 0., 0.

    X, Y = [ct], [pos]

    while ct < dur:
        nt = ct + dt

        thd = np.random.uniform(-thd_ampl, thd_ampl) - (acc + vel * 0.1 + pos * 0.01) * grav
        f = thd * ((x - ct) ** 3) + acc * ((x - ct) ** 2) + vel * (x - ct) + pos
        pos = f.subs(x, nt).evalf()
        vel = sp.diff(f, x).subs(x, nt).evalf()
        acc = sp.diff(f, x, 2).subs(x, nt).evalf()
        ct = nt

        X.append(ct)
        Y.append(pos)

    spl = CubicSpline(X, Y)
    xx = np.linspace(0, ct, 51)
    yy = spl(xx)

    if draw:
        plt.plot(xx, yy, '-', label=r'object movement', color='black', linewidth=3)
        # plt.plot(X, Y, 'o', label='pivot points')
        plt.xlabel("time (s)")
        plt.ylabel("position (m)")

    return spl


def sample_pts(min_delay=0.2, max_delay=0.5, noise_ampl=1., traj_dt=1., dur=30., grav=0.25, thd_ampl=1., draw=False):
    spl = gen_traj(traj_dt, dur, grav, thd_ampl, draw=draw)
    ct = 0
    X, Y = [], []
    while ct < dur:
        X.append(ct)
        Y.append(spl(ct) + np.random.uniform(-noise_ampl, noise_ampl))
        ct += np.random.uniform(min_delay, max_delay)

    if draw:
        plt.plot(X, Y, '.', label=r'samples')
    return X, Y


if __name__ == '__main__':
    sample_pts(draw=True, dur=10.)
    plt.show()
