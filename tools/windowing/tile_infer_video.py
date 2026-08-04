#!/usr/bin/env python3
"""Поклеточный инференс на видео (визуализация модели-бейзлайна, кроп на лету).

Кадр режется на плитки WINDOW_SIZE x WINDOW_SIZE с перекрытием, модель
гоняется на каждой плитке независимо (тот же режим входа, что при обучении —
без ресайза, 1:1 пиксели), предсказания переводятся в координаты кадра и
объединяются через NMS (дубли в зоне перекрытия соседних плиток).

    python tile_infer_video.py --frames-dir DIR --weights best.pt --out out.mp4 \
        [--tile 640] [--overlap 140] [--conf 0.5593] [--nms-iou 0.5] [--fps 4]
"""

import argparse
import os

import cv2
import numpy as np


def make_tile_grid(frame_w, frame_h, tile, overlap):
    """Список (x0,y0) левых верхних углов плиток tile x tile, покрывающих
    весь кадр с перекрытием >= overlap; последняя плитка в каждой строке/
    колонке прижата к правому/нижнему краю (не выходит за кадр).

    Кадр МЕНЬШЕ tile по какой-то оси — last_x/last_y уже 0 (max(...,0)); без
    этого клампа сравнение "xs[-1] != frame_w - tile" сверяло бы с
    отрицательным числом и всегда было бы True, задваивая (0,0)."""
    stride = tile - overlap
    last_x = max(frame_w - tile, 0)
    xs = list(range(0, last_x + 1, stride))
    if xs[-1] != last_x:
        xs.append(last_x)
    last_y = max(frame_h - tile, 0)
    ys = list(range(0, last_y + 1, stride))
    if ys[-1] != last_y:
        ys.append(last_y)
    return [(x, y) for y in ys for x in xs]


def nms(boxes, scores, iou_thr):
    """boxes: Nx4 (x0,y0,x1,y1). -> индексы, прошедшие NMS."""
    if len(boxes) == 0:
        return []
    boxes = np.array(boxes, dtype=np.float32)
    scores = np.array(scores, dtype=np.float32)
    x0, y0, x1, y1 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x1 - x0) * (y1 - y0)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        rest = order[1:]
        xx0 = np.maximum(x0[i], x0[rest])
        yy0 = np.maximum(y0[i], y0[rest])
        xx1 = np.minimum(x1[i], x1[rest])
        yy1 = np.minimum(y1[i], y1[rest])
        inter = np.maximum(0, xx1 - xx0) * np.maximum(0, yy1 - yy0)
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_thr]
    return keep


def infer_frame(model, frame, tile, overlap, low_conf, imgsz):
    h, w = frame.shape[:2]
    tiles = make_tile_grid(w, h, tile, overlap)
    all_boxes, all_scores = [], []
    for (tx, ty) in tiles:
        crop = frame[ty:ty + tile, tx:tx + tile]
        res = model.predict(crop, imgsz=imgsz, conf=low_conf, verbose=False)[0]
        for bb, c in zip(res.boxes.xyxy.cpu().numpy(), res.boxes.conf.cpu().numpy()):
            x0, y0, x1, y1 = bb
            all_boxes.append((x0 + tx, y0 + ty, x1 + tx, y1 + ty))
            all_scores.append(float(c))
    return all_boxes, all_scores, len(tiles)


def draw_boxes(frame, boxes, scores, conf_thr):
    for (x0, y0, x1, y1), c in zip(boxes, scores):
        if c < conf_thr:
            continue
        p0, p1 = (int(x0), int(y0)), (int(x1), int(y1))
        cv2.rectangle(frame, p0, p1, (0, 220, 0), 2)
        label = f"{c:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(frame, (p0[0], p0[1] - th - 6), (p0[0] + tw + 4, p0[1]), (0, 220, 0), -1)
        cv2.putText(frame, label, (p0[0] + 2, p0[1] - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
    return frame


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames-dir", required=True, help="папка с извлечёнными кадрами (f*.jpg, по порядку)")
    ap.add_argument("--weights", required=True)
    ap.add_argument("--out", required=True, help="итоговое видео (mp4)")
    ap.add_argument("--tile", type=int, default=640)
    ap.add_argument("--overlap", type=int, default=140)
    ap.add_argument("--conf", type=float, default=0.5593, help="порог отрисовки (по умолчанию aligned_threshold этой модели)")
    ap.add_argument("--low-conf", type=float, default=0.05, help="порог на самом инференсе, до NMS/фильтра отрисовки")
    ap.add_argument("--nms-iou", type=float, default=0.5)
    ap.add_argument("--fps", type=float, default=4)
    args = ap.parse_args()

    from ultralytics import YOLO
    model = YOLO(args.weights)

    names = sorted(f for f in os.listdir(args.frames_dir) if f.endswith(".jpg"))
    assert names, f"нет кадров в {args.frames_dir}"
    first = cv2.imread(os.path.join(args.frames_dir, names[0]))
    h, w = first.shape[:2]

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(args.out, fourcc, args.fps, (w, h))

    total_dets = 0
    n_tiles_per_frame = None
    for i, name in enumerate(names):
        frame = cv2.imread(os.path.join(args.frames_dir, name))
        boxes, scores, n_tiles = infer_frame(model, frame, args.tile, args.overlap, args.low_conf, args.tile)
        n_tiles_per_frame = n_tiles
        keep = nms(boxes, scores, args.nms_iou)
        boxes = [boxes[k] for k in keep]
        scores = [scores[k] for k in keep]
        shown = sum(1 for c in scores if c >= args.conf)
        total_dets += shown
        draw_boxes(frame, boxes, scores, args.conf)
        cv2.putText(frame, f"frame {i+1}/{len(names)}  t={i/args.fps:.1f}s  dets={shown}",
                    (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 220, 0), 2, cv2.LINE_AA)
        writer.write(frame)
        if (i + 1) % 20 == 0 or i == len(names) - 1:
            print(f"{i+1}/{len(names)} кадров, всего показано детекций: {total_dets}")

    writer.release()
    print(f"готово: {args.out}  ({len(names)} кадров, {n_tiles_per_frame} плиток/кадр, tile={args.tile} overlap={args.overlap})")


if __name__ == "__main__":
    main()
