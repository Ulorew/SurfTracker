#!/usr/bin/env python3
"""Сводка ютубного прохода (тикет "ночь", блок 2).

ЧТО ЗДЕСЬ НЕ СЧИТАЕТСЯ И ПОЧЕМУ. Честная тройка (на цели / подмена / отказ)
требует разметки: без неё нельзя сказать, ту ли цель ведёт петля. Поэтому
считается ПАРА, которая от разметки не зависит:

    ведение = доля тактов, где принят кандидат
    отказ   = доля тактов, где не принят никто

плюс события: потери, повторные захваты, серии пропусков. Подмена без
разметки не определяется в принципе — отсюда топ худших минут для просмотра
глазами.

Статус ожиданий ПОНИЖЕННЫЙ: f_x у ютубных — прикидки, камеры движутся и
зумят, метрика меряет смесь свойств трекера и свойств съёмки.
"""
import json
import os
import sys
from collections import defaultdict


def load(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def per_video(rows, tick_hz=3.0):
    n = len(rows)
    lead = sum(1 for r in rows if r["chosen"] is not None)
    lost = sum(1 for r in rows if r.get("lost_transition"))
    reacq = sum(1 for r in rows if r.get("reacquired"))
    minutes = n / tick_hz / 60.0
    # худшие минуты: больше всего отказов подряд
    by_min = defaultdict(lambda: {"n": 0, "refuse": 0, "lost": 0})
    for r in rows:
        m = int(r["timestamp_sec"] // 60)
        b = by_min[m]
        b["n"] += 1
        b["refuse"] += r["chosen"] is None
        b["lost"] += bool(r.get("lost_transition"))
    worst = sorted(((m, b) for m, b in by_min.items() if b["n"] >= 30),
                   key=lambda x: (-x[1]["refuse"] / x[1]["n"], -x[1]["lost"]))[:5]
    return {
        "тактов": n, "минут": round(minutes, 1),
        "ведение": round(lead / n, 3) if n else 0,
        "отказ": round(1 - lead / n, 3) if n else 0,
        "потерь_на_минуту": round(lost / minutes, 2) if minutes else 0,
        "возвратов_на_минуту": round(reacq / minutes, 2) if minutes else 0,
        "худшие_минуты": [{"минута": m, "отказ": round(b["refuse"] / b["n"], 2),
                            "потерь": b["lost"]} for m, b in worst],
    }


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "tools/windowing/output/yt_pass"
    out, tot = {}, {"тактов": 0, "минут": 0.0, "ведение_взв": 0.0, "потерь": 0, "возвратов": 0}
    for f in sorted(os.listdir(d)):
        if not f.endswith(".jsonl"):
            continue
        rows = load(os.path.join(d, f))
        if not rows:
            continue
        v = per_video(rows)
        out[f[:-6]] = v
        tot["тактов"] += v["тактов"]
        tot["минут"] += v["минут"]
        tot["ведение_взв"] += v["ведение"] * v["минут"]
        tot["потерь"] += round(v["потерь_на_минуту"] * v["минут"])
        tot["возвратов"] += round(v["возвратов_на_минуту"] * v["минут"])

    print(f"{'видео':52s} {'минут':>6s} {'ведение':>8s} {'отказ':>7s} {'потерь/мин':>11s} {'возвр/мин':>10s}")
    for k, v in sorted(out.items(), key=lambda x: -x[1]["минут"]):
        print(f"{k[:50]:52s} {v['минут']:6.1f} {v['ведение']:8.3f} {v['отказ']:7.3f} "
              f"{v['потерь_на_минуту']:11.2f} {v['возвратов_на_минуту']:10.2f}")
    m = tot["минут"]
    print(f"\n{'ИТОГО':52s} {m:6.1f} {tot['ведение_взв']/m:8.3f} {1-tot['ведение_взв']/m:7.3f} "
          f"{tot['потерь']/m:11.2f} {tot['возвратов']/m:10.2f}")
    print(f"\nТоп-5 худших минут (для просмотра глазами):")
    allw = []
    for k, v in out.items():
        for w in v["худшие_минуты"]:
            allw.append((w["отказ"], w["потерь"], k, w["минута"]))
    for r, l, k, mn in sorted(allw, reverse=True)[:5]:
        # mn — номер минуты от начала, значит таймкод mn:00, а не mn секунд
        print(f"  отказ {r:.2f}, потерь {l}  —  {k[:44]}  с {mn}:00 по {mn+1}:00")
    json.dump(out, open(os.path.join(d, "summary.json"), "w"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
