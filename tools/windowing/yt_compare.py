#!/usr/bin/env python3
"""Сравнение режимов ограничения убеждения полем зрения на ютубном проходе.

ЧТО ЭТИ ЧИСЛА ЗНАЧАТ И ЧЕГО НЕ ЗНАЧАТ. Разметки на ютубном материале нет,
поэтому «ведение» — это доля тактов, где принят ЛЮБОЙ кандидат, а не доля
тактов на правильной цели. Рост ведения после правки может означать и
восстановление цели, и то, что петля стала хватать соседа. Различить может
только глаз — отсюда список худших минут.

Что от разметки НЕ зависит и потому здесь главное: доля тактов, на которых
центр окна лежит ВНЕ кадра. Окно за пределами кадра показывает модели поля,
и это неправильно независимо от того, кого петля потом выберет.
"""
import json
import math
import os
import sys
from collections import defaultdict

TICK_HZ = 3.0


def read_run(jsonl):
    cfg_path = jsonl[:-6] + ".runcfg.json"
    if not os.path.exists(cfg_path):
        return None
    cfg = json.load(open(cfg_path))
    rows = [json.loads(l) for l in open(jsonl) if l.strip()]
    if not rows:
        return None
    W, H = cfg["intrinsics"]["cx"] * 2, cfg["intrinsics"]["cy"] * 2
    n = len(rows)
    off = sum(1 for r in rows
              if not (0 <= r["predicted_cx"] <= W and 0 <= r["predicted_cy"] <= H))
    return {
        "тактов": n,
        "минут": n / TICK_HZ / 60.0,
        "ведение": sum(1 for r in rows if r["chosen"] is not None) / n,
        "вне_кадра": off / n,
        "в_потере": sum(1 for r in rows if r["status"] == "lost") / n,
        "потерь": sum(1 for r in rows if r.get("lost_transition")),
        "возвратов": sum(1 for r in rows if r.get("reacquired")),
        "режим": cfg.get("view_clamp", "off (прогон до правки)"),
    }


def collect(d):
    out = {}
    for f in sorted(os.listdir(d)):
        if f.endswith(".jsonl"):
            r = read_run(os.path.join(d, f))
            if r:
                out[f[:-6]] = r
    return out


def main():
    dirs = sys.argv[1:] or [
        "tools/windowing/output/yt_pass",
        "tools/windowing/output/yt_pass_window",
        "tools/windowing/output/yt_pass_frame",
    ]
    runs = {os.path.basename(d): collect(d) for d in dirs if os.path.isdir(d)}
    runs = {k: v for k, v in runs.items() if v}
    if len(runs) < 2:
        print("нечего сравнивать: нужны хотя бы два каталога с прогонами")
        return 1

    # Сравнение ТОЛЬКО по общим видео: наборы файлов у прогонов могут
    # отличаться (базовый проход фильтровал дубликаты и AV1), и среднее по
    # разным наборам сравнивало бы разное с разным.
    common = set.intersection(*(set(v) for v in runs.values()))
    dropped = {k: sorted(set(v) - common) for k, v in runs.items()}
    print(f"общих видео: {len(common)}")
    for k, miss in dropped.items():
        if miss:
            print(f"  не в пересечении ({k}): {len(miss)} — {', '.join(m[:28] for m in miss)}")

    print(f"\n{'режим':22s} {'минут':>7s} {'ведение':>9s} {'вне кадра':>11s} "
          f"{'в потере':>10s} {'потерь/мин':>11s} {'возвр/мин':>10s}")
    base = None
    for name, v in runs.items():
        mins = sum(v[k]["минут"] for k in common)
        w = lambda field: sum(v[k][field] * v[k]["минут"] for k in common) / mins
        lead, off, lost = w("ведение"), w("вне_кадра"), w("в_потере")
        pl = sum(v[k]["потерь"] for k in common) / mins
        pr = sum(v[k]["возвратов"] for k in common) / mins
        mode = next(iter(v.values()))["режим"]
        print(f"{mode:22s} {mins:7.1f} {lead:9.3f} {off:11.3f} {lost:10.3f} {pl:11.2f} {pr:10.2f}")
        if base is None:
            base = (lead, off)
    print("\nвзвешивание по минутам видео; проценты — доли тактов")

    # Подробно по видео: где правка помогла, где нет
    if len(runs) >= 2:
        names = list(runs)
        print(f"\nведение по видео ({' -> '.join(names)}):")
        for k in sorted(common, key=lambda x: -runs[names[0]][x]["минут"]):
            cells = "  ".join(f"{runs[n][k]['ведение']:.3f}/{runs[n][k]['вне_кадра']:.2f}"
                              for n in names)
            print(f"  {k[:44]:46s} {cells}")
        print("  (в каждой ячейке: ведение / доля тактов вне кадра)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
