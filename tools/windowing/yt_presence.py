#!/usr/bin/env python3
"""Есть ли вообще кого вести: тайловый обход ВСЕГО кадра по тактам.

Зачем. На ютубном проходе «ведение» — доля тактов, где петля приняла
кандидата. Само по себе это число неинтерпретируемо: подборка собиралась как
«видео про виндсёрфинг», а не как «видео с непрерывно видимым сёрфером», и
низкое ведение может значить и промах петли, и то, что в кадре сейчас
говорящая голова. Здесь считается знаменатель: на скольких тактах модель
находит сёрфера хоть где-нибудь в кадре.

Это НЕ разметка. Тайловый обход — та же модель с теми же ошибками, просто без
ограничения окном. Он отвечает на вопрос «мог ли трекер что-то найти», а не
«та ли это цель».

    python yt_presence.py --video V.mp4 --weights W.pt --tick-hz 3 --out P.json
"""
import argparse
import json
import os

import cv2

import config
from tile_infer_video import make_tile_grid, nms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--weights", required=True)
    ap.add_argument("--tick-hz", type=float, default=3.0)
    ap.add_argument("--tile", type=int, default=config.WINDOW_SIZE)
    ap.add_argument("--overlap", type=int, default=140)
    ap.add_argument("--conf", type=float, default=0.5593,
                     help="порог присутствия; умолчание — aligned_threshold модели")
    ap.add_argument("--low-conf", type=float, default=0.05)
    ap.add_argument("--nms-iou", type=float, default=0.5)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from ultralytics import YOLO
    model = YOLO(args.weights)

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        raise SystemExit(f"не открывается видео: {args.video}")
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    nframes = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = nframes / fps if nframes else 0.0
    grid = make_tile_grid(W, H, args.tile, args.overlap)

    rows = []
    t = 0.0
    period = 1.0 / args.tick_hz
    while t < duration:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, frame = cap.read()
        if not ok:
            break
        boxes, scores = [], []
        for (x0, y0) in grid:
            tile = frame[y0:y0 + args.tile, x0:x0 + args.tile]
            res = model.predict(tile, imgsz=args.tile, conf=args.low_conf, verbose=False)[0]
            for bb, c in zip(res.boxes.xyxy.cpu().numpy(), res.boxes.conf.cpu().numpy()):
                boxes.append([float(bb[0]) + x0, float(bb[1]) + y0,
                              float(bb[2]) + x0, float(bb[3]) + y0])
                scores.append(float(c))
        keep = nms(boxes, scores, args.nms_iou) if boxes else []
        kept = [(boxes[i], scores[i]) for i in keep]
        strong = [b for b, s in kept if s >= args.conf]
        rows.append({"timestamp_sec": round(t, 3), "n_any": len(kept),
                      "n_strong": len(strong),
                      "best_conf": round(max([s for _, s in kept], default=0.0), 4)})
        t += period

    n = len(rows) or 1
    out = {
        "video": os.path.basename(args.video),
        "frame": [W, H],
        "tick_hz": args.tick_hz,
        "conf": args.conf,
        "tiles": len(grid),
        "тактов": len(rows),
        "доля_тактов_с_сёрфером": round(sum(1 for r in rows if r["n_strong"]) / n, 4),
        "доля_тактов_хоть_что-то": round(sum(1 for r in rows if r["n_any"]) / n, 4),
        "ticks": rows,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"{os.path.basename(args.video)[:44]:46s} тактов {len(rows):5d}  "
          f"с сёрфером {out['доля_тактов_с_сёрфером']:.3f}")


if __name__ == "__main__":
    main()
