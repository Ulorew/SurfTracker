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
import math
import os
import random
import statistics

import cv2

import config
from crop import crop
from eval_640 import bin_name, box_center, box_size, center_dist_ratio, iou, load_gt
from geometry import Square, resolve_placement


# Критерии сопоставления детекция<->истина. Главный — радиальный (тикет
# "камерное зрение": задача наведение, а не обводка). IoU остаётся колонкой
# LEGACY по §5 регламента, радиусы 0.4/0.6 — проверка чувствительности:
# если порядок рецептов зависит от радиуса в этом диапазоне, значит и
# радиальный критерий их не разрешает.
#
# ВНИМАНИЕ, расхождение с трекером: track_eval использует
# GT_HIT_RADIAL_FRAC = 0.6, а здесь тикетом задано 0.5. По ФОРМЕ критерии
# совпали, по КОНСТАНТЕ — нет. Оставлено как есть (тикет явный), 0.6 всё
# равно считается соседней колонкой; сведение констант — решение владельца.
MAIN_CRITERION = f"radial_{config.EVAL_CENTER_HIT_THRESHOLD:g}"
LEGACY_CRITERION = f"iou_{config.EVAL_IOU_MATCH_THR:g}"
CRITERIA = {MAIN_CRITERION: ("radial", config.EVAL_CENTER_HIT_THRESHOLD),
            LEGACY_CRITERION: ("iou", config.EVAL_IOU_MATCH_THR)}
for _f in config.EVAL_RADIAL_SENSITIVITY:
    CRITERIA.setdefault(f"radial_{_f:g}", ("radial", _f))


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


def radial_match(pred, gt, frac):
    """-> (совпало: bool, качество: float). Критерий НАВЕДЕНИЯ.

    Расстояние центров меньше frac от размера истинной рамки; размер —
    большая сторона, как везде в проекте.

    Почему это вместо IoU. Задача — навести камеру, а не обвести объект.
    Физический вопрос «попали ли мы в цель» означает «достаточно ли близок
    центр»: окно центрируется по цели, механика поворачивается на угол.
    IoU на вытянутой рамке паруса (w/h ~ 0.13-0.5) отвечает на другой
    вопрос — насколько совпали ПЛОЩАДИ, — и падает от несовпадения формы
    даже при идеально совпавших центрах. Тот же критерий уже используют
    track_eval и клиповая матрица, так что оценка модели и оценка трекера
    впервые считают одно и то же.

    Качество — насколько близко к центру (1.0 в точку, 0.0 на границе):
    нужно, чтобы из нескольких попавших детекций выбрать лучшую, как это
    делал IoU.
    """
    px, py = box_center(pred)
    gx, gy = box_center(gt)
    r = frac * max(gt[2] - gt[0], gt[3] - gt[1])
    if r <= 0:
        return False, 0.0
    d = math.hypot(px - gx, py - gy)
    return d <= r, max(0.0, 1.0 - d / r)


def classify_detection(pred, primary, other_targets, ignore_boxes, thr,
                        criterion="radial"):
    """-> ('primary', качество) | ('other_target', None) | ('ignore', None) | ('fp', None).

    criterion: "radial" — расстояние центров < thr * размер истины (основной,
    см. radial_match); "iou" — прежний площадной порог, оставлен колонкой
    LEGACY по §5 регламента.

    Критерий применяется ко ВСЕМ трём сопоставлениям, а не только к цели:
    иначе «ложной» осталась бы детекция, попавшая в соседа или в ignore по
    центру, но не по площади, и колонки считали бы разное разным способом.
    """
    if criterion == "iou":
        piou = iou(pred, primary)
        if piou >= thr:
            return "primary", piou
        for ot in other_targets:
            if iou(pred, ot) >= thr:
                return "other_target", None
        for ig in ignore_boxes:
            if iou(pred, ig) >= thr or center_inside(pred, ig):
                return "ignore", None
        return "fp", None

    ok, q = radial_match(pred, primary, thr)
    if ok:
        return "primary", q
    for ot in other_targets:
        if radial_match(pred, ot, thr)[0]:
            return "other_target", None
    for ig in ignore_boxes:
        if radial_match(pred, ig, thr)[0] or center_inside(pred, ig):
            return "ignore", None
    return "fp", None


def evaluate_track(weights, images_dir, labels_dir, k=3.5, jitter_frac=0.15, seed=0,
                    realizations=config.EVAL_REALIZATIONS_PER_BOX,
                    low_conf=0.01, fixed_conf=0.25,
                    iou_thr=config.EVAL_IOU_MATCH_THR,
                    center_thr=config.EVAL_CENTER_HIT_THRESHOLD,
                    target_fp_per_window=config.EVAL_TARGET_FP_PER_WINDOW,
                    imgsz=config.WINDOW_SIZE, viz_dir=None, viz_n=10, subset_of=None,
                    window_ceiling=True):
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
                square = build_track_square(g, k, jitter_frac, rng,
                                             fw if window_ceiling else None,
                                             fh if window_ceiling else None)
                placement = resolve_placement(square, fw, fh, config.WINDOW_SIZE)
                window_img = crop(frame, square)

                res = model.predict(window_img, imgsz=imgsz, conf=low_conf, verbose=False)[0]
                preds_local = [tuple(float(v) for v in bb) for bb in res.boxes.xyxy.cpu().numpy()]
                confs = [float(c) for c in res.boxes.conf.cpu().numpy()]
                preds = preds_to_frame(preds_local, placement)

                # Одна и та же детекция классифицируется КАЖДЫМ критерием:
                # инференс общий, различается только сопоставление. Иначе
                # колонки считались бы на разных прогонах модели, и разница
                # между ними включала бы недетерминизм.
                by = {}
                for cname, (crit, cthr) in CRITERIA.items():
                    m_conf, m_q, fps = None, 0.0, []
                    for p, c in zip(preds, confs):
                        kind, q = classify_detection(p, g, other_targets, ignore,
                                                      cthr, crit)
                        if kind == "primary":
                            # СИЛЬНЕЙШАЯ из совпавших, а не ближайшая. Метрика
                            # отвечает на вопрос "нашлась ли цель при пороге T",
                            # то есть "есть ли ХОТЬ ОДНА совпавшая детекция с
                            # conf >= T". Отбор по близости записывал сюда
                            # уверенность ближней слабой детекции, и цель
                            # считалась пропущенной при живой сильной рядом.
                            # Порог от правки не съезжает: он садится на
                            # уверенности ЛОЖНЫХ, а не совпавших.
                            if m_conf is None or c > m_conf:
                                m_conf, m_q = c, q
                        elif kind == "fp":
                            fps.append(c)
                        # other_target / ignore: не цель этого испытания и не ложная
                    by[cname] = {"match_conf": m_conf, "fp_confs": fps}

                trials.append({"bin": b, "by": by})
                main_conf = by[MAIN_CRITERION]["match_conf"]
                per_box_hits_fixed[box_key].append(main_conf is not None and main_conf >= fixed_conf)

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

    def completeness_at(conf_thr, cname=None):
        """(per_bin found/total) при заданном пороге уверенности."""
        cname = cname or MAIN_CRITERION
        stats = {}
        for t in trials:
            stats.setdefault(t["bin"], [0, 0])
            stats[t["bin"]][1] += 1
            mc = t["by"][cname]["match_conf"]
            if mc is not None and mc >= conf_thr:
                stats[t["bin"]][0] += 1
        return stats

    def fp_rate_at(conf_thr, cname=None):
        cname = cname or MAIN_CRITERION
        total_fp = sum(1 for t in trials for c in t["by"][cname]["fp_confs"] if c >= conf_thr)
        return total_fp / len(trials) if trials else float("nan")

    def align(cname):
        """Порог под целевую частоту ложных — СВОЙ для каждого критерия.

        Общий порог был бы ошибкой: критерии по-разному решают, что считать
        ложной, поэтому и бюджет 0.05/окно достигается на разных порогах.

        budget_reached: если ложных меньше бюджета даже на самом низком
        пороге, подбор вырождается — порог откатывается к нижнему кандидату
        (~low_conf), и полнота получается завышенной ни за что. Раньше это
        молчало; теперь пишется в отчёт (замер: прореживание ложных до 85
        штук поднимало полноту 0.9221 -> 0.9843 без изменения модели).
        """
        cand = sorted({round(c, 4) for t in trials
                       for c in ([t["by"][cname]["match_conf"]]
                                 if t["by"][cname]["match_conf"] is not None else [])
                       + t["by"][cname]["fp_confs"]})
        if not cand:
            cand = [fixed_conf]
        best, best_diff = cand[0], float("inf")
        for thr in cand:
            diff = abs(fp_rate_at(thr, cname) - target_fp_per_window)
            if diff < best_diff:
                best_diff, best = diff, thr
        return {
            "aligned_threshold": best,
            "aligned_fp_per_window": fp_rate_at(best, cname),
            "budget_reached": fp_rate_at(cand[0], cname) >= target_fp_per_window,
            "completeness_aligned": {b_: {"found": f_, "total": t_}
                                      for b_, (f_, t_) in completeness_at(best, cname).items()},
        }

    per_criterion = {cname: align(cname) for cname in CRITERIA}
    main = per_criterion[MAIN_CRITERION]
    aligned_thr = main["aligned_threshold"]
    aligned_fp_rate = main["aligned_fp_per_window"]
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
        mc = t["by"][MAIN_CRITERION]["match_conf"]
        if mc is not None:
            conf_by_bin.setdefault(t["bin"], []).append(mc)
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

    print(f"\n[главная] критерий {MAIN_CRITERION}, порог под {target_fp_per_window} "
          f"ложных/окно: conf>={aligned_thr:.3f} (факт. частота {aligned_fp_rate:.3f}/окно)")
    if not main["budget_reached"]:
        print("[ВНИМАНИЕ] бюджет ложных не достигнут даже на низшем пороге — "
              "порог выродился, полнота завышена, для сравнений НЕ годится")
    print("[критерии] полнота по всем критериям (микро, все корзины):")
    for cname in CRITERIA:
        d = per_criterion[cname]["completeness_aligned"]
        f_ = sum(v["found"] for v in d.values()); t_ = sum(v["total"] for v in d.values())
        mark = " <- главный" if cname == MAIN_CRITERION else (
            " <- LEGACY" if cname == LEGACY_CRITERION else "")
        print(f"    {cname:>12}: {f_}/{t_} = {f_ / t_ if t_ else float('nan'):.4f}  "
              f"порог {per_criterion[cname]['aligned_threshold']:.4f}"
              f"{'' if per_criterion[cname]['budget_reached'] else '  БЮДЖЕТ НЕ ДОСТИГНУТ'}{mark}")
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
        "budget_reached": main["budget_reached"],
        "match_criterion": MAIN_CRITERION,
        "completeness_aligned": {b: {"found": f_, "total": t} for b, (f_, t) in completeness_aligned.items()},
        # --- прежний площадной критерий, для сверки со старыми отчётами ---
        "completeness_aligned_LEGACY_iou": per_criterion[LEGACY_CRITERION]["completeness_aligned"],
        "aligned_threshold_LEGACY_iou": per_criterion[LEGACY_CRITERION]["aligned_threshold"],
        # --- чувствительность к радиусу: все критерии целиком ---
        "by_criterion": per_criterion,
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
    ap.add_argument("--downscale", choices=["area", "linear"], default=None,
                     help="интерполятор уменьшения окна. Обучение идёт на area; "
                          "телефонный путь реализован линейным — этот флаг позволяет "
                          "сверить их на размеченном наборе, а не поверить на слово")
    ap.add_argument("--no-window-ceiling", action="store_true",
                     help="отключить потолок стороны окна оценки короткой стороной кадра "
                          "(добавлен 07.08). Только для СВЕРКИ со старыми числами: без него "
                          "1.18% испытаний получают анизотропную вырезку")
    a = ap.parse_args()

    if a.downscale:
        config.DOWNSCALE_INTERPOLATION = (cv2.INTER_AREA if a.downscale == "area"
                                           else cv2.INTER_LINEAR)
    report = evaluate_track(a.weights, a.images_dir, a.labels_dir, k=a.k,
                             jitter_frac=a.jitter_frac, seed=a.seed,
                             realizations=a.realizations,
                             low_conf=a.low_conf, fixed_conf=a.fixed_conf,
                             viz_dir=a.viz_dir, viz_n=a.viz_n,
                             window_ceiling=not a.no_window_ceiling)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
