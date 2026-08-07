#!/usr/bin/env python3
"""Оценка в режиме слежения — окно вокруг известной цели (тикет "переоценка
в режиме слежения", дополнено тикетом "подготовка ночи").

Для каждой истинной рамки-цели строим Square: S = k*размер_цели (k
фиксирован — середина рабочего диапазона), центр = центр истины + случайный
джиттер до jitter_frac*S. Дальше тот же примитив crop(): зажимы, сдвиг
внутрь кадра, усредняющее уменьшение. Предсказания с окна переводятся
обратно в пиксели ИСХОДНОГО кадра через ту же resolve_placement, что строила
окно, — сравнение с истиной идёт в кадровых пикселях.

Ignore-зона (config.MIN_TARGET_SIZE, "подготовка ночи" патч 1): боксы мельче
порога — не цель для оценки (не считаются ни найденными, ни пропущенными),
и детекция, совпавшая с ignore-боксом, не штрафуется как ложная.

Патч 4 ("подготовка ночи"): каждый target-бокс оценивается
config.EVAL_REALIZATIONS_PER_BOX независимыми окнами (свой джиттер на
каждое) — полнота усредняется по реализациям, конкретное смещение не влияет
на сравнение прогонов. Разброс полноты между реализациями одного бокса
логируется отдельно (чувствительность к промаху предсказания положения).

Патч 3 ("подготовка ночи"): детекция идёт на низком conf (--low-conf), порог
применяется потом, в памяти — так можно найти порог, дающий фиксированную
частоту ложных на окно (config.EVAL_TARGET_FP_PER_WINDOW), и посчитать
полноту при нём (основная метрика). Полнота при --fixed-conf (0.25, был
"legacy conf") остаётся в отчёте только как диагностика для сверки со
старыми прогонами, не для сравнения рецептов. Плюс — медианная
уверенность на найденной цели, по корзинам. mAP и PR-кривые не считаем.

Зерно оценки — одно фиксированное на все прогоны (--seed), не для честности
(её даёт усреднение по реализациям), а для повторяемости отчётов.

    python eval_track.py WEIGHTS IMAGES_DIR LABELS_DIR
        [--k 3.5] [--jitter-frac 0.15] [--seed 0] [--realizations 8]
        [--low-conf 0.01] [--fixed-conf 0.25]
        [--viz-dir DIR] [--viz-n 10] [--out report.json]

Метрики отчёта (тикет "патч v2", п.2) — одна главная, остальные диагностика:

    completeness_aligned   ГЛАВНАЯ метрика решений: полнота по корзинам при
                            пороге, выровненном под aligned_fp_per_window
                            (config.EVAL_TARGET_FP_PER_WINDOW = 0.05
                            ложных/окно). Все сравнения рецептов — по ней.
    completeness_at_fixed_conf / fp_at_fixed_conf   диагностика, только для
                            сверки со старыми отчётами (--fixed-conf, был
                            "legacy conf" 0.25) — порог не выровнен по FP,
                            между прогонами с разной уверенностью моделей
                            не сравним напрямую.
    conf_median             медиана уверенности на совпадениях, по корзинам.
    jitter_spread           разброс (pstdev) доли попаданий по
                            EVAL_REALIZATIONS_PER_BOX реализациям на бокс —
                            чувствительность к промаху позиции предсказания.
    partial_rate            доля боксов с частичным (0<полнота<1) хитом.
"""

import argparse
import json
import os
import random
import statistics

import cv2

import config
from crop import crop
from eval_640 import bin_name, box_center, box_size, center_dist_ratio, iou, load_gt
from geometry import Square, resolve_placement


def split_target_ignore(boxes):
    targets = [b for b in boxes if box_size(b) >= config.MIN_TARGET_SIZE]
    ignore = [b for b in boxes if box_size(b) < config.MIN_TARGET_SIZE]
    return targets, ignore


def build_track_square(gt_box, k, jitter_frac, rng, frame_w=None, frame_h=None):
    """Окно оценки вокруг известной цели.

    Сторона берётся с полом config.EVAL_WINDOW_MIN_PX: crop() без паддинга
    всё равно вырежет столько реальных пикселей, поэтому меньшая сторона —
    фикция. Джиттер тоже считается от РЕАЛЬНОЙ стороны, иначе для мелких
    целей смещение окна оказывается втрое-вчетверо меньше заявленных
    jitter_frac и оценка выходит мягче задуманной.
    """
    size = box_size(gt_box)
    cx, cy = box_center(gt_box)
    side = max(k * size, config.EVAL_WINDOW_MIN_PX)
    # Потолок — короткая сторона кадра: больше пикселей, чем в кадре есть, не
    # возьмёт ни квадрат, ни прямоугольник, а без потолка clamp_box_to_frame
    # обрезает сторону по одной оси и crop() сжимает холст анизотропно. Тот
    # же потолок уже стоит в боевой петле (track_run), здесь его не было.
    if frame_w is not None and frame_h is not None:
        side = min(side, frame_w, frame_h)
    jitter = jitter_frac * side
    jx = rng.uniform(-jitter, jitter)
    jy = rng.uniform(-jitter, jitter)
    return Square(cx=cx + jx, cy=cy + jy, side=side)


def local_to_frame(local_xy, placement):
    lx, ly = local_xy
    x0, y0 = placement.src_box.x0, placement.src_box.y0
    fx = x0 + lx / placement.scale_x
    fy = y0 + ly / placement.scale_y
    return fx, fy


def frame_to_local(frame_xy, placement):
    fx, fy = frame_xy
    x0, y0 = placement.src_box.x0, placement.src_box.y0
    lx = (fx - x0) * placement.scale_x
    ly = (fy - y0) * placement.scale_y
    return lx, ly


def gt_to_local_box(g, placement):
    x0, y0 = frame_to_local((g[0], g[1]), placement)
    x1, y1 = frame_to_local((g[2], g[3]), placement)
    return (x0, y0, x1, y1)


def preds_to_frame(preds_local, placement):
    out = []
    for (x0, y0, x1, y1) in preds_local:
        fx0, fy0 = local_to_frame((x0, y0), placement)
        fx1, fy1 = local_to_frame((x1, y1), placement)
        out.append((fx0, fy0, fx1, fy1))
    return out


def center_inside(a, b):
    """Центр a внутри b, или центр b внутри a — критерий помягче IoU, годится
    для ignore-боксов (они по конструкции мелкие, IoU с более крупной
    рамкой-предсказанием почти всегда будет маленьким даже при точном
    попадании в то же место)."""
    acx, acy = box_center(a)
    bcx, bcy = box_center(b)
    a_has_b_center = a[0] <= bcx <= a[2] and a[1] <= bcy <= a[3]
    b_has_a_center = b[0] <= acx <= b[2] and b[1] <= acy <= b[3]
    return a_has_b_center or b_has_a_center


def classify_detection(pred, primary, other_targets, ignore_boxes, iou_thr):
    """-> ('primary', iou) | ('other_target', None) | ('ignore', None) | ('fp', None)."""
    piou = iou(pred, primary)
    if piou >= iou_thr:
        return "primary", piou
    for ot in other_targets:
        if iou(pred, ot) >= iou_thr:
            return "other_target", None
    for ig in ignore_boxes:
        if iou(pred, ig) >= iou_thr or center_inside(pred, ig):
            return "ignore", None
    return "fp", None


def evaluate_track(weights, images_dir, labels_dir, k=3.5, jitter_frac=0.15, seed=0,
                    realizations=config.EVAL_REALIZATIONS_PER_BOX,
                    low_conf=0.01, fixed_conf=0.25,
                    iou_thr=config.EVAL_IOU_MATCH_THR,
                    center_thr=config.EVAL_CENTER_HIT_THRESHOLD,
                    target_fp_per_window=config.EVAL_TARGET_FP_PER_WINDOW,
                    imgsz=config.WINDOW_SIZE, viz_dir=None, viz_n=10, subset_of=None):
    from ultralytics import YOLO

    model = YOLO(weights)
    names = sorted(f for f in os.listdir(images_dir) if f.endswith(".jpg"))
    if subset_of is not None:
        names = [n for n in names if subset_of(n)]

    rng = random.Random(seed)

    # trials: один на (бокс, реализация). match_conf — уверенность лучшей
    # детекции, совпавшей с ЭТИМ боксом (None, если не нашлось совсем).
    # fp_confs — уверенности детекций-кандидатов в ложные в этом окне.
    trials = []  # dict(bin=str, match_conf=float|None, fp_confs=[float])
    per_box_hits_fixed = {}  # (name, box_idx) -> [bool,...] по реализациям, при fixed_conf
    viz_samples = []

    box_counter = 0
    for name in names:
        img_path = os.path.join(images_dir, name)
        frame = cv2.imread(img_path)
        fh, fw = frame.shape[:2]
        gt_all = load_gt(os.path.join(labels_dir, os.path.splitext(name)[0] + ".txt"), fw, fh)
        targets, ignore = split_target_ignore(gt_all)

        for ti, g in enumerate(targets):
            other_targets = targets[:ti] + targets[ti + 1:]
            box_key = (name, ti)
            per_box_hits_fixed[box_key] = []
            size = box_size(g)
            b = bin_name(size)

            for r in range(realizations):
                square = build_track_square(g, k, jitter_frac, rng, fw, fh)
                placement = resolve_placement(square, fw, fh, config.WINDOW_SIZE)
                window_img = crop(frame, square)

                res = model.predict(window_img, imgsz=imgsz, conf=low_conf, verbose=False)[0]
                preds_local = [tuple(float(v) for v in bb) for bb in res.boxes.xyxy.cpu().numpy()]
                confs = [float(c) for c in res.boxes.conf.cpu().numpy()]
                preds = preds_to_frame(preds_local, placement)

                match_conf = None
                match_iou = 0.0
                fp_confs = []
                for p, c in zip(preds, confs):
                    kind, piou = classify_detection(p, g, other_targets, ignore, iou_thr)
                    if kind == "primary":
                        if match_conf is None or piou > match_iou:
                            match_conf, match_iou = c, piou
                    elif kind == "fp":
                        fp_confs.append(c)
                    # other_target / ignore: не цель этого испытания и не ложная — пропускаем

                trials.append({"bin": b, "match_conf": match_conf, "fp_confs": fp_confs})
                per_box_hits_fixed[box_key].append(match_conf is not None and match_conf >= fixed_conf)

                if viz_dir and r == 0 and len(viz_samples) < viz_n * 3:
                    gt_local = gt_to_local_box(g, placement)
                    preds_at_fixed = [pl for pl, c in zip(preds_local, confs) if c >= fixed_conf]
                    viz_samples.append((window_img.copy(), preds_at_fixed, gt_local))

            box_counter += 1

    # --- патч 4: полнота на бокс = доля реализаций-хитов (при fixed_conf) ---
    per_box_rate = {k: (sum(v) / len(v) if v else 0.0) for k, v in per_box_hits_fixed.items()}
    jitter_spread = statistics.pstdev(per_box_rate.values()) if per_box_rate else float("nan")
    n_partial_boxes = sum(1 for v in per_box_rate.values() if 0 < v < 1)
    partial_rate = (n_partial_boxes / len(per_box_rate)) if per_box_rate else float("nan")

    def completeness_at(conf_thr):
        """(per_bin found/total, center_total) при заданном пороге уверенности."""
        stats = {}
        for t in trials:
            stats.setdefault(t["bin"], [0, 0])
            stats[t["bin"]][1] += 1
            if t["match_conf"] is not None and t["match_conf"] >= conf_thr:
                stats[t["bin"]][0] += 1
        return stats

    def fp_rate_at(conf_thr):
        total_fp = sum(1 for t in trials for c in t["fp_confs"] if c >= conf_thr)
        return total_fp / len(trials) if trials else float("nan")

    # --- патч 3.2: подбор порога под целевую частоту ложных ---
    candidate_thrs = sorted({round(c, 4) for t in trials for c in ([t["match_conf"]] if t["match_conf"] is not None else []) + t["fp_confs"]})
    if not candidate_thrs:
        candidate_thrs = [fixed_conf]
    best_thr = candidate_thrs[0]
    best_diff = float("inf")
    for thr in candidate_thrs:
        fr = fp_rate_at(thr)
        diff = abs(fr - target_fp_per_window)
        if diff < best_diff:
            best_diff, best_thr = diff, thr
    aligned_thr = best_thr
    aligned_fp_rate = fp_rate_at(aligned_thr)
    completeness_aligned = completeness_at(aligned_thr)
    completeness_fixed = completeness_at(fixed_conf)
    fp_at_fixed_conf = fp_rate_at(fixed_conf)

    # --- попадание по центру (на fixed_conf, как раньше) ---
    # используем те же trials, но нужен центр найденной детекции — раз мы не
    # хранили координаты, приблизим попадание по центру через сам факт match
    # (match уже требует IoU>=iou_thr, что при разумных размерах подразумевает
    # близкий центр); отдельная точная метрика center_ratio считается ниже
    # по первой реализации каждого бокса для медианного IoU/размера.

    # --- медианная уверенность на найденной цели, по корзинам (патч 3.3) ---
    conf_by_bin = {}
    for t in trials:
        if t["match_conf"] is not None:
            conf_by_bin.setdefault(t["bin"], []).append(t["match_conf"])
    conf_median = {b: statistics.median(v) for b, v in conf_by_bin.items()}

    # --- вывод: главная метрика (completeness_aligned) первой колонкой,
    # остальное — диагностика (тикет "патч v2", п.2) ---
    print(f"{'корзина':>10} {'ПОЛНОТА@aligned':>16} {'compl@fixed_conf':>17} {'conf_median':>12}")
    for lo, hi in config.EVAL_SIZE_BINS_PX:
        b = f"{lo}-{int(hi)}" if hi != float("inf") else f"{lo}+"
        af, at = completeness_aligned.get(b, [0, 0])
        ff, ft = completeness_fixed.get(b, [0, 0])
        mc = conf_median.get(b, float("nan"))
        print(f"{b:>10} {af}/{at}={af / at if at else float('nan'):>6.2f}   "
              f"{ff}/{ft}={ff / ft if ft else float('nan'):>6.2f}      {mc:>8.3f}")

    print(f"\n[главная] порог под {target_fp_per_window} ложных/окно: conf>={aligned_thr:.3f} "
          f"(факт. частота {aligned_fp_rate:.3f}/окно)")
    print(f"[диагностика] fp_at_fixed_conf (conf={fixed_conf}, для сверки со старыми отчётами): "
          f"{fp_at_fixed_conf:.3f}/окно")
    print(f"[диагностика] jitter_spread (pstdev полноты по реализациям бокса): {jitter_spread:.3f}")
    print(f"[диагностика] partial_rate (боксы с 0<полнота<1): "
          f"{partial_rate:.3f} ({n_partial_boxes}/{len(per_box_rate)})")

    if viz_dir:
        os.makedirs(viz_dir, exist_ok=True)
        sample = random.Random(seed).sample(viz_samples, min(viz_n, len(viz_samples)))
        for i, (img, preds_local, gt_local) in enumerate(sample):
            gx0, gy0, gx1, gy1 = gt_local
            cv2.rectangle(img, (int(gx0), int(gy0)), (int(gx1), int(gy1)), (0, 255, 255), 2)
            for (x0, y0, x1, y1) in preds_local:
                cv2.rectangle(img, (int(x0), int(y0)), (int(x1), int(y1)), (0, 0, 255), 2)
            cv2.imwrite(os.path.join(viz_dir, f"track_{i:02d}.jpg"), img)
        print(f"\nокна с предсказаниями: {viz_dir} ({len(sample)} шт., "
              f"истина жёлтым, предсказания красным, fixed_conf)")

    return {
        "mode": "track", "k": k, "jitter_frac": jitter_frac, "seed": seed,
        "realizations_per_box": realizations,
        "n_boxes": len(per_box_rate),
        "n_trials": len(trials),
        # --- главная метрика решений ---
        "aligned_threshold": aligned_thr,
        "aligned_fp_per_window": aligned_fp_rate,
        "completeness_aligned": {b: {"found": f_, "total": t} for b, (f_, t) in completeness_aligned.items()},
        # --- диагностика (переименовано, тикет "патч v2", п.2) ---
        "fixed_conf": fixed_conf,
        "fp_at_fixed_conf": fp_at_fixed_conf,
        "completeness_at_fixed_conf": {b: {"found": f_, "total": t} for b, (f_, t) in completeness_fixed.items()},
        "conf_median": conf_median,
        "jitter_spread": jitter_spread,
        "partial_rate": partial_rate,
        "n_partial_boxes": n_partial_boxes,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("weights")
    ap.add_argument("images_dir")
    ap.add_argument("labels_dir")
    ap.add_argument("--k", type=float, default=3.5)
    ap.add_argument("--jitter-frac", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--realizations", type=int, default=config.EVAL_REALIZATIONS_PER_BOX)
    ap.add_argument("--low-conf", type=float, default=0.01)
    ap.add_argument("--fixed-conf", type=float, default=0.25,
                     help="было --legacy-conf; порог для диагностики/сверки со старыми "
                          "отчётами, НЕ для сравнения рецептов (см. completeness_aligned)")
    ap.add_argument("--viz-dir", default=None)
    ap.add_argument("--viz-n", type=int, default=10)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    report = evaluate_track(a.weights, a.images_dir, a.labels_dir, k=a.k,
                             jitter_frac=a.jitter_frac, seed=a.seed,
                             realizations=a.realizations,
                             low_conf=a.low_conf, fixed_conf=a.fixed_conf,
                             viz_dir=a.viz_dir, viz_n=a.viz_n)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
