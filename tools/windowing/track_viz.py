#!/usr/bin/env python3
"""Пере-отрисовка прогона трекера поверх кадров: состояние трекера И истина
в одном кадре (тикет "трекинг", п.6 — видео с наложением).

Читает JSONL-лог track_run.py, модель НЕ запускает — поэтому быстро и,
главное, показывает ровно то, что видел трекер в том прогоне.

Ключевое отличие от оверлея внутри track_run.py: здесь рисуется ещё и
истина. Без неё "окно уверенно ведёт что-то" и "окно уверенно ведёт ЧУЖОГО
сёрфера" выглядят одинаково — а это главный отказ по тикету п.5.

    python track_viz.py --frames-dir DIR --log run.jsonl --out viz.mp4
"""
import argparse
import json
import math
import os

import cv2

import tracking_config as tcfg
from track_eval import gt_box_at, is_hit_radial, load_gt_track

C_GT = (0, 255, 255)        # истина — жёлтый
C_TRACK = (0, 200, 0)       # ведём и на цели — зелёный
C_MISS = (0, 165, 255)      # пропуск — оранжевый
C_LOST = (0, 0, 220)        # потеря — красный
C_SWAP = (255, 0, 255)      # взяли ЧУЖУЮ цель — пурпурный
C_CHOSEN = (255, 255, 0)    # взятая детекция, она же цель — голубой
C_CAND = (150, 150, 150)    # прочие найденные паруса — серый
C_UNKNOWN = (230, 230, 230)  # цель взята, но проверить нечем — истины на этот такт нет
C_INK = (255, 255, 255)


def status_colour(status, miss_count, on_target):
    """Цвет состояния петли. -> (цвет окна, цвет выбранной рамки, приписка).

    Три состояния истины, а не два: цель подтверждена (on_target True),
    цель опровергнута (False) и ПРОВЕРИТЬ НЕЧЕМ (None — разметка на этот такт
    кончилась или в ней дыра). Третье нельзя красить ни как подтверждение, ни
    как подмену: и то и другое — утверждение, которого мы не делали.
    """
    if status == "lost":
        win = C_LOST
    elif on_target is False:
        win = C_SWAP
    elif miss_count > 0:
        win = C_MISS
    else:
        win = C_TRACK

    if on_target is None:
        chosen, note = C_UNKNOWN, "  (истины нет)"
    elif on_target:
        chosen, note = C_CHOSEN, ""
    else:
        chosen, note = C_SWAP, "  — ЧУЖАЯ ЦЕЛЬ"
    return win, chosen, note


def draw(frame, row, gt_box, on_target):
    h, w = frame.shape[:2]
    status = row["status"]
    miss = row["miss_count"]

    col, cc, note = status_colour(status, miss, on_target)

    # окно слежения — ровно то, что видела модель
    side = row["window_side"]
    cx, cy = row["predicted_cx"], row["predicted_cy"]
    cv2.rectangle(frame, (int(cx - side / 2), int(cy - side / 2)),
                  (int(cx + side / 2), int(cy + side / 2)), col, 2)
    cv2.drawMarker(frame, (int(cx), int(cy)), col, cv2.MARKER_CROSS, 18, 2)

    # истина (интерполированная по таймстампам)
    if gt_box is not None:
        gx, gy, gw, gh = gt_box
        cv2.rectangle(frame, (int(gx - gw / 2), int(gy - gh / 2)),
                      (int(gx + gw / 2), int(gy + gh / 2)), C_GT, 2)
        cv2.putText(frame, "цель", (int(gx - gw / 2), int(gy - gh / 2) - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, C_GT, 1, cv2.LINE_AA)

    # все кандидаты такта — тускло-серым, чтобы было видно, из чего выбирали
    ch = row["chosen"]
    for d in row.get("detections") or []:
        if ch is not None and abs(d[0] - ch[0]) < 0.5 and abs(d[1] - ch[1]) < 0.5:
            continue  # выбранный рисуется отдельно, ярко
        cv2.rectangle(frame, (int(d[0]), int(d[1])), (int(d[2]), int(d[3])), C_CAND, 1)
        if len(d) > 4:
            cv2.putText(frame, f"{d[4]:.2f}", (int(d[0]), int(d[1]) - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, C_CAND, 1, cv2.LINE_AA)

    # выбранная детекция: голубая — подтверждённая цель, пурпурная — чужая,
    # белая — проверить нечем (истины на этот такт нет)
    if ch is not None:
        cv2.rectangle(frame, (int(ch[0]), int(ch[1])), (int(ch[2]), int(ch[3])), cc, 2)
        if len(ch) > 4:
            cv2.putText(frame, f"{ch[4]:.2f}", (int(ch[0]), int(ch[1]) - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, cc, 1, cv2.LINE_AA)
        if gt_box is not None:
            ccx, ccy = (ch[0] + ch[2]) / 2, (ch[1] + ch[3]) / 2
            cv2.line(frame, (int(ccx), int(ccy)), (int(gt_box[0]), int(gt_box[1])), cc, 1)

    label = {"tracking": "ВЕДУ", "lost": "ПОТЕРЯ"}[status]
    if status == "tracking" and miss > 0:
        label = f"ПРОПУСК ({miss})"
    label += note
    txt = f"t={row['timestamp_sec']:.2f}s  {label}  окно={side:.0f}px"
    cv2.rectangle(frame, (0, 0), (w, 40), (0, 0, 0), -1)
    cv2.putText(frame, txt, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, col, 2, cv2.LINE_AA)

    legend = ("жёлтый = истина | голубой = цель подтверждена | пурпур = чужая цель | "
              "белый = истины на этот такт нет | серый = прочие найденные паруса")
    cv2.rectangle(frame, (0, h - 30), (w, h), (0, 0, 0), -1)
    cv2.putText(frame, legend, (10, h - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.55, C_INK, 1, cv2.LINE_AA)
    return frame


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames-dir", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=float, default=3.0)
    ap.add_argument("--gt-first-pick", type=int, default=None,
                     help="тот же флаг, что у track_run/track_eval — ОБЯЗАН совпадать с тем, "
                          "с которым делался прогон, иначе на видео трекер ведёт одну цель, "
                          "а истина нарисована на другой")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.log) if l.strip()]
    try:
        gt = load_gt_track(args.frames_dir, manual_first_pick_index=args.gt_first_pick)
    except ValueError:
        gt = []

    first = cv2.imread(os.path.join(args.frames_dir, rows[0]["frame"]))
    h, w = first.shape[:2]
    writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*"mp4v"), args.fps, (w, h))

    n_on, n_graded = 0, 0
    for row in rows:
        frame = cv2.imread(os.path.join(args.frames_dir, row["frame"]))
        gt_box = gt_box_at(gt, row["timestamp_sec"]) if gt else None
        on_target = None
        if gt_box is not None and row["chosen"] is not None:
            ccx = (row["chosen"][0] + row["chosen"][2]) / 2
            ccy = (row["chosen"][1] + row["chosen"][3]) / 2
            on_target = is_hit_radial(ccx, ccy, gt_box)
            n_graded += 1
            n_on += on_target
        writer.write(draw(frame, row, gt_box, on_target))
    writer.release()
    frac = f"{n_on / n_graded:.2f}" if n_graded else "—"
    print(f"{os.path.basename(args.out)}: тактов={len(rows)} на цели={frac}")


if __name__ == "__main__":
    main()
