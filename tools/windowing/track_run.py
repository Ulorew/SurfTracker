#!/usr/bin/env python3
"""Прогон петли слежения на одной "проездной" папке кадров (тикет "трекинг,
офлайн на видео"). Такт симулируется прореживанием кадров ДО заданного Гц ПО
ТАЙМСТАМПАМ (не константным шагом кадров) — см. manifest.json рядом с
кадрами (пишет write_manifests.py/extract_val_ranges.py).

    python track_run.py --frames-dir Data/frames/val_manual/<range>/ \
        --weights best.pt --tick-hz 3.0 --out run.mp4 --log-out run.jsonl
"""
import argparse
import hashlib
import json
import os

import math

import cv2
import numpy as np

import angles as ang
import config
import tracking_config as tcfg
from crop import crop
from eval_track import local_to_frame, preds_to_frame
from geometry import Square, resolve_placement
from track_eval import gt_box_at, load_gt_track
from track_logic import STATUS_LOST, STATUS_TRACKING, TrackState

# Петля живёт в углах (тикет "ночь", п.2.0): состояние TrackState — радианы,
# пиксели остаются только на входе (детекции) и выходе (вырезка окна, оверлей,
# лог). Здесь — ровно эти две границы перевода.
MAX_ABS_ANGLE = math.pi / 2 - 1e-3  # tan() у пи/2 уходит в бесконечность


def det_to_angles(det, intr, index):
    """Пиксельная детекция (x0,y0,x1,y1,conf) -> угловая, с индексом исходной.

    Углы считаются по УГЛАМ рамки, а не по её центру и размеру: тогда и
    центр (среднее углов), и размер (разность углов) остаются согласованными
    между собой, а обратный переход к пикселям вообще не нужен — исходная
    рамка достаётся по индексу, без потери точности на round-trip.
    """
    x0, y0, x1, y1, conf = det[0], det[1], det[2], det[3], det[4]
    th0, ph0 = ang.px_to_angle(x0, y0, intr)
    th1, ph1 = ang.px_to_angle(x1, y1, intr)
    return (th0, ph0, th1, ph1, conf, index)


def angular_window_to_square(cx_ang, cy_ang, side_ang, intr):
    """Угловое окно -> квадрат в пикселях для crop().

    Сторона — БОЛЬШАЯ из двух пиксельных проекций угловой стороны: tan
    нелинеен, поэтому одна и та же угловая ширина у края кадра занимает
    больше пикселей, чем в центре, и по горизонтали с вертикалью числа
    расходятся. Берём максимум — окно обязано ПОКРЫВАТЬ то, что запросила
    петля; недобор означал бы, что цель, которую петля считает видимой, в
    вырезку не попала.

    Центр квадрата — СЕРЕДИНА пиксельного пролёта, а не пиксель углового
    центра: вдали от оптической оси проекция несимметрична (дальняя от
    центра половина окна растягивается сильнее), и квадрат вокруг углового
    центра срезал бы дальний край. Из-за этого пиксельный центр вырезки
    слегка смещён наружу относительно предсказания — для ЛОГА и метрик
    берётся честный angle_to_px(предсказание), а не этот центр.
    """
    def clamp(a):
        return max(-MAX_ABS_ANGLE, min(MAX_ABS_ANGLE, a))

    u_lo, v_lo = ang.angle_to_px(clamp(cx_ang - side_ang / 2), clamp(cy_ang - side_ang / 2), intr)
    u_hi, v_hi = ang.angle_to_px(clamp(cx_ang + side_ang / 2), clamp(cy_ang + side_ang / 2), intr)
    return Square(cx=(u_lo + u_hi) / 2.0, cy=(v_lo + v_hi) / 2.0,
                   side=max(u_hi - u_lo, v_hi - v_lo))


STATUS_COLOR = {
    STATUS_TRACKING: (0, 200, 0),   # зелёный — ведём
    "miss": (0, 165, 255),           # оранжевый — промах (ещё tracking, но пропуск в этом такте)
    STATUS_LOST: (0, 0, 220),        # красный — потеря
}


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(frames_dir):
    with open(os.path.join(frames_dir, "manifest.json")) as f:
        m = json.load(f)
    m["frames"].sort(key=lambda r: r["timestamp_sec"])
    return m


def decimate_by_timestamp(frames, tick_hz):
    """Ближайший доступный кадр к каждому целевому тактовому времени, шаг
    1/tick_hz от таймстампа первого кадра. Не даёт задвоений подряд."""
    if not frames:
        return []
    period = 1.0 / tick_hz
    start, end = frames[0]["timestamp_sec"], frames[-1]["timestamp_sec"]
    chosen = []
    target = start
    while target <= end + 1e-9:
        best = min(frames, key=lambda r: abs(r["timestamp_sec"] - target))
        if not chosen or chosen[-1]["name"] != best["name"]:
            chosen.append(best)
        target += period
    return chosen


def detect_in_window(model, frame, square, imgsz, low_conf):
    """-> (detections_frame_coords [(x0,y0,x1,y1,conf),...], placement).

    placement считается по config.WINDOW_SIZE, а НЕ по imgsz: холст рисует
    crop(), и он всегда отдаёт config.WINDOW_SIZE (см. crop.py). imgsz — это
    только то, к чему ultralytics приведёт уже готовый холст перед сетью;
    если считать инверсию окно->кадр по imgsz, при imgsz != WINDOW_SIZE
    координаты детекций поедут в масштабе.
    """
    placement = resolve_placement(square, frame.shape[1], frame.shape[0], config.WINDOW_SIZE)
    window_img = crop(frame, square)
    res = model.predict(window_img, imgsz=imgsz, conf=low_conf, verbose=False)[0]
    preds_local = [tuple(float(v) for v in bb) for bb in res.boxes.xyxy.cpu().numpy()]
    confs = [float(c) for c in res.boxes.conf.cpu().numpy()]
    preds_frame = preds_to_frame(preds_local, placement)
    dets = [(x0, y0, x1, y1, c) for (x0, y0, x1, y1), c in zip(preds_frame, confs)]
    return dets, placement


def bootstrap_seed_from_gt(gt_track, ticks):
    """Первый такт, покрытый разметкой (через сплайн track_eval.gt_box_at)
    — если истина есть, она надёжнее модельной затравки по уверенности:
    среди нескольких похожих сёрферов "самая уверенная детекция" может
    оказаться не той целью, что нужна (см. чат тикета — реальный случай)."""
    for i, rec in enumerate(ticks):
        box = gt_box_at(gt_track, rec["timestamp_sec"])
        if box is not None:
            cx, cy, w, h = box
            return i, (cx, cy), max(w, h)
    return None, None, None


def bootstrap_seed(model, frames_dir, frames, imgsz, conf=0.25):
    """Первая детекция на ПОЛНОМ кадре (единственное место в петле, где
    решение принимается по уверенности — состояния/предсказания ещё нет,
    select_target по расстоянию тут неприменим). Пробуем кадры по порядку,
    пока не найдётся хоть одна детекция."""
    for rec in frames:
        frame = cv2.imread(os.path.join(frames_dir, rec["name"]))
        res = model.predict(frame, imgsz=imgsz, conf=conf, verbose=False)[0]
        if len(res.boxes) == 0:
            continue
        confs = [float(c) for c in res.boxes.conf.cpu().numpy()]
        boxes = [tuple(float(v) for v in b) for b in res.boxes.xyxy.cpu().numpy()]
        best_i = max(range(len(confs)), key=lambda i: confs[i])
        x0, y0, x1, y1 = boxes[best_i]
        size = max(x1 - x0, y1 - y0)
        return rec, ((x0 + x1) / 2.0, (y0 + y1) / 2.0), size
    return None, None, None


def draw_overlay(frame, tick_idx, n_ticks, ts_sec, square, detections, chosen, status, miss_count):
    color = STATUS_COLOR[STATUS_LOST] if status == STATUS_LOST else (
        STATUS_COLOR["miss"] if miss_count > 0 else STATUS_COLOR[STATUS_TRACKING])
    x0, y0 = int(square.cx - square.side / 2), int(square.cy - square.side / 2)
    x1, y1 = int(square.cx + square.side / 2), int(square.cy + square.side / 2)
    cv2.rectangle(frame, (x0, y0), (x1, y1), color, 2)
    cv2.drawMarker(frame, (int(square.cx), int(square.cy)), color,
                    markerType=cv2.MARKER_CROSS, markerSize=16, thickness=2)
    for (dx0, dy0, dx1, dy1, dc) in detections:
        is_chosen = chosen is not None and (dx0, dy0, dx1, dy1, dc) == chosen
        c = (255, 255, 0) if is_chosen else (140, 140, 140)
        cv2.rectangle(frame, (int(dx0), int(dy0)), (int(dx1), int(dy1)), c, 1)

    label = {STATUS_TRACKING: "ВЕДУ", STATUS_LOST: "ПОТЕРЯ"}[status]
    if status == STATUS_TRACKING and miss_count > 0:
        label = f"ПРОПУСК ({miss_count})"
    text = f"[{tick_idx+1}/{n_ticks}] t={ts_sec:.2f}s  {label}"
    cv2.putText(frame, text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA)
    return frame


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames-dir", required=True)
    ap.add_argument("--weights", required=True)
    ap.add_argument("--tick-hz", type=float, default=tcfg.TICK_HZ_BASE)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--out", required=True)
    ap.add_argument("--log-out", required=True)
    ap.add_argument("--bootstrap-conf", type=float, default=0.25)
    ap.add_argument("--enable-a", action="store_true",
                     help="механизм А: размер кандидата в счёте + вето по отношению размеров")
    ap.add_argument("--enable-b", action="store_true",
                     help="механизм Б: пауза на окклюзии (кандидаты сошлись — не выбирать)")
    ap.add_argument("--enable-v", action="store_true",
                     help="механизм В: гейт по скорости. ВНИМАНИЕ: в текущей формулировке "
                          "алгебраически тождествен дистанционному слагаемому (см. коммент "
                          "в track_logic.score_candidate) — включать смысла нет")
    ap.add_argument("--enable-vdir", action="store_true",
                     help="механизм В-направленный: штраф и вето за РАЗВОРОТ движения "
                          "(в отличие от --enable-v, не повторяет дистанционное слагаемое)")
    ap.add_argument("--filter-level", type=int, default=None, choices=[0, 1, 2],
                     help="0 = последняя детекция, 1 = alpha-beta (по умолчанию из конфига), "
                          "2 = Калман в углах (theta, theta', phi, phi', log h)")
    ap.add_argument("--enable-gate", action="store_true",
                     help="махаланобисов гейт вместо фиксированного радиуса отбора "
                          "(требует --filter-level 2: у alpha-beta нет ковариации)")
    ap.add_argument("--gt-first-pick", type=int, default=None,
                     help="индекс бокса трекуемой цели на ПЕРВОМ размеченном кадре — если цель "
                          "не помечена group_id (или помечена не та). Дальше цель тянется "
                          "цепочкой по ближайшему боксу с отсечкой по скачку размера")
    args = ap.parse_args()

    # Механизмы включаются на модуле-конфиге до создания TrackState: сам
    # TrackState читает cfg по ссылке, поэтому переключение обязано произойти
    # раньше. Состав пишется рядом с логом — без этого по логу не восстановить,
    # какая конфигурация его породила.
    tcfg.ENABLE_SIZE_SCORING = args.enable_a
    tcfg.ENABLE_OCCLUSION_HOLD = args.enable_b
    tcfg.ENABLE_VELOCITY_GATE = args.enable_v
    tcfg.ENABLE_VELOCITY_DIRECTION = args.enable_vdir
    if args.filter_level is not None:
        tcfg.FILTER_LEVEL = args.filter_level
    if args.enable_gate and tcfg.FILTER_LEVEL != 2:
        raise SystemExit("--enable-gate без --filter-level 2: гейту нужна ковариация Калмана")
    tcfg.ENABLE_MAHALANOBIS_GATE = args.enable_gate
    if args.enable_b and tcfg.FILTER_LEVEL == 2:
        # в Калман-ветке пауза короче: неопределённость и так растёт по Q
        tcfg.OCCLUSION_HOLD_TICKS = tcfg.KALMAN_OCCLUSION_HOLD_TICKS
    run_cfg = {
        # Провенанс: без весов и папки кадров лог не воспроизводим — по
        # прежним прогонам матрицы уже невозможно установить, какой моделью
        # они сделаны (проверено перебором models/*.pt: точного совпадения
        # детекций нет ни с одной).
        "weights": os.path.abspath(args.weights),
        "weights_sha256": _file_sha256(args.weights),
        "frames_dir": os.path.abspath(args.frames_dir),
        "imgsz": args.imgsz,
        "detect_low_conf": tcfg.DETECT_LOW_CONF,
        "mechanism_A_size": args.enable_a,
        "mechanism_B_occlusion": args.enable_b,
        "mechanism_V_velocity": args.enable_v,
        "mechanism_Vdir_direction": args.enable_vdir,
        "vdir_lambda": tcfg.VDIR_LAMBDA, "vdir_cos_veto": tcfg.VDIR_COS_VETO,
        "tick_hz": args.tick_hz,
        "size_lambda": tcfg.SIZE_LAMBDA, "size_veto_ratio": tcfg.SIZE_VETO_RATIO,
        "occlusion_proximity_frac": tcfg.OCCLUSION_PROXIMITY_FRAC,
        "occlusion_hold_ticks": tcfg.OCCLUSION_HOLD_TICKS,
        "target_select_max_dist_frac": tcfg.TARGET_SELECT_MAX_DIST_FRAC,
        "filter_level": tcfg.FILTER_LEVEL,
        "mahalanobis_gate": tcfg.ENABLE_MAHALANOBIS_GATE,
        "kalman": {
            "sigma_accel_mps2": tcfg.KALMAN_SIGMA_ACCEL_MPS2,
            "ref_distance_m": tcfg.KALMAN_REF_DISTANCE_M,
            "max_speed_mps": tcfg.KALMAN_MAX_SPEED_MPS,
            "min_distance_m": tcfg.KALMAN_MIN_DISTANCE_M,
            "r_pos_size_frac": tcfg.KALMAN_R_POS_SIZE_FRAC,
            "r_logh": tcfg.KALMAN_R_LOGH,
            "logh_radial_frac": tcfg.KALMAN_LOGH_RADIAL_FRAC,
            "gate_chi2": tcfg.KALMAN_GATE_CHI2,
        } if tcfg.FILTER_LEVEL == 2 else None,
    }
    from ultralytics import YOLO
    model = YOLO(args.weights)

    manifest = load_manifest(args.frames_dir)
    frame_w, frame_h = manifest["frame_w"], manifest["frame_h"]
    ticks = decimate_by_timestamp(manifest["frames"], args.tick_hz)
    assert ticks, f"нет кадров в {args.frames_dir}"

    try:
        gt_track = load_gt_track(args.frames_dir,
                                  manual_first_pick_index=args.gt_first_pick)
    except ValueError:
        gt_track = []
        print("GT неоднозначна (нет единого group_id) — затравка по уверенности модели")

    start_i = None
    if gt_track:
        start_i, seed_center, seed_size = bootstrap_seed_from_gt(gt_track, ticks)
        if start_i is not None:
            print(f"затравка по разметке: такт {start_i} ({ticks[start_i]['name']})")

    if start_i is None:
        seed_rec, seed_center, seed_size = bootstrap_seed(
            model, args.frames_dir, ticks, args.imgsz, conf=args.bootstrap_conf)
        if seed_rec is None:
            raise SystemExit(f"не нашли цель ни на одном такте для затравки: {args.frames_dir}")
        start_i = next(i for i, r in enumerate(ticks) if r["name"] == seed_rec["name"])
        print(f"затравка по уверенности модели: такт {start_i} ({seed_rec['name']})")

    ticks = ticks[start_i:]

    intr = ang.intrinsics_for(os.path.basename(os.path.normpath(args.frames_dir)),
                               frame_w, frame_h)
    # Пол и потолок стороны окна — угловые эквиваленты прежних пиксельных:
    # WINDOW_SIZE (столько crop() вырежет в любом случае) и короткая сторона
    # кадра (за ней вырезка перестаёт быть квадратной).
    min_window_ang = ang.px_size_to_angle(tcfg.DETECT_MIN_WINDOW_PX, intr)
    max_window_ang = ang.px_size_to_angle(min(frame_w, frame_h), intr)
    run_cfg["intrinsics"] = {"fx": intr.fx, "cx": intr.cx, "cy": intr.cy,
                              "source": intr.source, "note": intr.note}
    run_cfg["min_window_deg"] = math.degrees(min_window_ang)
    run_cfg["max_window_deg"] = math.degrees(max_window_ang)
    with open(os.path.splitext(args.log_out)[0] + ".runcfg.json", "w") as f:
        json.dump(run_cfg, f, indent=2, ensure_ascii=False)

    seed_th, seed_ph = ang.px_to_angle(seed_center[0], seed_center[1], intr)
    ts_state = TrackState(tcfg, seed_th, seed_ph, ang.px_size_to_angle(seed_size, intr),
                           min_window_ang, max_window_ang)

    writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*"mp4v"), max(args.tick_hz, 1.0),
                              (frame_w, frame_h))
    log_rows = []
    prev_ts = ticks[0]["timestamp_sec"]

    for i, rec in enumerate(ticks):
        frame = cv2.imread(os.path.join(args.frames_dir, rec["name"]))
        dt = rec["timestamp_sec"] - prev_ts if i > 0 else 0.0
        prev_ts = rec["timestamp_sec"]

        # ровно та же точка и сторона, от которых step() примет решение —
        # иначе модель смотрит в одно окно, а цель выбирается относительно
        # другого центра (см. plan_window в track_logic).
        cx_ang, cy_ang, side_ang = ts_state.plan_window(dt)
        square = angular_window_to_square(cx_ang, cy_ang, side_ang, intr)

        if i == 0:
            # затравочный такт: состояние уже проинициализировано, детекцию не гоняем повторно
            detections, chosen = [], None
            result_status, miss_count = STATUS_TRACKING, 0
        else:
            detections, _ = detect_in_window(model, frame, square, args.imgsz, tcfg.DETECT_LOW_CONF)
            ang_dets = [det_to_angles(d, intr, k) for k, d in enumerate(detections)]
            r = ts_state.step(dt, ang_dets)
            # обратно в пиксели — исходная рамка по индексу, без round-trip
            chosen = detections[r.chosen[5]] if r.chosen is not None else None
            result_status, miss_count = r.status, r.miss_count
            # Лог остаётся ПИКСЕЛЬНЫМ: его читают метрики (track_eval) и
            # визуализация (track_viz), обе работают в координатах кадра, а
            # истина размечена там же. Угловые величины пишутся рядом, с
            # суффиксом _ang, чтобы можно было проверить саму петлю.
            # положение предсказания в пикселях — честный перевод самого
            # угла, а не центр вырезки (тот смещён наружу, см. докстринг
            # angular_window_to_square); метрика сравнивает с истиной именно
            # предсказание, и смещение вырезки в неё попадать не должно
            pred_u, pred_v = ang.angle_to_px(r.predicted_cx, r.predicted_cy, intr)
            win_px = angular_window_to_square(r.predicted_cx, r.predicted_cy,
                                               r.window_side, intr)
            chosen_dist_px = None
            if chosen is not None:
                ccx, ccy = (chosen[0] + chosen[2]) / 2.0, (chosen[1] + chosen[3]) / 2.0
                chosen_dist_px = math.hypot(ccx - pred_u, ccy - pred_v)
            log_rows.append({
                "frame": rec["name"], "frame_index": rec["frame_index"],
                "timestamp_sec": rec["timestamp_sec"], "dt": dt,
                "status": r.status, "predicted_cx": pred_u, "predicted_cy": pred_v,
                "window_side": win_px.side,
                "predicted_theta": r.predicted_cx, "predicted_phi": r.predicted_cy,
                "window_side_ang": r.window_side,
                "chosen": list(chosen) if chosen is not None else None,
                "chosen_dist": chosen_dist_px, "chosen_dist_ang": r.chosen_dist,
                "miss_count": r.miss_count,
                "lost_transition": r.lost_transition, "reacquired": r.reacquired,
                "occluded": r.occluded, "n_candidates": r.n_candidates,
                "n_vetoed": r.n_vetoed,
                # сколько кандидатов оставил бы каждый способ отбора — тикет
                # требует сравнить гейт с фиксированным радиусом, а задним
                # числом по логу это не восстановить
                "n_candidates_radius": r.n_candidates_radius,
                "n_candidates_gate": r.n_candidates_gate,
                "target_size_ang": ts_state.filtered_size,
                # ВСЕ кандидаты этого такта, а не только выбранный: без них по
                # логу не видно, из чего трекер выбирал — а именно это
                # объясняет подмены (сосед оказался ближе к предсказанию).
                "detections": [[round(v, 2) for v in d] for d in detections],
            })

        vis = draw_overlay(frame.copy(), i, len(ticks), rec["timestamp_sec"], square,
                            detections, chosen, result_status, miss_count)
        writer.write(vis)

    writer.release()
    with open(args.log_out, "w") as f:
        for row in log_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    n_lost = sum(1 for r in log_rows if r["lost_transition"])
    n_reacq = sum(1 for r in log_rows if r["reacquired"])
    n_miss_ticks = sum(1 for r in log_rows if r["chosen"] is None)
    print(f"готово: {args.out}  тактов={len(ticks)}  промахов={n_miss_ticks}  "
          f"потерь={n_lost}  повторных_захватов={n_reacq}")
    gate_rows = [r for r in log_rows if r["n_candidates_gate"] is not None]
    if gate_rows:
        by_rad = sum(r["n_candidates_radius"] for r in gate_rows)
        by_gate = sum(r["n_candidates_gate"] for r in gate_rows)
        print(f"отбор кандидатов: радиусом {by_rad}, гейтом {by_gate} "
              f"(за {len(gate_rows)} тактов)")
    print(f"лог: {args.log_out}")


if __name__ == "__main__":
    main()
