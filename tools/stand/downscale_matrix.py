#!/usr/bin/env python3
"""Чем уменьшать окно до входа сети: матрица 4 варианта x 4 коэффициента.

Повод. Телефон уменьшает окно билинейно (дёшево, стоимость не зависит от S),
обучение идёт на INTER_AREA. Прежняя сверка «0.9744 против 0.9744» гоняла
окна с уменьшением 1.3-1.8, где два интерполятора почти совпадают, — вопрос
там фактически не задавался. На телефонных 4К те же угловые окна дают
уменьшение 2.7 в медиане и ~3.8 в p95, и там билинейка вырождается в почти
точечную выборку.

КРИТЕРИЙ ЗАДАН ДО ЗАМЕРА и он не «картинки похожи», а детекции:
полнота по радиальному критерию (центр детекции ближе 0.5 размера цели) и
медианная уверенность на цели. Эталон — полный INTER_AREA.

ГИПОТЕЗА, ЗАПИСАННАЯ ДО ЗАМЕРА:
  * блочный m x m + билинейный добор неотличим от полного area — он
    математически почти он и есть;
  * 16 отсчётов чуть хуже на факторе 4.8: сетка 4x4 при блоке ~5x5 начинает
    пропускать исходные пиксели;
  * билинейка на 3.8-4.8 теряет заметно. Если НЕ потеряет — это тоже
    результат: вся тревога снимается бесплатно.

ОГОВОРКА ПО ДАННЫМ, тоже до замера. Сцена — размеченные кадры, показанные на
мониторе и переснятые телефоном. Муар и ресемплинг экрана добавляют
высокочастотного мусора, на котором антиалиасинг проявляется РЕЗЧЕ, чем на
воде. Это консервативно: разница будет преувеличена, а не спрятана. Для
ВЫБОРА варианта годится; абсолютные потери полноты отсюда в поле не
переносить.

    python downscale_matrix.py --weights models/....pt --out matrix.json
"""
import argparse
import json
import math
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
OUT = os.path.join(HERE, "out")
NET = 640
SCREEN_W, SCREEN_H = 1920, 1200
FRAME_W, FRAME_H = 1920, 1080
OFF_X, OFF_Y = (SCREEN_W - FRAME_W) // 2, (SCREEN_H - FRAME_H) // 2

sys.path.insert(0, HERE)
from yuv_host import to_rgb  # noqa: E402


def sensor_bgr(base):
    """Полный кадр сенсора, из сырых плоскостей, той же арифметикой, что на
    телефоне. Конвертация ОДНА на все четыре варианта: сравнивается именно
    пересэмплинг, а не цветность (цветовой путь этой матрицей не покрыт).

    Порядок каналов BGR: ultralytics ждёт от numpy того же, что даёт
    cv2.imread, а to_rgb отдаёт RGB."""
    return to_rgb(base)[:, :, ::-1].copy()


# ------------------------- четыре варианта -------------------------
def v_bilinear(crop):
    return cv2.resize(crop, (NET, NET), interpolation=cv2.INTER_LINEAR)


def v_area(crop):
    return cv2.resize(crop, (NET, NET), interpolation=cv2.INTER_AREA)


def v_taps16(crop):
    """16 отсчётов на выходной пиксель по регулярной сетке 4x4 внутри его
    исходного блока. Стоимость ~NET^2*16, то есть от S не зависит."""
    S = crop.shape[0]
    scale = S / NET
    acc = np.zeros((NET, NET, 3), np.float32)
    off = [(i + 0.5) / 4.0 for i in range(4)]
    xs = np.arange(NET, dtype=np.float32)
    for oy in off:
        for ox in off:
            mx = ((xs + ox) * scale - 0.5).astype(np.float32)
            my = ((xs + oy) * scale - 0.5).astype(np.float32)
            mapx = np.tile(mx, (NET, 1))
            mapy = np.repeat(my[:, None], NET, axis=1)
            acc += cv2.remap(crop, mapx, mapy, cv2.INTER_NEAREST,
                              borderMode=cv2.BORDER_REPLICATE).astype(np.float32)
    return np.clip(acc / 16.0 + 0.5, 0, 255).astype(np.uint8)


def v_block(crop):
    """Блок m x m целочисленным усреднением, m = floor(S/640), затем
    билинейный добор до 640. При m == 1 совпадает с билинейкой ПО
    ПОСТРОЕНИЮ — это не совпадение результатов, а тождество."""
    S = crop.shape[0]
    m = max(1, int(S // NET))
    if m == 1:
        return v_bilinear(crop)
    k = (S // m) * m
    c = crop[:k, :k].reshape(k // m, m, k // m, m, 3).mean(axis=(1, 3))
    return cv2.resize(c.astype(np.uint8), (NET, NET), interpolation=cv2.INTER_LINEAR)


VARIANTS = [("билинейка", v_bilinear), ("16 отсчётов", v_taps16),
            ("блок+добор", v_block), ("полный area", v_area)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", required=True)
    ap.add_argument("--factors", type=float, nargs="+", default=[1.7, 2.7, 3.8, 4.8])
    ap.add_argument("--realizations", type=int, default=8)
    ap.add_argument("--jitter", type=float, default=0.15)
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from ultralytics import YOLO
    model = YOLO(args.weights)

    H = np.array(json.load(open(os.path.join(OUT, "calib.result.json")))["H"], float)
    frames = json.load(open(os.path.join(HERE, "frames.json")))
    lab_dir = os.path.join(ROOT, "Datasets/dataset_v6/labels/val")

    rng = np.random.default_rng(args.seed)
    trials = []
    for fr in frames:
        base = os.path.join(OUT, fr["tag"])
        if not os.path.exists(base + ".y"):
            continue
        lp = os.path.join(lab_dir, fr["file"][:-4] + ".txt")
        boxes = [[float(x) for x in l.split()[1:5]] for l in open(lp) if len(l.split()) >= 5]
        if not boxes:
            continue                      # пустые кадры — не про полноту
        img = sensor_bgr(base)
        SH, SW = img.shape[:2]
        for (cx, cy, w, h) in boxes:
            # рамка истины: экранные координаты -> координаты сенсора
            pts = np.array([[[(cx - w / 2) * FRAME_W + OFF_X, (cy - h / 2) * FRAME_H + OFF_Y]],
                             [[(cx + w / 2) * FRAME_W + OFF_X, (cy + h / 2) * FRAME_H + OFF_Y]]],
                            dtype=np.float64)
            s = cv2.perspectiveTransform(pts, H).reshape(2, 2)
            tx0, ty0 = s[0]; tx1, ty1 = s[1]
            tcx, tcy = (tx0 + tx1) / 2, (ty0 + ty1) / 2
            tsize = max(abs(tx1 - tx0), abs(ty1 - ty0))
            for f in args.factors:
                # Прижимаем к кадру, а не отбрасываем: фактор 4.8 даёт 3072
                # при высоте сенсора 3060, и молча потерять верх диапазона
                # значило бы не ответить на главную половину вопроса.
                S = min(int(round(NET * f)), min(SW, SH))
                for r in range(args.realizations):
                    jx, jy = rng.uniform(-args.jitter, args.jitter, 2) * S
                    x0 = int(np.clip(tcx + jx - S / 2, 0, SW - S))
                    y0 = int(np.clip(tcy + jy - S / 2, 0, SH - S))
                    crop = img[y0:y0 + S, x0:x0 + S]
                    # цель в координатах ТЕНЗОРА
                    k = NET / S
                    gx, gy = (tcx - x0) * k, (tcy - y0) * k
                    gs = tsize * k
                    if not (0 <= gx < NET and 0 <= gy < NET):
                        continue          # джиттер выбросил цель из окна
                    for vname, fn in VARIANTS:
                        t = fn(crop)
                        res = model.predict(t, imgsz=NET, conf=0.05, verbose=False)[0]
                        bb = res.boxes.xyxy.cpu().numpy()
                        cf = res.boxes.conf.cpu().numpy()
                        best = None
                        for b, c in zip(bb, cf):
                            if c < args.conf:
                                continue
                            dcx, dcy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
                            if math.hypot(dcx - gx, dcy - gy) <= 0.5 * gs:
                                if best is None or c > best:
                                    best = float(c)
                        trials.append({"tag": fr["tag"], "f": f, "S": S, "r": r,
                                        "variant": vname, "hit": best is not None,
                                        "conf": best, "target_px": gs})

    json.dump({"weights": os.path.abspath(args.weights), "factors": args.factors,
                "realizations": args.realizations, "jitter": args.jitter,
                "conf": args.conf, "seed": args.seed, "trials": trials},
               open(args.out, "w"), ensure_ascii=False)

    # ПАРНОЕ сравнение: кроп у всех четырёх вариантов один и тот же, значит
    # сравнивать краевые доли — терять почти всю чувствительность. Ключ
    # испытания (кадр, цель, фактор, реализация) один, различается вариант.
    import collections
    key = lambda t: (t["tag"], t["f"], t["r"], round(t["target_px"], 3))
    by = collections.defaultdict(dict)
    for t in trials:
        by[key(t)][t["variant"]] = t
    REF = "полный area"
    print("\nПАРНО против эталона (полный area), только испытания, где эталон нашёл цель:")
    print(f"{'вариант':>14} " + " ".join(f"{('x%.1f' % f):>22}" for f in args.factors))
    for vname, _ in VARIANTS:
        if vname == REF:
            continue
        cells = []
        for f in args.factors:
            grp = [v for k, v in by.items() if k[1] == f and REF in v and vname in v]
            ref_hit = [v for v in grp if v[REF]["hit"]]
            lost = sum(1 for v in ref_hit if not v[vname]["hit"])
            gained = sum(1 for v in grp if not v[REF]["hit"] and v[vname]["hit"])
            dc = [v[vname]["conf"] - v[REF]["conf"] for v in ref_hit if v[vname]["hit"]]
            med = float(np.median(dc)) if dc else float("nan")
            cells.append(f"-{lost}/+{gained} из {len(ref_hit)}, dconf {med:+.3f}")
        print(f"{vname:>14} " + " ".join(f"{c:>22}" for c in cells))
    print("\n  -N = эталон нашёл, вариант потерял; +N = наоборот; dconf — медиана разницы уверенности")

    print()
    print(f"{'вариант':>14} " + " ".join(f"{('x%.1f' % f):>14}" for f in args.factors))
    for vname, _ in VARIANTS:
        cells = []
        for f in args.factors:
            t = [x for x in trials if x["variant"] == vname and x["f"] == f]
            if not t:
                cells.append(f"{'—':>14}")
                continue
            hits = [x for x in t if x["hit"]]
            cm = float(np.median([x["conf"] for x in hits])) if hits else float("nan")
            cells.append(f"{len(hits)}/{len(t)}={len(hits)/len(t):.2f} {cm:.2f}")
        print(f"{vname:>14} " + " ".join(f"{c:>14}" for c in cells))
    print("\nв ячейке: попаданий/испытаний=полнота  медиана conf на цели")
    return 0


if __name__ == "__main__":
    sys.exit(main())
