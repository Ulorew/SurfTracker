#!/usr/bin/env python3
"""Сличение переноса трекера на телефон с офлайн-оригиналом.

Тот же детерминированный сценарий гоняется двумя реализациями, и числа
сравниваются по тактам. Перенос, проверенный «на глаз по коду», в этом
проекте уже один раз оказался с другими константами и худшим поведением.

Сравниваются функции, а не весь TrackState: у офлайн-версии в шаге живут
теневые треки, гейт и вето, которых на этапе 1 нет по замыслу. Сверяется
ровно то, что перенесено.
"""
import math, subprocess, sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import track_filters as tf
import track_logic as tl


class Cfg:
    EXTRAPOLATION_TAU_SEC = 1.5


def run_python():
    f = tf.AlphaBetaFilter(0.6, 0.3, Cfg())
    dt = 0.25
    filtered = 200.0
    miss = 0
    out = []
    f.seed(960, 720)
    for k in range(16):
        side = max(640.0, 3.5 * filtered * (1.15 ** min(miss, 64)))
        side = min(side, 1440.0)
        if f.initialized and miss == 0:
            pcx, pcy = f.predict(dt)
        else:
            pcx, pcy = f.predict(dt) if f.initialized else (f.cx, f.cy)
        half = side / 2
        pcx = min(max(pcx, half), 1920 - half)
        pcy = min(max(pcy, half), 1440 - half)
        if 6 <= k <= 8:
            f.advance(dt); miss += 1
        else:
            mx, my, sz = 960 + 40.0 * k, 720 + 5.0 * k, 200 + 6.0 * k
            dets = [(mx - sz / 2, my - sz / 2, mx + sz / 2, my + sz / 2, 0.9),
                    (mx + 300 - sz * 0.7, my - 120 - sz * 0.7,
                     mx + 300 + sz * 0.7, my - 120 + sz * 0.7, 0.95)]
            det, d = tl.select_target(pcx, pcy, dets, side, 0.30)
            if det is None:
                f.advance(dt); miss += 1
            else:
                dcx = (det[0] + det[2]) / 2
                dcy = (det[1] + det[3]) / 2
                dsz = max(det[2] - det[0], det[3] - det[1])
                f.update(dcx, dcy, dt)
                filtered = tl.update_size_filter(filtered, dsz, 0.5, 0.1)
                miss = 0
        out.append((k, f.cx, f.cy, f.vx, f.vy, filtered, side, miss))
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, "..", "..", "..",
                       "android/camfps/src/com/surftracker/camfps/Tracker.java")
    out = os.path.join(here, "build")
    os.makedirs(out, exist_ok=True)
    env = dict(os.environ)
    jh = os.path.expanduser("~/Android/jdk")
    if os.path.isdir(jh):
        env["PATH"] = os.path.join(jh, "bin") + ":" + env.get("PATH", "")
    r = subprocess.run(["javac", "-encoding", "UTF-8", "-d", out, src,
                        os.path.join(here, "PortCheck.java")],
                       capture_output=True, text=True, env=env)
    if r.returncode:
        print("javac:", r.stderr[:800]); return 2
    r = subprocess.run(["java", "-cp", out, "PortCheck"],
                       capture_output=True, text=True, env=env)
    if r.returncode:
        print("java:", r.stderr[:800]); return 2

    java = {}
    for line in r.stdout.splitlines():
        p = line.split()
        if len(p) == 9:
            java[int(p[0])] = [float(x) for x in p[1:7]] + [int(p[7])]

    py = run_python()
    bad = 0
    print(f"{'такт':>5} {'величина':>10} {'python':>12} {'java':>12} {'разница':>10}")
    names = ["cx", "cy", "vx", "vy", "размер", "окно", "промахи"]
    for (k, *vals) in py:
        if k not in java:
            print(f"{k:>5}  нет такта в java"); bad += 1; continue
        for i, nm in enumerate(names):
            a, b = vals[i], java[k][i]
            d = abs(a - b)
            tol = 1e-6 if i < 6 else 0
            if d > tol:
                print(f"{k:>5} {nm:>10} {a:>12.4f} {b:>12.4f} {d:>10.2e}")
                bad += 1
    print()
    print("ИТОГ: перенос СОШЁЛСЯ" if bad == 0 else f"ИТОГ: расхождений {bad}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
