#!/usr/bin/env python3
"""Какой критерий сопоставления детекция<->истина лучше (тикет «радиус»).

Повод: на стенде модель нашла парус уверенно (conf 0.791), но центр её рамки
лёг в 53 px от центра размеченной при радиусе зачёта 28 (0.5 x размер).
Разметка и модель расходятся в том, ЧТО считать объектом, а не в точности.

Вопрос поставлен так, чтобы на него можно было ответить числом, а не вкусом:

    критерий тем лучше, чем больше ЗАКОННЫХ совпадений он принимает,
    не начиная принимать ЧУЖУЮ цель.

Поэтому меряются две кривые сразу:

  принято своих  — доля целей, где критерий засчитал детекцию, которая к
                   ЭТОЙ цели ближе всех (в долях размера);
  принято чужих  — доля целей, где критерий засчитал детекцию, которая на
                   деле ближе к ДРУГОЙ размеченной цели. Это подмена, и
                   ослабление критерия обязано за неё платить.

Критерий, у которого при равном «чужих» больше «своих», лучше — и это уже не
мнение. Если ни один не доминирует, честный ответ «неразрешимо».

    python radius_study.py WEIGHTS IMAGES_DIR LABELS_DIR --out study.json
"""
import argparse
import json
import math
import os

import cv2

import config
from crop import crop
from eval_640 import bin_name, box_center, box_size, load_gt
from eval_track import build_track_square, preds_to_frame, split_target_ignore
from geometry import resolve_placement


def pct(sorted_vals, q):
    """Перцентиль ближайшим рангом. Прежде стояло sorted[int(q*n)], что при
    n=20 и q=0.95 даёт индекс 19 — то есть МАКСИМУМ, а не p95. Именно так в
    отчёт попало 0.097 вместо 0.079 по узким целям."""
    if not sorted_vals:
        return float("nan")
    k = max(0, min(len(sorted_vals) - 1, int(math.ceil(q * len(sorted_vals))) - 1))
    return sorted_vals[k]


def norm_dist(pred, gt):
    """Расстояние центров в долях размера истины (размер = большая сторона)."""
    px, py = box_center(pred)
    gx, gy = box_center(gt)
    return math.hypot(px - gx, py - gy) / max(box_size(gt), 1e-9)


def axis_frac(pred, gt):
    """max(|dx|/w, |dy|/h) — «центр внутри рамки, растянутой в 2*frac раз».

    Отличается от радиального ровно на вытянутой рамке: вдоль длинной стороны
    допуск шире, поперёк уже. Для паруса (w/h ~ 0.27) это разные критерии, для
    квадратной цели — один и тот же.
    """
    px, py = box_center(pred)
    gx, gy = box_center(gt)
    w = max(gt[2] - gt[0], 1e-9)
    h = max(gt[3] - gt[1], 1e-9)
    return max(abs(px - gx) / w, abs(py - gy) / h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("weights")
    ap.add_argument("images_dir")
    ap.add_argument("labels_dir")
    ap.add_argument("--k", type=float, default=3.5)
    ap.add_argument("--low-conf", type=float, default=0.05)
    ap.add_argument("--min-conf", type=float, default=0.25,
                     help="детекции слабее этого не рассматриваются вовсе")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from ultralytics import YOLO
    model = YOLO(args.weights)
    names = sorted(f for f in os.listdir(args.images_dir) if f.endswith(".jpg"))

    import random
    rng = random.Random(0)
    rows = []
    for n in names:
        frame = cv2.imread(os.path.join(args.images_dir, n))
        if frame is None:
            continue
        fh, fw = frame.shape[:2]
        boxes = load_gt(os.path.join(args.labels_dir, n[:-4] + ".txt"), fw, fh)
        targets, ignore = split_target_ignore(boxes)
        for ti, g in enumerate(targets):
            others = [t for j, t in enumerate(targets) if j != ti]
            # Окно без джиттера: изучается критерий, а не устойчивость к
            # промаху наведения — джиттер добавил бы своего шума.
            square = build_track_square(g, args.k, 0.0, rng, fw, fh)
            placement = resolve_placement(square, fw, fh, config.WINDOW_SIZE)
            res = model.predict(crop(frame, square), imgsz=config.WINDOW_SIZE,
                                 conf=args.low_conf, verbose=False)[0]
            preds = preds_to_frame(
                [tuple(float(v) for v in bb) for bb in res.boxes.xyxy.cpu().numpy()],
                placement)
            confs = [float(c) for c in res.boxes.conf.cpu().numpy()]
            cand = [(p, c) for p, c in zip(preds, confs) if c >= args.min_conf]
            if not cand:
                rows.append({"name": n, "ti": ti, "size": box_size(g),
                              "bin": bin_name(box_size(g)), "found": False})
                continue
            # ВНИМАНИЕ: это ДРУГАЯ популяция, чем у метрики. Здесь берётся
            # одна лучшая по уверенности детекция на цель, а eval_track
            # смотрит любую совпавшую; петля же выбирает ближайшую к
            # предсказанию и уверенность вообще не учитывает
            # (track_logic.select_target). Поэтому выводы отсюда — про
            # ГЕОМЕТРИЮ совпадений, а не про значения метрики.
            p, c = max(cand, key=lambda x: x[1])
            d_own = norm_dist(p, g)
            d_oth = min((norm_dist(p, o) * box_size(o) / max(box_size(g), 1e-9)
                         for o in others), default=float("inf"))
            # ближе ли к чужой цели В АБСОЛЮТНЫХ пикселях
            px, py = box_center(p)
            abs_own = math.hypot(px - box_center(g)[0], py - box_center(g)[1])
            abs_oth = min((math.hypot(px - box_center(o)[0], py - box_center(o)[1])
                           for o in others), default=float("inf"))
            rows.append({"name": n, "ti": ti, "size": box_size(g),
                          "bin": bin_name(box_size(g)), "found": True,
                          "conf": c, "d_norm": d_own, "axis": axis_frac(p, g),
                          "is_neighbour": abs_oth < abs_own,
                          "w_over_h": (g[2] - g[0]) / max(g[3] - g[1], 1e-9)})
    json.dump({"weights": os.path.abspath(args.weights), "k": args.k,
                "min_conf": args.min_conf, "rows": rows},
               open(args.out, "w"), ensure_ascii=False)

    # --- кривые ---
    found = [r for r in rows if r["found"]]
    print(f"целей {len(rows)}, детекция выше {args.min_conf} есть у {len(found)}")
    print(f"из них ближе к ЧУЖОЙ цели: {sum(1 for r in found if r['is_neighbour'])}")
    print(f"\n{'критерий':>22} {'принято своих':>14} {'принято чужих':>14}")
    n = len(rows)
    for label, key, thrs in (("радиальный", "d_norm", (0.4, 0.5, 0.6, 0.75, 1.0, 1.25)),
                              ("по осям рамки", "axis", (0.5, 0.75, 1.0, 1.25, 1.5))):
        for t in thrs:
            own = sum(1 for r in found if not r["is_neighbour"] and r[key] <= t)
            oth = sum(1 for r in found if r["is_neighbour"] and r[key] <= t)
            print(f"{label + ' ' + str(t):>22} {own}/{n} = {own / n:6.3f}   "
                  f"{oth}/{n} = {oth / n:6.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
