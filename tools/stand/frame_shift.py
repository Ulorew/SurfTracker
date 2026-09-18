#!/usr/bin/env python3
"""Сдвиг кадра по видео: фазовая корреляция с ГЕЙТОМ на достоверность пика.

ЗАЧЕМ ГЕЙТ. 18 сентября разбор четырёх записей дал «остановку вала на 4.5
секунды» в одной из них. Остановки не было: сцена - пустая стена, и в
центральном кропе местами нет никакой фактуры. На гладком пятне корреляция
не имеет выраженного пика и возвращает сдвиг около нуля - неотличимо от
неподвижного вала. Стенд в это же время показывал ровный ход, и прав был он.

ЧЕМ ГЕЙТУЕМ. PSR (peak-to-sidelobe ratio): насколько пик выше фона той же
поверхности корреляции. Считается из самих данных, порога «на глаз» не
требует. Кадры ниже порога не усредняются в статистику, а ВЫБРАСЫВАЮТСЯ,
и доля выживших печатается: если её мало, замера просто нет.

ПОЧЕМУ НЕ ПРОВЕРКА ФАКТУРЫ КАДРА. Контраст кропа говорит, есть ли что
ловить, но не говорит, поймалось ли. PSR меряет то, что нужно - качество
самого сопоставления.
"""
import argparse, math, subprocess, sys
import numpy as np

N = 512
PSR_ПОРОГ = 6.0          # ниже - пик неотличим от фона корреляции


def кадры(путь, n=N):
    vf = f"crop={n}:{n}:(iw-{n})/2:(ih-{n})/2,format=gray"
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", путь, "-vf", vf,
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE, bufsize=10**8)
    раз = n * n
    while True:
        b = p.stdout.read(раз)
        if len(b) < раз:
            break
        yield np.frombuffer(b, np.uint8).reshape(n, n).astype(np.float32)
    p.stdout.close(); p.wait()


def пик_и_psr(c):
    """-> (dy, dx, psr). Субпиксельный максимум и его превышение над фоном."""
    i = np.unravel_index(np.argmax(c), c.shape)
    d = []
    for ось, k in enumerate(i):
        n = c.shape[ось]
        м = c[(k - 1) % n, i[1]] if ось == 0 else c[i[0], (k - 1) % n]
        п = c[(k + 1) % n, i[1]] if ось == 0 else c[i[0], (k + 1) % n]
        зн = 2 * (2 * c[i] - м - п)
        d.append(k + ((п - м) / зн if abs(зн) > 1e-12 else 0.0))
    маска = np.ones_like(c, bool)
    r = 5
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            маска[(i[0] + dy) % c.shape[0], (i[1] + dx) % c.shape[1]] = False
    фон = c[маска]
    psr = (c[i] - фон.mean()) / (фон.std() + 1e-12)
    dy, dx = (v - n if v > n / 2 else v for v, n in zip(d, c.shape))
    return dy, dx, psr


def сдвиги(путь):
    """-> массив (dy, dx, psr) на каждую пару соседних кадров."""
    окно = np.outer(np.hanning(N), np.hanning(N)).astype(np.float32)
    пред, out = None, []
    for f in кадры(путь):
        F = np.fft.rfft2((f - f.mean()) * окно)
        if пред is not None:
            R = F * np.conj(пред)
            R /= np.abs(R) + 1e-9
            out.append(пик_и_psr(np.fft.irfft2(R, s=(N, N))))
        пред = F
    return np.array(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("видео", nargs="+")
    ap.add_argument("--fs", type=float, default=29.8)
    ap.add_argument("--psr", type=float, default=PSR_ПОРОГ)
    a = ap.parse_args()
    print(f"СДВИГ КАДРА, кроп {N}x{N}, гейт PSR > {a.psr}")
    print("выброшенные кадры - те, где корреляции не за что зацепиться\n")
    for путь in a.видео:
        s = сдвиги(путь)
        годен = s[:, 2] > a.psr
        print(f"  {путь}: {len(s)} пар, годных {годен.sum()} "
              f"({годен.mean():.0%}), медиана PSR {np.median(s[:,2]):.1f}")
        if годен.sum() < 30:
            print("     годных кадров мало - замера нет")
            continue
        np.save(путь.rsplit(".", 1)[0] + "_shift.npy", s)
    return s


if __name__ == "__main__":
    main()
