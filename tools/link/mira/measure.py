#!/usr/bin/env python3
"""Предел разрешения по мире, через СОВМЕЩЕНИЕ кадра с эталоном.

Почему так, а не поиском полос в кадре. Три предыдущие редакции искали
развёртку по энергии градиента и каждый раз промахивались: то брали фон за
её продолжение, то не дотягивались до тонкого конца, где контраст падает.
Масштаб выходил заниженным на 10-35%, и «предел» рос вместе с зумом — то
есть измеритель говорил, что при большем зуме камера видит ХУЖЕ.

Здесь кадр совмещается с эталонной мирой гомографией (ORB + RANSAC). После
этого положение любой точки миры в кадре известно точно, а вместе с ним и
период развёртки в этой точке — он задан рисунком, а не измеряется.

    measure.py эталон.png кадр1.png кадр2.png ...
"""
import sys, numpy as np, cv2

X0, ШИР, П0, П1 = 460, 1000, 40.0, 2.0      # развёртка в координатах миры
Y0, ВЫС = 430, 340


def гомография(эталон, кадр):
    orb = cv2.ORB_create(6000)
    k1, d1 = orb.detectAndCompute(эталон, None)
    k2, d2 = orb.detectAndCompute(кадр, None)
    if d1 is None or d2 is None:
        return None, 0
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    пары = sorted(bf.match(d1, d2), key=lambda m: m.distance)[:1200]
    if len(пары) < 30:
        return None, len(пары)
    src = np.float32([k1[m.queryIdx].pt for m in пары]).reshape(-1, 1, 2)
    dst = np.float32([k2[m.trainIdx].pt for m in пары]).reshape(-1, 1, 2)
    H, маска = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
    return H, int(маска.sum()) if маска is not None else 0


def образец(кадр, H, x, полупериодов=3.0):
    """контраст развёртки в точке миры x: снимается ВДОЛЬ строки в кадре"""
    период = П0 + (П1 - П0) * (x - X0) / ШИР
    # масштаб в этой точке: куда уезжает соседний пиксель миры
    a = cv2.perspectiveTransform(np.float32([[[x, Y0 + ВЫС/2]]]), H)[0][0]
    b = cv2.perspectiveTransform(np.float32([[[x + 10, Y0 + ВЫС/2]]]), H)[0][0]
    m = np.hypot(*(b - a)) / 10.0
    if m <= 0.02:
        return период, None, m
    n = int(max(12, полупериодов * период * m))
    ts = np.linspace(x - полупериодов*период/2, x + полупериодов*период/2, n*2)
    проба = []
    for dy in (-ВЫС*0.3, 0.0, ВЫС*0.3):
        точки = np.float32([[[t, Y0 + ВЫС/2 + dy]] for t in ts])
        сп = cv2.perspectiveTransform(точки, H).reshape(-1, 2)
        зн = cv2.remap(кадр, сп[:, 0].reshape(1, -1).astype(np.float32),
                       сп[:, 1].reshape(1, -1).astype(np.float32),
                       cv2.INTER_LINEAR).ravel().astype(np.float32)
        проба.append(зн)
    з = np.mean(проба, 0)
    з = з - cv2.GaussianBlur(з.reshape(-1, 1), (1, 31), 0).ravel()
    return период, float(np.percentile(з, 95) - np.percentile(з, 5)), m


def анализ(эталон, путь, порог=0.5):
    кадр = cv2.imread(путь, cv2.IMREAD_GRAYSCALE)
    H, годных = гомография(эталон, кадр)
    if H is None:
        return None
    точки = []
    for x in range(X0 + 10, X0 + ШИР - 5, 3):
        п, к, m = образец(кадр, H, x)
        if к is not None:
            точки.append((п, к, m))
    if len(точки) < 50:
        return None
    m = float(np.median([t[2] for t in точки]))
    эт = float(np.median([к for п, к, _ in точки if п > 25]))
    предел = min([п for п, к, _ in точки if к >= порог * эт], default=None)
    return dict(m=m, годных=годных, эталон=эт, предел=предел,
                кривая=[(п, к/эт) for п, к, _ in точки])


if __name__ == '__main__':
    файлы = [a for a in sys.argv[1:] if not a.startswith('--')]
    эталон = cv2.imread(файлы[0], cv2.IMREAD_GRAYSCALE)
    print(f'{"кадр":<22}{"совпад.":>9}{"масштаб":>9}'
          f'{"предел мира-px":>16}{"предел кадр-px":>16}')
    for путь in файлы[1:]:
        r = анализ(эталон, путь)
        имя = путь.split('/')[-1].replace('.png', '')
        if r is None:
            print(f'{имя:<22}   совмещение не вышло')
            continue
        пр = r['предел']
        print(f'{имя:<22}{r["годных"]:>9}{r["m"]:>9.3f}'
              f'{(f"{пр:.2f}" if пр else "—"):>16}'
              f'{(f"{пр*r['m']:.2f}" if пр else "—"):>16}')
        if '--кривая' in sys.argv:
            for п, о in r['кривая'][::max(1, len(r['кривая'])//12)]:
                print(f'      период {п:>6.2f}  контраст {о:.2f}')
