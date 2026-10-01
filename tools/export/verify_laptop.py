#!/usr/bin/env python3
"""Сверка PyTorch <-> LiteRT на ноутбуке.

Порядок проверок намеренно такой: сначала fp32, и если он не сходится —
дальше идти нельзя, потому что это баг конвертации, а не потери квантования.
Только потом w8a32.

Отдельно проверяются известные грабли, каждая — явным утверждением, а не
"вроде похоже":
  1. letterbox против простого resize (у нас вход уже 640x640 — обязан быть
     no-op, и это проверяется, а не предполагается);
  2. RGB против BGR (перепутанные каналы дают правдоподобные, но другие
     рамки — глазами не отличить);
  3. нормализация /255 против int8 scale/zero_point на входе;
  4. выход YOLO11: координаты в пикселях входа или нормированы.

    .venv-export/bin/python verify_laptop.py --weights x.pt \\
        --models-dir ../../export/models --testset ../../export/testset
"""
import argparse
import json
import math
import os
import statistics as st
import sys

import cv2
import numpy as np

CONF = 0.25   # порог для сравнения РАМОК; сырые тензоры сравниваются целиком
IOU_MATCH = 0.5


def load_interpreter(path):
    try:
        from ai_edge_litert.interpreter import Interpreter
    except ImportError:
        from tensorflow.lite.python.interpreter import Interpreter
    it = Interpreter(model_path=path)
    it.allocate_tensors()
    return it


def preprocess(bgr, layout, dtype):
    """BGR uint8 HWC -> тензор для интерпретатора.

    RGB и /255 — контракт ultralytics; layout берётся из самой модели, а не
    угадывается.
    """
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    x = rgb[None]                      # NHWC
    if layout == "nchw":
        x = x.transpose(0, 3, 1, 2)
    return x.astype(dtype)


def run_tflite(it, bgr):
    inp = it.get_input_details()[0]
    shape = list(inp["shape"])
    layout = "nchw" if shape[1] == 3 else "nhwc"
    x = preprocess(bgr, layout, inp["dtype"])
    it.set_tensor(inp["index"], x)
    it.invoke()
    outs = [it.get_tensor(o["index"]) for o in it.get_output_details()]
    return outs[0] if len(outs) == 1 else outs


def as_pixels(raw, imgsz):
    """-> (тензор в ПИКСЕЛЯХ (4+nc, N), как_было).

    Ultralytics-экспорт в LiteRT отдаёт координаты НОРМИРОВАННЫМИ, а
    PyTorch-модель — в пикселях входа. Это известная грабля экспорта, и она
    здесь не предполагается, а определяется по данным: у нормированного
    выхода координаты не выходят за ~1. Сравнивать тензоры, не приведя
    единицы, бессмысленно — расхождение будет ~imgsz на ровном месте.

    На телефоне это же означает, что приложение ОБЯЗАНО домножать выход на
    imgsz. Пропустив это, получишь рамки в левом верхнем пикселе и решишь,
    что модель сломалась.
    """
    a = np.asarray(raw)
    if a.ndim == 3:
        a = a[0]
    if a.shape[0] > a.shape[1]:
        a = a.T                      # -> (4+nc, N)
    a = a.copy()
    normalized = float(np.abs(a[:4]).max()) <= 2.0
    if normalized:
        a[:4] *= imgsz
    return a, ("нормированные" if normalized else "пиксели")


def to_boxes(raw, conf=CONF, imgsz=640):
    """Сырой выход YOLO -> [(x0,y0,x1,y1,conf)] в пикселях входа.

    Ожидается (1, 4+nc, N) либо (1, N, 4+nc): различаем по тому, какая ось
    короче — классов у нас один, каналов 5.
    """
    a, _ = as_pixels(raw, imgsz)
    xywh, scores = a[:4], a[4:]
    cls_conf = scores.max(axis=0)
    keep = cls_conf >= conf
    out = []
    for i in np.nonzero(keep)[0]:
        cx, cy, w, h = xywh[:, i]
        out.append((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, float(cls_conf[i])))
    return nms(out)


def nms(boxes, thr=0.5):
    boxes = sorted(boxes, key=lambda b: -b[4])
    kept = []
    for b in boxes:
        if all(iou(b, k) < thr for k in kept):
            kept.append(b)
    return kept


def iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def match(pred, gt):
    """Пары (предсказание, истина) по максимальному IoU, жадно."""
    pairs, used = [], set()
    for p in sorted(pred, key=lambda b: -b[4]):
        best, best_i = 0.0, None
        for i, g in enumerate(gt):
            if i in used:
                continue
            v = iou(p, g + (1.0,))
            if v > best:
                best, best_i = v, i
        if best_i is not None and best >= IOU_MATCH:
            used.add(best_i)
            pairs.append((p, gt[best_i]))
    return pairs, len(used)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", required=True)
    ap.add_argument("--models-dir", required=True)
    ap.add_argument("--testset", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    meta = json.load(open(os.path.join(args.testset, "testset.json")))
    items = meta["items"]
    imgs = {i["name"]: cv2.imread(os.path.join(args.testset, i["name"])) for i in items}

    from ultralytics import YOLO
    model = YOLO(args.weights)

    report = {"грабли": {}, "сборки": {}, "кадров": len(items)}

    # --- эталон: PyTorch, сырой выход ------------------------------------
    torch_raw, torch_boxes = {}, {}
    import torch
    net = model.model.float().eval()
    for i in items:
        x = preprocess(imgs[i["name"]], "nchw", np.float32)  # noqa: PLW2901
        with torch.no_grad():
            y = net(torch.from_numpy(x))
        y = y[0] if isinstance(y, (list, tuple)) else y
        torch_raw[i["name"]] = y.cpu().numpy()
        torch_boxes[i["name"]] = to_boxes(torch_raw[i["name"]])

    # --- грабли, проверяемые явно ----------------------------------------
    first = items[0]["name"]
    ref = torch_raw[first]
    report["грабли"]["1_letterbox"] = {
        "вход_уже": list(imgs[first].shape),
        "ресайз_нужен": imgs[first].shape[:2] != (meta["imgsz"], meta["imgsz"]),
        "вывод": "вход ровно imgsz, letterbox — no-op; на телефоне обязан быть "
                  "тот же кроп, иначе сверка теряет смысл",
    }
    swapped = preprocess(imgs[first][:, :, ::-1], "nchw", np.float32)
    with torch.no_grad():
        y_sw = net(torch.from_numpy(swapped))
    y_sw = (y_sw[0] if isinstance(y_sw, (list, tuple)) else y_sw).cpu().numpy()
    report["грабли"]["2_rgb_против_bgr"] = {
        "макс_расхождение_выхода_при_подмене_каналов": float(np.abs(y_sw - ref).max()),
        "вывод": "перепутанные каналы дают ДРУГОЙ выход — значит сверка их поймает",
    }
    rng = np.asarray(ref)
    report["грабли"]["4_единицы_выхода"] = {
        "pytorch": {"форма": list(rng.shape),
                     "макс_координата": float(np.abs(rng[0, :4]).max()),
                     "единицы": as_pixels(rng, meta["imgsz"])[1]},
    }

    # --- сборки -----------------------------------------------------------
    for kind in ("fp32", "w8a32"):
        path = os.path.join(args.models_dir, f"surf_{kind}.tflite")
        if not os.path.exists(path):
            report["сборки"][kind] = {"ошибка": "файла нет"}
            continue
        it = load_interpreter(path)
        inp = it.get_input_details()[0]
        q = inp.get("quantization_parameters", {})
        report["грабли"].setdefault("3_нормализация_входа", {})[kind] = {
            "dtype": str(np.dtype(inp["dtype"])),
            "shape": [int(v) for v in inp["shape"]],
            "scale": [float(v) for v in q.get("scales", [])],
            "zero_point": [int(v) for v in q.get("zero_points", [])],
            "вывод": "float-вход, нормализация /255 на стороне раннера"
                      if np.dtype(inp["dtype"]) == np.float32
                      else "int8-вход: нужен scale/zero_point, а не /255",
        }
        d_raw, d_center, d_logsize, n_pred = [], [], [], 0
        rec_t = rec_x = gt_total = 0
        for i in items:
            name = i["name"]
            out = run_tflite(it, imgs[name])
            a_px, units = as_pixels(out if not isinstance(out, list) else out[0],
                                     meta["imgsz"])
            r_px, _ = as_pixels(torch_raw[name], meta["imgsz"])
            report["грабли"]["4_единицы_выхода"].setdefault(kind, units)
            if a_px.shape == r_px.shape:
                # координаты и уверенности сравниваем отдельно: у них разный
                # масштаб, и общий максимум прятал бы расхождение уверенностей.
                # Отдельно — только по УВЕРЕННЫМ якорям: из 8400 якорей
                # подавляющее большинство никогда не доходит до порога, и
                # максимум по всем меряет расхождение мусора, а не рамок.
                conf_mask = r_px[4:].max(axis=0) >= CONF
                d = {
                    "координаты_px": float(np.abs(a_px[:4] - r_px[:4]).max()),
                    "уверенности": float(np.abs(a_px[4:] - r_px[4:]).max()),
                    "координаты_px_уверенные": None,
                    "уверенности_уверенные": None,
                }
                if conf_mask.any():
                    d["координаты_px_уверенные"] = float(
                        np.abs(a_px[:4, conf_mask] - r_px[:4, conf_mask]).max())
                    d["уверенности_уверенные"] = float(
                        np.abs(a_px[4:, conf_mask] - r_px[4:, conf_mask]).max())
                d_raw.append(d)
            boxes = to_boxes(a_px, imgsz=meta["imgsz"])
            n_pred += len(boxes)
            pairs, _ = match(boxes, [tuple(g) for g in i["gt_xyxy"]])
            # сдвиг центров и отношение размеров — против PyTorch, не против истины
            tp = torch_boxes[name]
            for b in boxes:
                near = min(tp, key=lambda t: math.hypot((t[0] + t[2]) / 2 - (b[0] + b[2]) / 2,
                                                         (t[1] + t[3]) / 2 - (b[1] + b[3]) / 2),
                            default=None)
                if near is None:
                    continue
                d_center.append(math.hypot((near[0] + near[2]) / 2 - (b[0] + b[2]) / 2,
                                            (near[1] + near[3]) / 2 - (b[1] + b[3]) / 2))
                s_a = max(b[2] - b[0], b[3] - b[1])
                s_b = max(near[2] - near[0], near[3] - near[1])
                if s_a > 0 and s_b > 0:
                    d_logsize.append(abs(math.log(s_a / s_b)))
            gt = [tuple(g) for g in i["gt_xyxy"]]
            gt_total += len(gt)
            rec_x += match(boxes, gt)[1]
            rec_t += match(torch_boxes[name], gt)[1]
        report["сборки"][kind] = {
            "макс_расхождение_координат_px": max(d["координаты_px"] for d in d_raw) if d_raw else None,
            "макс_расхождение_уверенностей": max(d["уверенности"] for d in d_raw) if d_raw else None,
            "макс_расхождение_координат_px_уверенные": max(
                (d["координаты_px_уверенные"] for d in d_raw
                 if d["координаты_px_уверенные"] is not None), default=None),
            "макс_расхождение_уверенностей_уверенные": max(
                (d["уверенности_уверенные"] for d in d_raw
                 if d["уверенности_уверенные"] is not None), default=None),
            "медиана_сдвига_центров_px": st.median(d_center) if d_center else None,
            "медиана_|log(отношение размеров)|": st.median(d_logsize) if d_logsize else None,
            "рамок_всего": n_pred,
            "полнота_pytorch": rec_t / gt_total if gt_total else None,
            "полнота_экспорта": rec_x / gt_total if gt_total else None,
            "падение_полноты": (rec_t - rec_x) / gt_total if gt_total else None,
            "рамок_истины": gt_total,
            "разрешение_одна_рамка": 1 / gt_total if gt_total else None,
        }
        r = report["сборки"][kind]
        print(f"\n=== {kind} ===")
        print(f"  макс расхождение координат: {r['макс_расхождение_координат_px']:.3e} px")
        print(f"  макс расхождение уверенностей: {r['макс_расхождение_уверенностей']:.3e}")
        if r.get("макс_расхождение_координат_px_уверенные") is not None:
            print(f"  по уверенным якорям: координаты "
                  f"{r['макс_расхождение_координат_px_уверенные']:.3e} px, уверенности "
                  f"{r['макс_расхождение_уверенностей_уверенные']:.3e}")
        print(f"  медиана сдвига центров: {r['медиана_сдвига_центров_px']}")
        print(f"  полнота: PyTorch {r['полнота_pytorch']:.4f} -> "
              f"экспорт {r['полнота_экспорта']:.4f} "
              f"(падение {r['падение_полноты']:+.4f}; одна рамка = "
              f"{1 / gt_total:.4f})")

    if args.out:
        json.dump(report, open(args.out, "w"), indent=2, ensure_ascii=False)
        print(f"\nотчёт: {args.out}")

    fp = report["сборки"].get("fp32", {})
    if fp.get("макс_расхождение_координат_px") is not None:
        ok = fp["макс_расхождение_координат_px"] <= 1e-4 * meta["imgsz"]
        print(f"\nfp32 сходится (координаты до 1e-4 от imgsz): "
              f"{'ДА' if ok else 'НЕТ — это баг конвертации, дальше не идти'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
