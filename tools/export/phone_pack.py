#!/usr/bin/env python3
"""Пакет для телефона + сверка его выходов (тикет, п.3).

Собирает всё, что нужно унести на устройство, и сохраняет ЭТАЛОН — сырые
выходы w8a32, посчитанные на ноутбуке тем же интерпретатором. Сравнение потом
идёт против них, а не против PyTorch: интерпретатор один и тот же, поэтому
допуск жёсткий (1e-5), и любое расхождение сверх него — это препроцессинг
приложения, а не арифметика модели.

    phone_pack.py build   --out ../../export/phone_pack
    phone_pack.py compare --pack ../../export/phone_pack --phone phone_out.json
"""
import argparse
import json
import os
import shutil
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_laptop import CONF, as_pixels, load_interpreter, run_tflite, to_boxes  # noqa: E402

README = """# Пакет сверки на телефоне

Redmi Note 14 Pro 5G (Dimensity 7300-Ultra). Цель: убедиться, что на телефоне
модель считает ТО ЖЕ, что на ноутбуке, и только потом мерить скорость.

## Контракт входа и выхода (проверен на ноутбуке, не угадан)

- вход: `[1, 3, 640, 640]` float32, **NCHW** (не NHWC);
- порядок каналов **RGB** (cv2 читает BGR — конвертировать обязательно);
- нормализация: `pixel / 255.0`. Вход float32, никаких scale/zero_point:
  квантование w8a32 касается ВЕСОВ, а не входа;
- выход: `[1, 5, 8400]`, строки `cx, cy, w, h, conf`;
- **координаты выхода НОРМИРОВАНЫ** — умножать на 640. Это главная грабля:
  без умножения все рамки схлопнутся в левый верхний угол, и выглядеть это
  будет как сломанная модель;
- letterbox не нужен: кадры уже 640x640. Если приложение всё же ресайзит —
  сверка сломается, и это правильно.

## Что запустить

1. Скопировать `frames/` и `surf_w8a32.tflite` на устройство.
2. Прогнать каждый кадр через интерпретатор с XNNPACK, сохранить СЫРОЙ выход.
3. Сложить в json: `{"<имя кадра>": [[cx,cy,w,h,conf], ...]}` — только якоря
   с conf >= 0.25, координаты в том виде, в каком их отдала модель.
4. Забрать json на ноутбук и:

       phone_pack.py compare --pack . --phone phone_out.json

Допуск: 1e-5 против эталона (интерпретатор тот же). Больше — искать в
препроцессинге приложения, а не в модели.

## Скорость (после того как сверка сошлась)

benchmark_model **как APK, не adb-бинарь**: планировщик душит фоновые
процессы, и разница видна именно на многопоточном CPU.

- XNNPACK, потоки 1 / 2 / 4, закрепление на больших ядрах (4x A78);
- не меньше 200 прогонов после прогрева, брать p50 и p95;
- GPU-делегат: таймбокс полдня, не завёлся — закрыли;
- NNAPI/NPU не трогать.

Бюджет: такт 3 Гц = 333 мс. Инференс <=100 мс p95 — комфорт, <=150 — приемлемо.
"""


def build(args):
    os.makedirs(args.out, exist_ok=True)
    frames_dir = os.path.join(args.out, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    meta = json.load(open(os.path.join(args.testset, "testset.json")))
    for i in meta["items"]:
        shutil.copy2(os.path.join(args.testset, i["name"]),
                     os.path.join(frames_dir, i["name"]))
    shutil.copy2(os.path.join(args.models_dir, "surf_w8a32.tflite"), args.out)
    shutil.copy2(os.path.join(args.models_dir, "export_manifest.json"), args.out)

    it = load_interpreter(os.path.join(args.models_dir, "surf_w8a32.tflite"))
    ref = {}
    for i in meta["items"]:
        bgr = cv2.imread(os.path.join(args.testset, i["name"]))
        out = run_tflite(it, bgr)
        a = np.asarray(out if not isinstance(out, list) else out[0])
        b = a[0] if a.ndim == 3 else a
        if b.shape[0] > b.shape[1]:
            b = b.T
        keep = b[4:].max(axis=0) >= CONF
        ref[i["name"]] = b[:, keep].T.astype(np.float32)   # (M, 5) КАК ОТДАЛА МОДЕЛЬ
    np.savez_compressed(os.path.join(args.out, "laptop_w8a32_ref.npz"), **ref)

    with open(os.path.join(args.out, "README.md"), "w") as f:
        f.write(README)
    json.dump(meta, open(os.path.join(args.out, "testset.json"), "w"),
              indent=2, ensure_ascii=False)
    n = sum(v.shape[0] for v in ref.values())
    print(f"пакет: {args.out}")
    print(f"  кадров {len(ref)}, эталонных якорей (conf>={CONF}) {n}")
    print(f"  модель w8a32 + манифест + README с контрактом входа/выхода")


def compare(args):
    ref = dict(np.load(os.path.join(args.pack, "laptop_w8a32_ref.npz")))
    phone = json.load(open(args.phone))
    meta = json.load(open(os.path.join(args.pack, "testset.json")))
    imgsz = meta["imgsz"]

    worst, rows, missing = 0.0, [], []
    for name, r in sorted(ref.items()):
        p = phone.get(name)
        if p is None:
            missing.append(name)
            continue
        p = np.asarray(p, dtype=np.float32)
        if p.shape != r.shape:
            rows.append((name, None, f"разное число якорей: телефон {p.shape}, ноутбук {r.shape}"))
            continue
        # сортируем по уверенности с обеих сторон: порядок якорей — деталь
        # реализации раннера, а не свойство модели
        p = p[np.argsort(-p[:, 4])]
        r = r[np.argsort(-r[:, 4])]
        d = float(np.abs(p - r).max())
        worst = max(worst, d)
        rows.append((name, d, "ок" if d <= args.tol else "РАСХОЖДЕНИЕ"))

    print(f"кадров сверено: {len(rows)}, пропущено телефоном: {len(missing)}")
    bad = [r for r in rows if r[1] is None or r[1] > args.tol]
    for name, d, note in rows[:5]:
        print(f"  {name[:40]:42s} {('%.3e' % d) if d is not None else '—':>10s}  {note}")
    print(f"худшее расхождение: {worst:.3e} (допуск {args.tol:.0e})")
    if missing:
        print(f"ПРОПУЩЕНЫ: {missing}")
    print("ВЕРДИКТ:", "сходится" if not bad and not missing
          else "НЕ сходится — искать в препроцессинге приложения "
               "(NCHW? RGB? /255? нормировка выхода?)")
    return 0 if (not bad and not missing) else 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", required=True)
    b.add_argument("--testset", default="../../export/testset")
    b.add_argument("--models-dir", default="../../export/models")
    b.set_defaults(func=build)
    c = sub.add_parser("compare")
    c.add_argument("--pack", required=True)
    c.add_argument("--phone", required=True)
    c.add_argument("--tol", type=float, default=1e-5)
    c.set_defaults(func=compare)
    args = ap.parse_args()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
