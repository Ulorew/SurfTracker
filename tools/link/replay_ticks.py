#!/usr/bin/env python3
"""Репроигрыш отдельных тактов записанного прогона: почему цель не взяли.

ЗАЧЕМ. Лог говорит, ЧТО решила петля (кандидатов столько-то, выбран такой-то,
до предсказания столько), но не говорит, КАКИЕ кандидаты были и почему
отвергнуты. Для тактов 409 и 419 прогона 260818_1059 известно лишь, что
уверенность в кадре была 0.90 и 0.92, а цель не взята.

Инструмент запускает детектор на СОХРАНЁННОМ КАДРЕ МОДЕЛИ (frames/NNNNN.jpg —
ровно тот тензор 640x640, который видела сеть на телефоне) и печатает каждого
кандидата с приговором по каждому механизму отбора порознь: радиус в прежней
форме, радиус в новой, вето по размеру, гейт.

Так вопрос «модель не нашла или алгоритм отверг» решается числом, а не глазом.

    replay_ticks.py runs/phone/260818_1059_полный 409 419 433 719 \\
        [--weights models/night_legacy_s3_best.pt]

Требует, чтобы прогон был стянут ВМЕСТЕ с папкой frames.
"""
import argparse
import csv
import math
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

LOW_CONF = 0.08          # Tracker.DETECT_LOW_CONF
WINDOW_K = 3.5           # TRACK_WINDOW_K
MIN_WINDOW = 640         # DETECT_MIN_WINDOW_PX
SELECT_FRAC = 0.30       # TARGET_SELECT_MAX_DIST_FRAC
VETO_RATIO = 1.8         # SIZE_VETO_RATIO
LAMBDA = 0.5             # SIZE_LAMBDA


def строки(run):
    with open(os.path.join(run, "log.csv")) as f:
        return list(csv.DictReader(f))


def число(row, key, default=float("nan")):
    v = row.get(key, "")
    if v in ("", "nan", None):
        return default
    try:
        return float(v)
    except ValueError:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("ticks", nargs="+", type=int)
    ap.add_argument("--weights",
                    default=os.path.join(ROOT, "models", "night_legacy_s3_best.pt"))
    ap.add_argument("--imgsz", type=int, default=640)
    args = ap.parse_args()

    run = args.run if os.path.isabs(args.run) else os.path.join(ROOT, args.run)
    frames = os.path.join(run, "frames")
    if not os.path.isdir(frames):
        sys.exit(f"нет папки {frames} — прогон стянут без кадров модели.\n"
                 f"Кадры пишутся при rec=true; стянуть: tools/link/pull_run.sh <имя>")

    rows = строки(run)
    by_i = {int(r["i"]): r for r in rows}

    from ultralytics import YOLO
    model = YOLO(args.weights)

    for tick in args.ticks:
        if tick not in by_i:
            print(f"\n=== такт {tick}: нет в логе ==="); continue
        r = by_i[tick]
        prev = by_i.get(tick - 1)
        if prev is None:
            print(f"\n=== такт {tick}: нет предыдущей строки, центр приёма не восстановить ===")
            continue

        # Центр приёма и окно — из ПРЕДЫДУЩЕЙ строки: план на этот такт
        # составлялся в конце прошлого. Сверено по логу: расхождение с
        # настоящим центром p50 = 1.8 px.
        pcx, pcy = число(prev, "winCx"), число(prev, "winCy")
        sc = число(prev, "Sc")
        filt = число(prev, "размер_фильтра")
        if not (sc > 0 and filt > 0):
            print(f"\n=== такт {tick}: в логе нет Sc или размера фильтра ==="); continue

        путь = os.path.join(frames, f"{tick:05d}.jpg")
        if not os.path.exists(путь):
            print(f"\n=== такт {tick}: нет кадра {путь} ==="); continue

        cropX = min(max(pcx - sc / 2, 0), 3840 - sc)   # как в петле
        cropY = min(max(pcy - sc / 2, 0), 2160 - sc)
        масштаб = sc / 640.0

        res = model.predict(путь, imgsz=args.imgsz, conf=LOW_CONF, verbose=False)[0]

        радиус_было = SELECT_FRAC * sc                        # от ПРИЖАТОГО окна
        радиус_стало = SELECT_FRAC * max(WINDOW_K * filt, MIN_WINDOW)

        print(f"\n=== такт {tick} ===")
        print(f"  ведомый размер {filt:.0f}, окно {sc:.0f} "
              f"(без потолка было бы {max(WINDOW_K*filt, MIN_WINDOW):.0f})")
        print(f"  радиус приёма: было {радиус_было:.0f}, стало {радиус_стало:.0f}")
        print(f"  в логе: есть_цель={r.get('есть_цель')} conf={r.get('conf')} "
              f"кандидатов={r.get('кандидатов')}")
        if len(res.boxes) == 0:
            print("  ДЕТЕКТОР НЕ НАШЁЛ НИЧЕГО — вопрос к модели, не к алгоритму")
            continue
        print(f'  {"#":>2} {"conf":>5} {"размер":>7} {"до предск.":>11} '
              f'{"отн.":>5}  {"было":>6} {"стало":>6} {"вето А":>7}')
        for k, b in enumerate(res.boxes):
            x0, y0, x1, y1 = [float(v) for v in b.xyxy[0]]
            conf = float(b.conf[0])
            cx = cropX + (x0 + x1) / 2 * масштаб
            cy = cropY + (y0 + y1) / 2 * масштаб
            размер = max(x1 - x0, y1 - y0) * масштаб
            d = math.hypot(cx - pcx, cy - pcy)
            отн = размер / filt
            вето = "ОТСЕЧЁН" if (отн > VETO_RATIO or отн < 1 / VETO_RATIO) else "прошёл"
            print(f"  {k:>2} {conf:>5.2f} {размер:>7.0f} {d:>11.0f} {отн:>5.2f}  "
                  f'{"взят" if d <= радиус_было else "мимо":>6} '
                  f'{"взят" if d <= радиус_стало else "мимо":>6} {вето:>7}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
