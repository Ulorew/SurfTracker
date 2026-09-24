#!/usr/bin/env python3
"""Резкость движения камеры по видео: скорость ФОНА по углам кадра.

Цель в центре кадра, и камера её держит — по центру панорамы не видно.
Фон по углам честно показывает, как едет камера. Скорость панорамы
(px/кадр) — медиана фазовой корреляции по четырём угловым окнам: человек,
зашедший в один угол, медиану не сдвинет.

Резкость — то, что остаётся от скорости после скользящего среднего 0.5 с:
плавный ход даёт малый остаток, лестница уставки — большой и с пиком на
частоте такта зрения (~4.7 Гц).

    pan_jerk.py видео.mp4 [ещё.mp4 ...]
"""
import subprocess, sys, math
import numpy as np

N = 256

def corners(path):
    w, h = map(int, subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
        "stream=width,height", "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip().split(","))
    xy = [(40, 40), (w - N - 40, 40), (40, h - N - 40), (w - N - 40, h - N - 40)]
    vf = ",".join([]) or None
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-vf", "format=gray", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], stdout=subprocess.PIPE, bufsize=10**8)
    okno = np.outer(np.hanning(N), np.hanning(N)).astype(np.float32)
    prev = None; out = []
    while True:
        b = p.stdout.read(w * h)
        if len(b) < w * h: break
        f = np.frombuffer(b, np.uint8).reshape(h, w)
        F = [np.fft.rfft2((c - c.mean()) * okno) for c in
             (f[y:y+N, x:x+N].astype(np.float32) for x, y in xy)]
        if prev is not None:
            dx = []
            for A, B in zip(F, prev):
                R = A * np.conj(B); R /= np.abs(R) + 1e-9
                c = np.fft.irfft2(R, s=(N, N))
                i = np.unravel_index(np.argmax(c), c.shape)
                k = i[1]; m, pl = c[i[0], (k-1) % N], c[i[0], (k+1) % N]
                den = 2 * (2 * c[i] - m - pl)
                x = k + ((pl - m) / den if abs(den) > 1e-12 else 0.0)
                dx.append(x - N if x > N / 2 else x)
            out.append(np.median(dx))
        prev = F
    return np.array(out)

def main():
    for path in sys.argv[1:]:
        fps = eval(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
            "stream=avg_frame_rate", "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip())
        v = corners(path)
        k = max(3, int(round(0.5 * fps)))
        slow = np.convolve(v, np.ones(k) / k, mode="same")
        r = (v - slow)[k:-k]
        moving = np.abs(slow[k:-k]) > 0.5
        rm = r[moving]
        w = np.hanning(len(rm)); sp = np.abs(np.fft.rfft((rm - rm.mean()) * w)) ** 2
        f = np.fft.rfftfreq(len(rm), 1 / fps)
        band = (f > 3.5) & (f < 6.0)
        share = sp[band].sum() / sp[f > 1.0].sum()
        print(f"{path}:")
        print(f"  кадров {len(v)}, {fps:.1f} к/с; камера в движении {moving.mean():.0%} времени")
        print(f"  средняя |скорость| в движении {np.abs(slow[k:-k][moving]).mean():.2f} px/кадр")
        print(f"  РЕЗКОСТЬ (СКО остатка скорости) {rm.std():.3f} px/кадр, p95 |остатка| {np.percentile(np.abs(rm), 95):.3f}")
        print(f"  доля энергии остатка в 3.5-6 Гц (лестница ~4.7 Гц): {share:.0%}")

if __name__ == "__main__":
    main()
