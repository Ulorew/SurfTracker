#!/usr/bin/env python3
"""Один прогон, три числа (тикет, п.7) — оценка модели в боевом (640, окна) режиме.

IMAGES_DIR/LABELS_DIR — окна (640x640) из dataset_gen.py (val-сплит), не
целые кадры: модель училась на этом распределении, тайлинг целых кадров под
инференс — работа трекера, вне рамок этого тикета.

    python eval_640.py WEIGHTS IMAGES_DIR LABELS_DIR
        [--meta-dir DIR] [--imgsz 640] [--conf 0.25]
        [--viz-dir DIR] [--viz-n 10] [--out report.json]

Три числа:
  1. полнота по корзинам размера цели (EVAL_SIZE_BINS_PX) — как в v1
     (eval_size_bins.py): истина найдена, если есть предсказание с
     IoU >= EVAL_IOU_MATCH_THR.
  2. попадание по центру: |центр_pred - центр_true| / размер_true, хитом
     считается <= EVAL_CENTER_HIT_THRESHOLD. Печатается общий hit-rate,
     сохраняется CDF (report["center_hit_curve_png"]) — это основная кривая.
  3. ложные срабатывания на кадр, разбивка по фону (вода/берег/небо) — через
     META_DIR (пишет dataset_gen.py), горизонт которой сохранён на кадр.
     Без META_DIR разбивка недоступна, все FP попадают в "unknown".

Дополнительно: медианный IoU совпавших пар (перекрытие рамок), медианное
отношение предсказанного размера к истинному (сторож масштаба — далеко от
1.0 значит центр ловится, а масштаб потерян). Боксы мельче нижней корзины
EVAL_SIZE_BINS_PX попадают в отдельный бакет "<{нижняя граница}" в size_bins.
"""

import argparse
import json
import os
import random
import statistics
from collections import Counter

import config


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def box_size(b):
    return max(b[2] - b[0], b[3] - b[1])


def box_center(b):
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def center_dist_ratio(gt, pred):
    gcx, gcy = box_center(gt)
    pcx, pcy = box_center(pred)
    size = box_size(gt)
    if size <= 0:
        return float("inf")
    d = ((gcx - pcx) ** 2 + (gcy - pcy) ** 2) ** 0.5
    return d / size


def load_gt(lbl_path, w, h):
    boxes = []
    if os.path.exists(lbl_path):
        for line in open(lbl_path):
            if not line.strip():
                continue
            _, cx, cy, bw, bh = map(float, line.split())
            boxes.append(((cx - bw / 2) * w, (cy - bh / 2) * h,
                          (cx + bw / 2) * w, (cy + bh / 2) * h))
    return boxes


def default_meta_dir(labels_dir):
    if "labels" in labels_dir:
        return labels_dir.replace("labels", "meta")
    return None


def load_meta(meta_dir, name):
    if not meta_dir:
        return None
    path = os.path.join(meta_dir, os.path.splitext(name)[0] + ".meta.json")
    if not os.path.exists(path):
        return None
    return json.load(open(path))


def classify_background(meta, local_cx, local_cy):
    x0, y0, x1, y1 = meta["src_box"]
    fx = x0 + (local_cx - meta["off_x"]) / meta["scale_x"]
    fy = y0 + (local_cy - meta["off_y"]) / meta["scale_y"]
    band = config.EVAL_COAST_BAND_FRAC_OF_FRAME_H * meta["frame_h"]
    hz = meta["horizon_y"]
    if fy < hz - band:
        return "sky"
    if fy > hz + band:
        return "water"
    return "coast"


def match_by_center(gts, preds, threshold):
    """Жадно: каждой истине — ближайшее ещё не занятое предсказание.

    Возвращает (пары (gt_idx, pred_idx|None), индексы непристроенных
    предсказаний — это и есть ложные срабатывания).
    """
    remaining = list(range(len(preds)))
    pairs = []
    for gi, g in enumerate(gts):
        best_pi, best_ratio = None, None
        for pi in remaining:
            ratio = center_dist_ratio(g, preds[pi])
            if best_ratio is None or ratio < best_ratio:
                best_pi, best_ratio = pi, ratio
        if best_pi is not None and best_ratio <= threshold:
            pairs.append((gi, best_pi))
            remaining.remove(best_pi)
        else:
            pairs.append((gi, None))
    matched = {pi for _, pi in pairs if pi is not None}
    fp_idx = [pi for pi in range(len(preds)) if pi not in matched]
    return pairs, fp_idx


def bin_name(size):
    for lo, hi in config.EVAL_SIZE_BINS_PX:
        if lo <= size < hi:
            return f"{lo}-{int(hi)}" if hi != float("inf") else f"{lo}+"
    return f"<{config.EVAL_SIZE_BINS_PX[0][0]}"


def evaluate(weights, images_dir, labels_dir, meta_dir=None, imgsz=config.TRAIN_IMGSZ,
             conf=0.25, iou_thr=config.EVAL_IOU_MATCH_THR,
             center_thr=config.EVAL_CENTER_HIT_THRESHOLD, viz_dir=None, viz_n=10):
    import cv2
    from ultralytics import YOLO

    if meta_dir is None:
        meta_dir = default_meta_dir(labels_dir)

    model = YOLO(weights)
    names = sorted(f for f in os.listdir(images_dir) if f.endswith(".jpg"))

    size_bin_stats = {}
    center_hits, center_total = 0, 0
    center_ratios = []       # для CDF, только по совпавшим парам
    iou_matched = []
    size_ratios = []
    fp_by_bg = Counter()
    fp_total = 0
    per_image_viz = []

    for name in names:
        img_path = os.path.join(images_dir, name)
        im = cv2.imread(img_path)
        h, w = im.shape[:2]
        gt = load_gt(os.path.join(labels_dir, os.path.splitext(name)[0] + ".txt"), w, h)
        r = model.predict(img_path, imgsz=imgsz, conf=conf, verbose=False)[0]
        preds = [tuple(float(v) for v in b) for b in r.boxes.xyxy.cpu().numpy()]

        # 1. полнота по корзинам (как в v1: любой IoU >= порога)
        found_flags = []
        for g in gt:
            size = box_size(g)
            hit = any(iou(g, p) >= iou_thr for p in preds)
            found_flags.append(hit)
            b = bin_name(size)
            size_bin_stats.setdefault(b, [0, 0])
            size_bin_stats[b][0] += hit
            size_bin_stats[b][1] += 1

        # 2. попадание по центру + перекрытие/масштаб на совпавших парах
        pairs, fp_idx = match_by_center(gt, preds, center_thr)
        for gi, pi in pairs:
            center_total += 1
            if pi is not None:
                center_hits += 1
                center_ratios.append(center_dist_ratio(gt[gi], preds[pi]))
                iou_matched.append(iou(gt[gi], preds[pi]))
                gsize = box_size(gt[gi])
                if gsize > 0:
                    size_ratios.append(box_size(preds[pi]) / gsize)

        # 3. ложные срабатывания по фону
        meta = load_meta(meta_dir, name)
        for pi in fp_idx:
            fp_total += 1
            if meta is not None:
                pcx, pcy = box_center(preds[pi])
                bg = classify_background(meta, pcx, pcy)
            else:
                bg = "unknown"
            fp_by_bg[bg] += 1

        per_image_viz.append((name, gt, preds, found_flags))

    # --- вывод -----------------------------------------------------------
    print(f"{'корзина':>10} {'найдено':>8} {'всего':>6} {'полнота':>8}")
    tiny_label = f"<{config.EVAL_SIZE_BINS_PX[0][0]}"
    bin_labels = [(f"{lo}-{int(hi)}" if hi != float('inf') else f"{lo}+") for lo, hi in config.EVAL_SIZE_BINS_PX]
    for b in [tiny_label] + bin_labels:
        f_, t = size_bin_stats.get(b, [0, 0])
        if t == 0 and b == tiny_label:
            continue  # обычно пусто (боксы мельче нижней корзины — редкость/ignore-зона выше по стеку)
        print(f"{b:>10} {f_:>8} {t:>6} {f_ / t if t else float('nan'):>8.2f}")

    center_hit_rate = center_hits / center_total if center_total else float("nan")
    print(f"\nпопадание по центру (<= {center_thr} размера цели): "
          f"{center_hits}/{center_total} = {center_hit_rate:.3f}")

    fp_per_frame = fp_total / len(names) if names else float("nan")
    print(f"\nложные срабатывания: {fp_total} на {len(names)} кадров = {fp_per_frame:.3f}/кадр")
    for bg in ("sky", "coast", "water", "unknown"):
        if fp_by_bg.get(bg):
            print(f"  {bg:>8}: {fp_by_bg[bg]}")

    median_iou = statistics.median(iou_matched) if iou_matched else float("nan")
    median_size_ratio = statistics.median(size_ratios) if size_ratios else float("nan")
    print(f"\nмедианный IoU совпавших пар: {median_iou:.3f}")
    print(f"медианное pred/true отношение размера: {median_size_ratio:.3f} "
          f"(сторож: далеко от 1.0 — центр ловится, масштаб потерян)")

    curve_png = None
    if center_ratios:
        curve_png = _save_center_hit_curve(center_ratios, center_thr, viz_dir or ".")

    if viz_dir:
        _save_viz(images_dir, viz_dir, per_image_viz, viz_n)

    report = {
        "size_bins": {b: {"found": f_, "total": t} for b, (f_, t) in size_bin_stats.items()},
        "center_hit": {"hits": center_hits, "total": center_total, "rate": center_hit_rate,
                       "threshold": center_thr, "curve_png": curve_png},
        "false_positives": {"total": fp_total, "per_frame": fp_per_frame,
                             "by_background": dict(fp_by_bg)},
        "median_iou_matched": median_iou,
        "median_size_ratio": median_size_ratio,
    }
    return report


def _save_center_hit_curve(ratios, threshold, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(out_dir, exist_ok=True)
    xs = sorted(ratios)
    ys = [i / len(xs) for i in range(1, len(xs) + 1)]
    fig, ax = plt.subplots(figsize=(5, 4))
    ax.plot(xs, ys)
    ax.axvline(threshold, color="red", linestyle="--", label=f"порог {threshold}")
    ax.set_xlabel("|центр_pred - центр_true| / размер_true")
    ax.set_ylabel("доля пар (CDF)")
    ax.set_title("Попадание по центру")
    ax.legend()
    path = os.path.join(out_dir, "center_hit_curve.png")
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return path


def _save_viz(images_dir, viz_dir, per_image, viz_n):
    import cv2
    os.makedirs(viz_dir, exist_ok=True)
    random.seed(0)
    sample = random.sample(per_image, min(viz_n, len(per_image)))
    for name, gt, preds, flags in sample:
        im = cv2.imread(os.path.join(images_dir, name))
        for g, hit in zip(gt, flags):
            c = (0, 200, 0) if hit else (0, 220, 220)
            cv2.rectangle(im, (int(g[0]), int(g[1])), (int(g[2]), int(g[3])), c, 2)
        for p in preds:
            cv2.rectangle(im, (int(p[0]), int(p[1])), (int(p[2]), int(p[3])), (0, 0, 255), 1)
        cv2.imwrite(os.path.join(viz_dir, name), im)
    print(f"\nкадры с рамками: {viz_dir} ({len(sample)} шт., "
          f"истина зелёным/жёлтым, предсказания красным)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("weights")
    ap.add_argument("images_dir")
    ap.add_argument("labels_dir")
    ap.add_argument("--meta-dir", default=None)
    ap.add_argument("--imgsz", type=int, default=config.TRAIN_IMGSZ)
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--viz-dir", default=None)
    ap.add_argument("--viz-n", type=int, default=10)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    report = evaluate(a.weights, a.images_dir, a.labels_dir, meta_dir=a.meta_dir,
                       imgsz=a.imgsz, conf=a.conf, viz_dir=a.viz_dir, viz_n=a.viz_n)

    if a.out:
        with open(a.out, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
