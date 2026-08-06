#!/usr/bin/env python3
"""Полнота экспорта ШТАТНОЙ метрикой проекта (eval_track), а не упрощённой.

Зачем отдельный файл. В verify_laptop.py полнота считается при ФИКСИРОВАННОМ
пороге conf=0.25. Тикет требует "полнота той же оценкой", а штатная оценка
проекта — eval_track.completeness_aligned — устроена иначе:

  - порог подбирается ПОД КАЖДУЮ МОДЕЛЬ так, чтобы ложных срабатываний было
    0.05 на окно (config.EVAL_TARGET_FP_PER_WINDOW);
  - на каждый бокс 8 независимых окон со случайным джиттером;
  - есть ignore-зона и разбиение по корзинам размеров.

Это не формальность. w8a32 сдвигает уверенности до 0.209, а при фиксированном
пороге сдвиг КАЛИБРОВКИ сам по себе двигает полноту — и результат смешивает
"стала хуже находить" со "стала иначе оценивать уверенность". Выравнивание по
ложным срабатываниям существует ровно затем, чтобы это разделить.

eval_track.py не трогается вовсе: подменяется только ultralytics.YOLO, чтобы
на .tflite он отдавал интерпретатор с тем же интерфейсом predict().

    .venv-export/bin/python verify_aligned.py \\
        --weights ../../models/night_legacy_s3_best.pt \\
        --models-dir ../../export/models \\
        --images ../../Datasets/dataset_v6/images/val \\
        --labels ../../Datasets/dataset_v6/labels/val
"""
import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "windowing"))
sys.path.insert(0, HERE)

from verify_laptop import as_pixels, load_interpreter, preprocess  # noqa: E402


class _Tensor:
    """Утиный двойник torch-тензора: eval_track зовёт .cpu().numpy()."""

    def __init__(self, a):
        self._a = np.asarray(a)

    def cpu(self):
        return self

    def numpy(self):
        return self._a

    def __len__(self):
        return len(self._a)


class _Boxes:
    def __init__(self, xyxy, conf):
        self.xyxy = _Tensor(xyxy)
        self.conf = _Tensor(conf)

    def __len__(self):
        return len(self.xyxy)


class _Result:
    def __init__(self, xyxy, conf):
        self.boxes = _Boxes(xyxy, conf)


class TFLiteModel:
    """Интерфейс ultralytics-модели поверх LiteRT-интерпретатора.

    NMS здесь СВОЙ, потому что экспорт отдаёт сырые якоря. Порог IoU взят тот
    же, что у ultralytics по умолчанию (0.7), иначе сравнение PyTorch с
    экспортом мерило бы разницу постобработки, а не модели.
    """

    def __init__(self, path, iou=0.7, imgsz=640):
        self.it = load_interpreter(path)
        self.iou = iou
        self.imgsz = imgsz
        inp = self.it.get_input_details()[0]
        self.layout = "nchw" if list(inp["shape"])[1] == 3 else "nhwc"
        self.dtype = inp["dtype"]
        self.index = inp["index"]

    def predict(self, img, imgsz=None, conf=0.25, verbose=False, **kw):
        x = preprocess(img, self.layout, self.dtype)
        self.it.set_tensor(self.index, x)
        self.it.invoke()
        outs = [self.it.get_tensor(o["index"]) for o in self.it.get_output_details()]
        a, _ = as_pixels(outs[0], self.imgsz)      # (4+nc, N) в пикселях входа
        scores = a[4:].max(axis=0)
        keep = scores >= conf
        xywh, sc = a[:4, keep], scores[keep]
        boxes = np.stack([xywh[0] - xywh[2] / 2, xywh[1] - xywh[3] / 2,
                          xywh[0] + xywh[2] / 2, xywh[1] + xywh[3] / 2], axis=1)
        idx = _nms(boxes, sc, self.iou)
        return [_Result(boxes[idx], sc[idx])]


def _nms(boxes, scores, thr):
    order = np.argsort(-scores)
    kept = []
    while len(order):
        i = order[0]
        kept.append(i)
        if len(order) == 1:
            break
        rest = order[1:]
        xx0 = np.maximum(boxes[i, 0], boxes[rest, 0])
        yy0 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx1 = np.minimum(boxes[i, 2], boxes[rest, 2])
        yy1 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.clip(xx1 - xx0, 0, None) * np.clip(yy1 - yy0, 0, None)
        area_i = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        area_r = (boxes[rest, 2] - boxes[rest, 0]) * (boxes[rest, 3] - boxes[rest, 1])
        iou = inter / np.maximum(area_i + area_r - inter, 1e-9)
        order = rest[iou < thr]
    return np.array(kept, dtype=int)


def frac(comp):
    f = sum(v["found"] for v in comp.values())
    t = sum(v["total"] for v in comp.values())
    return f / t if t else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", required=True)
    ap.add_argument("--models-dir", required=True)
    ap.add_argument("--images", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--realizations", type=int, default=None)
    ap.add_argument("--limit-frames", type=int, default=None,
                     help="ограничить число кадров (для быстрой проверки)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    import ultralytics
    import eval_track

    real_yolo = ultralytics.YOLO

    def yolo_or_tflite(path, *a, **kw):
        return TFLiteModel(str(path)) if str(path).endswith(".tflite") \
            else real_yolo(path, *a, **kw)

    ultralytics.YOLO = yolo_or_tflite

    names = sorted(f for f in os.listdir(args.images) if f.endswith(".jpg"))
    subset = None
    if args.limit_frames:
        allowed = set(names[:args.limit_frames])
        subset = allowed.__contains__

    kw = dict(images_dir=args.images, labels_dir=args.labels, subset_of=subset)
    if args.realizations:
        kw["realizations"] = args.realizations

    report = {}
    targets = [("pytorch", args.weights),
               ("fp32", os.path.join(args.models_dir, "surf_fp32.tflite")),
               ("w8a32", os.path.join(args.models_dir, "surf_w8a32.tflite"))]
    for label, path in targets:
        if not os.path.exists(path):
            print(f"{label}: файла нет, пропуск")
            continue
        print(f"\n=== {label} ===", flush=True)
        r = eval_track.evaluate_track(weights=path, **kw)
        report[label] = r
        print(f"  порог, выровненный по FP={r['aligned_fp_per_window']}: "
              f"{r['aligned_threshold']:.4f}")
        print(f"  полнота (выровненная): {frac(r['completeness_aligned']):.4f}")
        for b, v in sorted(r["completeness_aligned"].items()):
            print(f"    {b:>8s}: {v['found']}/{v['total']} = {v['found'] / v['total']:.4f}")

    if "pytorch" in report:
        base = frac(report["pytorch"]["completeness_aligned"])
        print("\n=== итог: падение полноты ШТАТНОЙ метрикой ===")
        for label in ("fp32", "w8a32"):
            if label in report:
                v = frac(report[label]["completeness_aligned"])
                print(f"  {label:6s} {v:.4f}  падение {base - v:+.4f}  "
                      f"(допуск тикета 0.01)")

    if args.out:
        json.dump(report, open(args.out, "w"), indent=2, ensure_ascii=False)
        print(f"\nотчёт: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
