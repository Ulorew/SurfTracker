#!/usr/bin/env python3
"""Три уровня сверки камерного зрения (тикет «камерное зрение», А.3).

Уровни разведены намеренно: они отвечают на разные вопросы, и сваливать их в
один вердикт значит не знать, что чинить.

  1. КОНВЕРТАЦИЯ. Сырой YUV с телефона -> RGB своим кодом; сравнение с
     RGB-кропом, который телефон отдал в сеть. Допуск: медиана |ΔRGB| <= 3 на
     канал (разные матрицы коэффициентов дают до ~2). Больше 5 — расходятся
     диапазоны (full/limited range), это ГЛАВНЫЙ подозреваемый.
  2. ИНФЕРЕНС. Детекции телефона против прогона того же .tflite на ТЕЛЕФОННОМ
     RGB интерпретатором ноутбука. Отделяет конвертацию от инференса: если
     уровень 1 прошёл, а этот нет, дело в модели или в упаковке тензора.
  3. СЦЕНА. Детекции против истины: на кадрах с целью — цель найдена в кропе,
     на пустых — детекций выше порога нет. Допуск мягкий: сцена снимается
     через экран (переэкспонирование, муар, ресемплинг), просадка
     уверенности ожидаема, ноль на крупной цели — нет.

    python verify.py [out_dir]
"""
import json
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from yuv_host import to_rgb, y_stats  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
MODEL = os.path.join(ROOT, "export/phone_pack/surf_w8a32.tflite")
CONF_MAIN = 0.25      # мягкий порог: «нашли или нет»
CONF_PROD = 0.5593    # выровненный порог модели, для строгого счёта


def load_shot(out, tag):
    j = json.load(open(os.path.join(out, tag + ".json")))
    png = cv2.imread(os.path.join(out, tag + ".rgb.png"))
    return j, (None if png is None else cv2.cvtColor(png, cv2.COLOR_BGR2RGB))


def level1(out, tag, j, phone_rgb):
    """Независимая конвертация против телефонной."""
    base = os.path.join(out, tag)
    if not os.path.exists(base + ".yuvmeta.json") or phone_rgb is None:
        return {"вердикт": "нет данных"}
    S = j["side"]
    host = to_rgb(base, j["crop_x"], j["crop_y"], S, S)
    d = np.abs(host.astype(np.int16) - phone_rgb.astype(np.int16))
    med = [float(np.median(d[..., c])) for c in range(3)]
    res = {"медиана_дельты_RGB": [round(m, 2) for m in med],
            "p95_дельты": [round(float(np.percentile(d[..., c], 95)), 1) for c in range(3)],
            "доля_пикселей_дельта_больше_5": round(float((d.max(axis=2) > 5).mean()), 4)}
    worst = max(med)
    res["вердикт"] = ("прошёл" if worst <= 3 else
                       "РАСХОЖДЕНИЕ ДИАПАЗОНОВ (проверить full/limited range)"
                       if worst > 5 else "на границе допуска")
    # Контрольная гипотеза: а если камера отдаёт видеодиапазон?
    if worst > 3:
        alt = to_rgb(base, j["crop_x"], j["crop_y"], S, S, video_range=True)
        da = np.abs(alt.astype(np.int16) - phone_rgb.astype(np.int16))
        res["если_видеодиапазон_медиана"] = [round(float(np.median(da[..., c])), 2)
                                              for c in range(3)]
    return res


_it = None


def laptop_infer(rgb):
    """Тот же .tflite интерпретатором ноутбука на ТЕЛЕФОННОМ RGB."""
    global _it
    from ai_edge_litert.interpreter import Interpreter
    if _it is None:
        _it = Interpreter(model_path=MODEL)
        _it.allocate_tensors()
    inp = _it.get_input_details()[0]
    x = (rgb.astype(np.float32) / 255.0)[None]
    if list(inp["shape"])[1] == 3:
        x = x.transpose(0, 3, 1, 2)
    _it.set_tensor(inp["index"], x.astype(inp["dtype"]))
    _it.invoke()
    o = _it.get_tensor(_it.get_output_details()[0]["index"])[0]   # [5, 8400]
    S = rgb.shape[0]
    dets = []
    for i in range(o.shape[1]):
        c = float(o[4, i])
        if c < 0.01:
            continue
        cx, cy, w, h = (float(o[k, i]) * S for k in range(4))
        dets.append({"x0": cx - w / 2, "y0": cy - h / 2, "x1": cx + w / 2,
                      "y1": cy + h / 2, "conf": c})
    return dets


def nms(dets, iou_thr=0.5):
    dets = sorted(dets, key=lambda d: -d["conf"])
    keep = []
    for d in dets:
        if all(_iou(d, k) < iou_thr for k in keep):
            keep.append(d)
    return keep


def _iou(a, b):
    x0, y0 = max(a["x0"], b["x0"]), max(a["y0"], b["y0"])
    x1, y1 = min(a["x1"], b["x1"]), min(a["y1"], b["y1"])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    ua = (a["x1"] - a["x0"]) * (a["y1"] - a["y0"]) + (b["x1"] - b["x0"]) * (b["y1"] - b["y0"]) - inter
    return inter / ua if ua > 0 else 0.0


# Допуск на расхождение уверенности между рантаймами. Ноль тут недостижим:
# на телефоне LiteRT со своим XNNPACK, на ноутбуке — свой, порядок сложения
# в свёртках разный. Замер на пробном снимке: 0.121 против 0.1123. Допуск
# взят с запасом ВЫШЕ наблюдаемого, но НИЖЕ того, что способно изменить
# решение (порог модели 0.18-0.56, шаг решения — десятые).
CONF_TOL = 0.02


def level2(j, phone_rgb):
    """Сверка на НИЗКОМ пороге: сравнивать надо сами рамки, а не то, что
    осталось после отсечки. На пробном снимке отсечка 0.25 не оставила ни
    одной рамки ни у той, ни у другой стороны, и вердикт «прошёл» не значил
    ничего."""
    if phone_rgb is None or not os.path.exists(MODEL):
        return {"вердикт": "нет данных"}
    LOW = 0.02
    phone = nms([d for d in j.get("detections", []) if d["conf"] >= LOW])
    host = nms([d for d in laptop_infer(phone_rgb) if d["conf"] >= LOW])
    pairs = []
    for p in phone:
        # key=, а не сравнение кортежей: при равном IoU python полез бы
        # сравнивать сами словари и падал
        best = max(((_iou(p, hh), hh) for hh in host), key=lambda t: t[0],
                   default=(0.0, None))
        pairs.append((p, best[1], best[0]))
    matched = [x for x in pairs if x[2] >= 0.5]
    dconf = max((abs(p["conf"] - q["conf"]) for p, q, _ in matched), default=0.0)
    if not phone and not host:
        verdict = "нечего сравнивать (обе стороны пусты)"
    elif len(phone) == len(host) == len(matched) and dconf <= CONF_TOL:
        verdict = "прошёл"
    elif len(phone) == len(host) == len(matched):
        verdict = f"РАСХОЖДЕНИЕ CONF {dconf:.3f}"
    else:
        verdict = "РАСХОЖДЕНИЕ РАМОК"
    return {"рамок_телефон": len(phone), "рамок_ноутбук": len(host),
            "совпало": len(matched), "макс_дельта_conf": round(dconf, 4),
            "вердикт": verdict}


def level3(rec, j):
    dets = [d for d in j.get("detections", []) if d["conf"] >= CONF_MAIN]
    dets = nms(dets)
    S = j["side"]
    if rec["n_targets"] == 0:
        strong = [d for d in dets if d["conf"] >= CONF_PROD]
        return {"цель": "нет", "рамок_выше_0.25": len(dets),
                "рамок_выше_прод_порога": len(strong),
                "вердикт": "прошёл" if not strong else "ЛОЖНЫЕ НА ПУСТОМ"}
    # Цель по построению в центре кропа (наведение), с точностью гомографии
    ecx = ecy = S / 2.0
    exp_size = rec.get("target_px_sensor") or 0
    radius = 0.5 * exp_size if exp_size else S / 4.0
    hits = [d for d in dets
            if np.hypot((d["x0"] + d["x1"]) / 2 - ecx, (d["y0"] + d["y1"]) / 2 - ecy) <= radius]
    return {"цель": "есть", "ожидаемый_размер_px": round(exp_size),
            "радиус_зачёта_px": round(radius),
            "рамок_всего": len(dets), "попало_в_цель": len(hits),
            "лучшая_conf": round(max((d["conf"] for d in hits), default=0.0), 3),
            "вердикт": "прошёл" if hits else "ЦЕЛЬ НЕ НАЙДЕНА"}


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "out")
    shots = json.load(open(os.path.join(out, "shots.json")))
    calib = json.load(open(os.path.join(out, "calib.result.json")))
    scale = calib["масштаб_кадр_в_сенсор"]
    rows = []
    for rec in shots:
        tag = rec["tag"]
        if rec["target_px"]:
            rec["target_px_sensor"] = rec["target_px"] * scale
        j, rgb = load_shot(out, tag)
        if not j.get("ok"):
            rows.append({"tag": tag, "ошибка": j.get("error", "?")})
            continue
        rows.append({"tag": tag,
                     "1_конвертация": level1(out, tag, j, rgb),
                     "2_инференс": level2(j, rgb),
                     "3_сцена": level3(rec, j)})
    res = {"калибровка": {"ошибка_px": calib["ошибка_px"],
                           "масштаб": round(scale, 3),
                           "яркость_Y": calib["яркость_Y"]},
            "кадры": rows}
    json.dump(res, open(os.path.join(out, "verify.json"), "w"), ensure_ascii=False, indent=1)

    print(f"калибровка: ошибка p95 {calib['ошибка_px']['p95']:.1f} px, "
          f"масштаб кадр->сенсор {scale:.3f}")
    ys = calib["яркость_Y"]
    print(f"яркость Y: {ys['min']}..{ys['max']}, ниже 16 — {ys['доля_ниже_16']:.4f}, "
          f"выше 235 — {ys['доля_выше_235']:.4f}  "
          f"({'ВИДЕОДИАПАЗОН' if ys['min'] >= 15 and ys['max'] <= 236 else 'полный диапазон'})")
    print(f"\n{'кадр':24s} {'1 конвертация':>26s} {'2 инференс':>22s} {'3 сцена':>24s}")
    for r in rows:
        if "ошибка" in r:
            print(f"{r['tag']:24s}   ОШИБКА: {r['ошибка'][:60]}")
            continue
        print(f"{r['tag']:24s} {r['1_конвертация']['вердикт']:>26s} "
              f"{r['2_инференс']['вердикт']:>22s} {r['3_сцена']['вердикт']:>24s}")
    ok = all("ошибка" not in r and all(r[k]["вердикт"] == "прошёл"
                                        for k in ("1_конвертация", "2_инференс", "3_сцена"))
             for r in rows)
    print("\nВЕРДИКТ:", "зрение доказано, камерные прогоны разблокированы" if ok
          else "НЕ доказано — см. verify.json")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
