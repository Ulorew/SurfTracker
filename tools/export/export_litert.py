#!/usr/bin/env python3
"""Экспорт модели в LiteRT (.tflite) — тикет "экспорт на телефон", п.1.

Новый официальный путь ultralytics: format="litert" (litert_torch, torch ->
LiteRT напрямую). Старый путь через onnx2tf/saved_model объявлен deprecated и
здесь не используется.

Две сборки:
  w8a32 — int8-веса, float32-активации (dynamic/weight-only INT8). Выбран
          давно: ~0.1 mAP потери против ~6.6 у статического int8, и это
          критично именно для мелких целей, которых у нас большинство.
  fp32  — эталон сверки и запасной вариант.

Манифест пишется рядом: sha256 исходных весов и обеих сборок, версии
ultralytics/litert, аргументы экспорта. Без него через месяц не установить,
какой файл из какой модели получен, — эта же грабля уже стоила нам прогонов
матрицы трекинга с неизвестными весами.

    .venv-export/bin/python export_litert.py --weights ../../models/x.pt \\
        --out-dir ../../export/models
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pkg_versions(names):
    out = {}
    for n in names:
        try:
            mod = __import__(n.replace("-", "_"))
            out[n] = getattr(mod, "__version__", "?")
        except Exception:
            try:
                r = subprocess.run([sys.executable, "-m", "pip", "show", n],
                                    capture_output=True, text=True)
                line = [x for x in r.stdout.splitlines() if x.startswith("Version:")]
                out[n] = line[0].split(":", 1)[1].strip() if line else "нет"
            except Exception:
                out[n] = "нет"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--only", nargs="+", default=["fp32", "w8a32"])
    args = ap.parse_args()

    from ultralytics import YOLO

    os.makedirs(args.out_dir, exist_ok=True)
    src_hash = sha256(args.weights)
    manifest = {
        "источник": {
            "файл": os.path.abspath(args.weights),
            "sha256": src_hash,
            "рецепт": "AdamW lr0=0.0003, size-bins-floor=20, онлайн-кроп, yolo11n",
            "оговорка": "лучший сид из восьми; выбор максимума по восьми — отбор "
                         "на шуме, а не измеренное превосходство рецепта",
        },
        "вход": {"imgsz": args.imgsz, "batch": args.batch},
        "окружение": {
            "python": platform.python_version(),
            "платформа": platform.platform(),
            **pkg_versions(["ultralytics", "torch", "litert-torch", "ai-edge-litert",
                            "ai-edge-quantizer"]),
        },
        "сборки": {},
    }

    for kind in args.only:
        quantize = None if kind == "fp32" else kind
        print(f"\n=== экспорт {kind} ===", flush=True)
        t0 = time.time()
        model = YOLO(args.weights)
        kw = dict(format="litert", imgsz=args.imgsz, batch=args.batch)
        if quantize:
            kw["quantize"] = quantize
        path = model.export(**kw)
        dst = os.path.join(args.out_dir, f"surf_{kind}.tflite")
        shutil.move(str(path), dst)
        manifest["сборки"][kind] = {
            "файл": os.path.abspath(dst),
            "sha256": sha256(dst),
            "байт": os.path.getsize(dst),
            "аргументы": {k: (str(v) if not isinstance(v, (int, float, str, bool, type(None))) else v)
                           for k, v in kw.items()},
            "секунд": round(time.time() - t0, 1),
        }
        print(f"{kind}: {dst}  {os.path.getsize(dst) / 1e6:.2f} МБ  "
              f"{time.time() - t0:.0f} c", flush=True)

    mpath = os.path.join(args.out_dir, "export_manifest.json")
    with open(mpath, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"\nманифест: {mpath}")
    print(f"  sha256 исходных весов: {src_hash}")
    for k, v in manifest["сборки"].items():
        print(f"  {k}: {v['байт'] / 1e6:.2f} МБ, sha256 {v['sha256'][:16]}...")


if __name__ == "__main__":
    main()
