#!/usr/bin/env python3
"""Что ПРОД НА САМОМ ДЕЛЕ исполняет: список достижимых и мёртвых функций.

ЗАЧЕМ ЭТО ЗАВЕДЕНО. 5 августа было принято решение «допуск по положению при
сопоставлении берётся из предсказанного размера цели, а не из размера
кандидата». Оно было реализовано в четырёх местах порознь, и три из них
правку получили. Не получило её `gate_distance2` — единственное из четырёх,
что при боевой конфигурации ВЫЗЫВАЕТСЯ: `SCORE_FORM = "distance"` означает,
что ни одна форма счёта не исполняется вовсе.

Тринадцать дней прод отбирал кандидатов по правилу, которое считалось
отменённым. Флаг в конфиге стоял в True и выглядел как решение, принятое
повсеместно; тесты покрывали починенную ветку; телефонный перенос честно
скопировал живую, непочиненную.

Ни один существующий инструмент этого поймать не мог, потому что все они
отвечают на вопрос «верно ли то, что написано», и ни один — на вопрос «а это
вообще исполняется?».

КАК РАБОТАЕТ. Боевая конфигурация гоняется по синтетическим сценариям под
`sys.settrace`, который записывает каждый вход в функцию. Дальше список
входов сличается со списком всех функций модулей трекинга. Что не вошло — то
прод не исполняет.

Мёртвая функция не порок сама по себе: диагностика, ветки под выключенными
флагами и запасные формы обязаны существовать. Порок — ЧИНИТЬ мёртвую,
думая, что чинишь живую. Отчёт отвечает ровно на этот вопрос и ни на какой
другой.

    python prod_reach.py            # отчёт
    python prod_reach.py --json     # то же машинно
"""
import argparse
import ast
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tracking_config as tcfg          # noqa: E402
import track_logic as tl                # noqa: E402

# Модули петли слежения. Обучение, датасеты и визуализация сюда не входят:
# вопрос стоит про боевой отбор цели, а не про весь репозиторий.
MODULES = ["track_logic.py", "track_kalman.py", "track_score.py",
           "track_filters.py", "track_shadows.py", "track_window.py"]

SIZE = math.radians(2.0)
DT = 0.25


def _box(cx, cy, size, conf=0.9):
    return (cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2, conf)


def scenarios():
    """Сценарии обязаны трогать ВСЕ режимы петли, иначе «мёртвое» окажется
    просто «не пройденным этим прогоном» — и отчёт начнёт врать в ту же
    сторону, что и стенд сличения до его починки."""
    out = []
    # ведение, одна цель
    out.append([[_box(0.02 * i, 0.0, SIZE)] for i in range(20)])
    # два кандидата, пересечение
    out.append([[_box(0.02 * i, 0.0, SIZE), _box(0.4 - 0.02 * i, 0.01, SIZE * 0.8)]
                for i in range(20)])
    # кандидат не того размера рядом
    out.append([[_box(0.0, 0.0, SIZE)] for _ in range(6)]
               + [[_box(0.05, 0.0, SIZE), _box(0.01, 0.0, SIZE * 0.3)]
                  for _ in range(10)])
    # потеря и возврат
    out.append([[_box(0.0, 0.0, SIZE)] for _ in range(8)]
               + [[] for _ in range(8)]
               + [[_box(0.1, 0.0, SIZE)] for _ in range(8)])
    # уход вдаль: размер падает
    out.append([[_box(0.02 * i, 0.0, SIZE * max(0.3, 1 - 0.05 * i))]
                for i in range(18)])
    # пусто с самого начала
    out.append([[] for _ in range(3)] + [[_box(0.0, 0.0, SIZE)] for _ in range(8)])
    return out


def drive():
    for ticks in scenarios():
        st = None
        for dets in ticks:
            if st is None:
                if not dets:
                    continue
                st = tl.TrackState(tcfg, 0.0, 0.0, SIZE,
                                   min_window=math.radians(0.5),
                                   max_window=math.radians(60.0),
                                   view_half_w=math.radians(36.0),
                                   view_half_h=math.radians(27.0))
                continue
            st.step(DT, dets)


def declared_functions():
    """Все def в модулях петли — через ast, а не через import: так видно и то,
    что вообще ни разу не импортировалось."""
    out = {}
    for m in MODULES:
        path = os.path.join(HERE, m)
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path).read(), filename=path)
        stack = [(tree, "")]
        while stack:
            node, prefix = stack.pop()
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    stack.append((child, prefix + child.name + "."))
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[(m, prefix + child.name)] = child.lineno
                    stack.append((child, prefix + child.name + "."))
    return out


def reached():
    seen = set()

    def tracer(frame, event, arg):
        if event != "call":
            return None
        fn = frame.f_code.co_filename
        if os.path.dirname(os.path.abspath(fn)) == HERE:
            seen.add((os.path.basename(fn), frame.f_code.co_name))
        return None

    sys.settrace(tracer)
    try:
        drive()
    finally:
        sys.settrace(None)
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    declared = declared_functions()
    hit_names = {(m, n.split(".")[-1]) for (m, n) in
                 [(mm, nn) for (mm, nn) in declared]}
    got = reached()
    live, dead = [], []
    for (m, qual), line in sorted(declared.items()):
        short = qual.split(".")[-1]
        (live if (m, short) in got else dead).append((m, qual, line))

    if args.json:
        print(json.dumps({"live": [f"{m}:{q}" for m, q, _ in live],
                          "dead": [f"{m}:{q}" for m, q, _ in dead]},
                         ensure_ascii=False, indent=1))
        return 0

    print(f"боевая конфигурация: SCORE_FORM={tcfg.SCORE_FORM!r}, "
          f"FILTER_LEVEL={tcfg.FILTER_LEVEL}, "
          f"гейт={tcfg.ENABLE_MAHALANOBIS_GATE}, "
          f"механизм А={tcfg.ENABLE_SIZE_SCORING}, "
          f"теневые={tcfg.ENABLE_SHADOW_TRACKS}")
    print(f"функций объявлено {len(declared)}, исполняется {len(live)}, "
          f"мёртвых {len(dead)}\n")
    print("НЕ ИСПОЛНЯЕТСЯ В БОЕВОЙ КОНФИГУРАЦИИ "
          "(чинить здесь — чинить не то, что работает):")
    cur = None
    for m, qual, line in dead:
        if m != cur:
            print(f"  {m}:")
            cur = m
        print(f"    {qual}  (строка {line})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
