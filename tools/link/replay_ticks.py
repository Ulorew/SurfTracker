#!/usr/bin/env python3
"""Репроигрыш отдельных тактов записанного прогона: почему цель не взяли.

ЗАЧЕМ. Лог говорит, ЧТО решила петля (кандидатов столько-то, выбран такой-то,
до предсказания столько), но при отказе не говорит, ГДЕ был отвергнутый
кандидат — а без этого «модель не нашла» и «алгоритм отверг» неразличимы.

Инструмент прогоняет ТУ ЖЕ МОДЕЛЬ на СОХРАНЁННОМ КАДРЕ МОДЕЛИ
(frames/NNNNN.jpg — ровно тот тензор 640x640, который видела сеть на
телефоне) и печатает каждого кандидата с приговором по каждому механизму
отбора порознь: радиус в прежней форме, радиус в новой, вето механизма А.

МОДЕЛЬ БЕРЁТСЯ ИЗ run.json И СВЕРЯЕТСЯ. Первая редакция этого не делала, и
подставленная не та модель (сёрферная вместо COCO) дала ноль детекций на
кадре, где телефон нашёл цель с уверенностью 0.886 — то есть инструмент
уверенно ответил «вопрос к модели» там, где вопрос был к нему самому.
Несовпадение теперь останавливает прогон.

NMS берётся из стенда nms_check — той самой реализации, что посимвольно
сличена с телефонной на 172 случаях. Своя копия здесь означала бы, что
репроигрыш меряет не тот отбор, что боевой.

    replay_ticks.py runs/phone/260818_1059_полный 409 419 433 719

Требует папку frames (пишется при rec=true) и модель в models/phone/.
"""
import argparse
import csv
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools", "windowing", "nms_check"))

WINDOW_K = 3.5           # TRACK_WINDOW_K
MIN_WINDOW = 640         # DETECT_MIN_WINDOW_PX
SELECT_FRAC = 0.30       # TARGET_SELECT_MAX_DIST_FRAC
EXPAND_PER_MISS = 1.15   # WINDOW_EXPAND_PER_MISS
MAX_EXPAND = 64          # MAX_EXPAND_STEPS
DIAG_FRAC = 0.5          # TARGET_RADIUS_VIEW_DIAG_FRAC
VETO_RATIO = 1.8         # SIZE_VETO_RATIO
NET = 640
MAX_DET = 16


def число(row, key, default=float("nan")):
    v = row.get(key, "")
    if v in ("", "nan", None):
        return default
    try:
        return float(v)
    except ValueError:
        return default


def инференс(interp, путь):
    import numpy as np
    from PIL import Image
    im = Image.open(путь).convert("RGB")
    if im.size != (NET, NET):
        sys.exit(f"кадр {путь} размера {im.size}, ожидался {NET}x{NET}")
    a = np.asarray(im, dtype=np.float32) / 255.0          # HWC
    x = np.transpose(a, (2, 0, 1))[None, ...]             # NCHW, как на телефоне
    inp = interp.get_input_details()[0]
    interp.set_tensor(inp["index"], x)
    interp.invoke()
    return interp.get_tensor(interp.get_output_details()[0]["index"])[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("ticks", nargs="+", type=int)
    ap.add_argument("--model", default=None,
                    help="по умолчанию — та, что записана в run.json прогона")
    args = ap.parse_args()

    run = args.run if os.path.isabs(args.run) else os.path.join(ROOT, args.run)
    frames = os.path.join(run, "frames")
    if not os.path.isdir(frames):
        sys.exit(f"нет папки {frames} — прогон стянут без кадров модели.\n"
                 f"Кадры пишутся при rec=true; стянуть: tools/link/pull_run.sh <имя>")

    cfg = json.load(open(os.path.join(run, "run.json")))
    имя_модели = cfg.get("модель")
    модель = args.model or os.path.join(ROOT, "models", "phone", имя_модели or "")
    if not имя_модели:
        sys.exit("в run.json нет поля «модель» — сверить нечем, отказываюсь гадать")
    if not os.path.exists(модель):
        sys.exit(f"нет модели {модель}.\nПрогон шёл на «{имя_модели}»; стянуть с телефона:\n"
                 f"  adb pull /sdcard/Android/data/com.surftracker.camfps/files/{имя_модели} "
                 f"models/phone/")
    if args.model and os.path.basename(args.model) != имя_модели:
        sys.exit(f"МОДЕЛЬ НЕ ТА: прогон шёл на «{имя_модели}», подставлена "
                 f"«{os.path.basename(args.model)}». Репроигрыш другой моделью отвечает "
                 f"на другой вопрос.")

    with open(os.path.join(run, "log.csv")) as f:
        by_i = {int(r["i"]): r for r in csv.DictReader(f)}

    from ai_edge_litert.interpreter import Interpreter
    import nms_ref
    interp = Interpreter(model_path=модель)
    interp.allocate_tensors()
    print(f"модель: {имя_модели}, кадр {cfg.get('сенсор')}, окно {cfg.get('окно')}")

    W, H = (int(v) for v in str(cfg.get("сенсор", "1920x1440")).split("x"))

    for tick in args.ticks:
        r, prev = by_i.get(tick), by_i.get(tick - 1)
        if r is None or prev is None:
            print(f"\n=== такт {tick}: нет строки в логе ===")
            continue

        # Центр приёма и окно — из ПРЕДЫДУЩЕЙ строки: план на этот такт
        # составлялся в конце прошлого. Сверено по логу: p50 = 1.8 px.
        pcx, pcy = число(prev, "winCx"), число(prev, "winCy")
        sc, filt = число(prev, "Sc"), число(prev, "размер_фильтра")
        # PNG ПРЕДПОЧТИТЕЛЬНЕЕ. Кадры модели пишутся в JPEG качества 60, и
        # сжатие смещает уверенности: на сличении с лослесс-дампами того же
        # прогона JPEG сдвигал 0.646 в 0.451 и однажды РОДИЛ кандидата 0.365
        # там, где телефон не нашёл ничего. Направление опасное — не только
        # потеря детекции, но и приписанная.
        путь = None
        for имя in (f"{tick:05d}.png", f"{tick:05d}.jpg"):
            к = os.path.join(frames, имя)
            if os.path.exists(к):
                путь = к
                break
        if путь is None:
            print(f"\n=== такт {tick}: нет кадра {путь} ===")
            continue

        cropX = min(max(pcx - sc / 2, 0), W - sc)
        cropY = min(max(pcy - sc / 2, 0), H - sc)
        масштаб = sc / NET

        raw = инференс(interp, путь)
        cols = [[float(raw[row][a]) for row in range(5)] for a in range(raw.shape[1])]
        dets = nms_ref.nms(cols, MAX_DET)

        # РАСШИРЕНИЕ НА ПРОМАХАХ учитывается в ОБЕИХ колонках. Прежде «было»
        # брало сторону из лога (в ней расширение уже сидит), а «стало»
        # считалось по своей формуле без него — две соседние колонки одной
        # таблицы жили по разным правилам, и на такте 409 печаталось 1239
        # вместо 1425. Промахи берутся из ПРЕДЫДУЩЕЙ строки: это состояние ДО
        # такта, от него и считался приём.
        промахов = int(число(prev, "промахов", 0))
        рост = EXPAND_PER_MISS ** min(промахов, MAX_EXPAND)
        радиус_было = SELECT_FRAC * sc                              # от ПРИЖАТОГО окна
        радиус_стало = min(SELECT_FRAC * max(WINDOW_K * filt, MIN_WINDOW) * рост,
                           DIAG_FRAC * math.hypot(W, H))

        print(f"\n=== такт {tick} ===")
        print(f"  лог: есть_цель={r.get('есть_цель')} conf={r.get('conf')} "
              f"кандидатов={r.get('кандидатов')} промахов={r.get('промахов')}")
        if путь.endswith(".jpg"):
            print("  ВНИМАНИЕ: кадр в JPEG q60, уверенности смещены сжатием")
        print(f"  ведомый размер {filt:.0f}, окно {sc:.0f} "
              f"(без потолка было бы {max(WINDOW_K * filt, MIN_WINDOW):.0f})")
        print(f"  радиус приёма: было {радиус_было:.0f}, стало {радиус_стало:.0f}")
        # СЛИЧЕНИЕ С ЛОГОМ. Инструмент, чьи детекции расходятся с записанными,
        # отвечает не про этот прогон. Прежде расхождение печаталось рядом на
        # экране и молчало.
        было = число(r, "кандидатов", float("nan"))
        if not math.isnan(было) and len(dets) != int(было):
            print(f"  РАСХОЖДЕНИЕ С ЛОГОМ: в записи {int(было)} кандидатов, "
                  f"здесь {len(dets)} — репроигрыш не воспроизводит прогон")
        if not dets:
            print("  ДЕТЕКТОР НЕ НАШЁЛ НИЧЕГО — вопрос к модели, не к алгоритму")
            continue
        print(f'  {"#":>2} {"conf":>5} {"размер":>7} {"до предск.":>11} '
              f'{"отн.":>5}  {"было":>6} {"стало":>6} {"вето А":>8}')
        for k, d in enumerate(dets):
            cx = cropX + d[0] * NET * масштаб
            cy = cropY + d[1] * NET * масштаб
            размер = d[2] * NET * масштаб
            dist = math.hypot(cx - pcx, cy - pcy)
            отн = размер / filt if filt > 0 else float("nan")
            вето = "ОТСЕЧЁН" if (отн > VETO_RATIO or отн < 1 / VETO_RATIO) else "прошёл"
            print(f"  {k:>2} {d[5]:>5.2f} {размер:>7.0f} {dist:>11.0f} {отн:>5.2f}  "
                  f'{"взят" if dist <= радиус_было else "мимо":>6} '
                  f'{"взят" if dist <= радиус_стало else "мимо":>6} {вето:>8}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
