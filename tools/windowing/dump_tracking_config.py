#!/usr/bin/env python3
"""Снимок конфигурации трекинга одним файлом (мини-тикет "заморозка", п.4).

Зачем отдельный файл, если константы и так в tracking_config.py: модуль
меняется, а замороженная версия должна остаться сравнимой. Снимок содержит
значения, коммит, на котором они действовали, и измеренные счета — чтобы
через полгода не пришлось восстанавливать по памяти, какая именно
конфигурация давала эти числа.

    python dump_tracking_config.py --out ../../configs/tracking_v1.json
"""
import argparse
import json
import os
import subprocess
import sys

import tracking_config as tcfg

HERE = os.path.dirname(os.path.abspath(__file__))

# Флаги track_run.py, соответствующие измеренным конфигурациям. Держатся здесь
# рядом со снимком, а не в прозе отчёта: строку из отчёта не запустишь.
MEASURED = {
    "prod_as_specified": {
        "флаги": ["--shadows"],
        "описание": "буквально по мини-тикету: база + демпфирование + теневые",
        "клипы_доля_на_цели": 0.526,
    },
    "prod_plus_A": {
        "флаги": ["--shadows", "--enable-a"],
        "описание": "то же + механизм А (размер в счёте и вето по размеру)",
        "клипы_доля_на_цели": 0.732,
    },
    "prod_plus_kalman": {
        "флаги": ["--shadows", "--filter-level", "2", "--enable-gate"],
        "описание": "то же + Калман и махаланобисов гейт, но БЕЗ механизма А",
        "клипы_доля_на_цели": 0.616,
    },
    "measured_best": {
        "флаги": ["--enable-a", "--filter-level", "2", "--enable-gate", "--shadows"],
        "описание": "А + Калман + гейт + теневые — конфигурация, давшая числа, "
                     "на которых основан мини-тикет",
        "клипы_доля_на_цели": 0.841,
    },
    "base_A": {
        "флаги": ["--enable-a"],
        "описание": "прежняя база без теневых",
        "клипы_доля_на_цели": 0.734,
    },
    "prod_frozen": {
        "флаги": ["--enable-a", "--filter-level", "2", "--enable-gate", "--shadows",
                   "--score-form", "distance"],
        "описание": "ЗАМОРОЖЕННЫЙ прод-состав (тег tracking-v1-frozen). Флаги "
                     "перечислены полностью, хотя часть уже умолчания: строка "
                     "обязана задавать конфигурацию целиком",
        "клипы_доля_на_цели": 0.841,
        "клипы_оговорка": "8 стресс-проездов, счёт на худшем случае, не ожидание для поля",
        "стенд_доля_без_подмены": 0.857,
        "известный_риск": {
            "имя": "закрепление ошибки (error_lockin)",
            "P_возврата_после_одной_ошибки": 0.496,
            "ожидалось": 0.8,
            "растёт_ли_со_временем": False,
            "ловушка_захлопывается_без_ошибки_доля_прогонов": 0.404,
            "сценарий": "track_bench.scen_error_lockin",
            "тесты": "tests/test_error_lockin.py",
        },
    },
}


def git(*args):
    try:
        return subprocess.run(["git"] + list(args), cwd=HERE, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return None


def measured_from_matrix(path):
    """Числа берутся ИЗ ПРОГОНА, а не из литералов в этом файле.

    Валидация показала, что прежний блок MEASURED был захардкожен: снимок
    выглядел выходом конвейера, а был ручной записью, и одно из чисел (0.857)
    не воспроизводилось никогда. Теперь снимок либо содержит числа реального
    прогона, либо честно говорит, что их нет.
    """
    if not path or not os.path.exists(path):
        return None
    data = json.load(open(path))
    res, out = data["results"], {}
    for key, r in res.items():
        cfg_name, hz, clip = key.split("|")
        if "error" in r:
            continue
        d = out.setdefault(cfg_name, {"такт_гц": float(hz), "проезды": {},
                                       "флаги": data["configs"].get(cfg_name)})
        d["проезды"][clip] = {k: r.get(k) for k in
                              ("on_target_all", "swap_all", "refuse_all",
                               "lead_fraction", "n_hit", "n_swap", "n_refuse")}
    for cfg_name, d in out.items():
        p = d["проезды"].values()
        n = len(p) or 1
        d["среднее_по_проездам"] = {
            k: round(sum(x[k] for x in p) / n, 4)
            for k in ("on_target_all", "swap_all", "refuse_all", "lead_fraction")}
        d["оговорка"] = ("8 стресс-проездов, знаменатель — все размеченные такты; "
                          "различия между составами на этой выборке не разрешимы "
                          "(максимум ~2 se)")
    return {"источник": os.path.abspath(path), "веса": data.get("weights"),
            "конфигурации": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--note", default="")
    ap.add_argument("--version", default="tracking-v2")
    ap.add_argument("--matrix", default=None,
                     help="summary.json прогона матрицы: числа берутся ОТТУДА, "
                          "а не из литералов")
    args = ap.parse_args()

    values = {k: getattr(tcfg, k) for k in dir(tcfg) if k.isupper()}
    measured = measured_from_matrix(args.matrix)
    snapshot = {
        "версия": args.version,
        "коммит": git("rev-parse", "HEAD"),
        "ветка": git("rev-parse", "--abbrev-ref", "HEAD"),
        # Различаем ИЗМЕНЁННЫЕ отслеживаемые файлы и просто неотслеживаемые:
        # первые меняют измеряемое поведение, вторые нет. Раньше здесь стоял
        # общий --porcelain, и снимок объявлял дерево грязным из-за случайной
        # новой папки рядом — тревога, которая обесценивает саму проверку.
        "дерево_чистое": git("status", "--porcelain", "--untracked-files=no") == "",
        "неотслеживаемые": [l[3:] for l in (git("status", "--porcelain") or "").splitlines()
                             if l.startswith("??")] or None,
        "примечание": args.note,
        "значения": values,
        "измерено_прогоном": measured,
        "измеренные_конфигурации_РУЧНЫЕ_ЛИТЕРАЛЫ": MEASURED,
        "клипы": "Data/frames/val_manual, 8 проездов, такт 3 Гц, "
                  "веса models/night_legacy_s3_best.pt",
        "стенд": "track_bench.py, 8 сценариев по 500 прогонов",
    }
    if measured is None:
        snapshot["ВНИМАНИЕ"] = ("--matrix не задан: числа прогоном не "
                                 "подтверждены, в блоке ниже ручные литералы")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False, sort_keys=False)
    print(f"снимок: {args.out}")
    print(f"  коммит {snapshot['коммит']}, дерево "
          f"{'чистое' if snapshot['дерево_чистое'] else 'ГРЯЗНОЕ'}")
    print(f"  констант: {len(values)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
