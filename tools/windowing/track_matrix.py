#!/usr/bin/env python3
"""Матрица конфигураций трекинга.

Гоняет один и тот же набор проездов через несколько конфигураций петли и
сводит метрики в таблицу "проезд x конфигурация". Детектор во всех строках
ОДИН — сравниваются конфигурации петли, а не модели; веса и их хеш пишутся в
сводку, иначе строки нельзя сопоставить между запусками матрицы.

    python track_matrix.py --weights best.pt --out-dir output/matrix_night \\
        [--tick-hz 3.0 4.0 6.0] [--configs base_A A_vdir ...]

A+Б в матрицу не входит намеренно: механизм Б был реализован и проверен
после того, как строки с ним уже гонялись (см. docs/journal/РЕВИЗИЯ_ДЕФЕКТОВ.md),
его вклад уже измерен и переизмерять его нечем.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FRAMES_ROOT = os.path.join(HERE, "..", "..", "Data", "frames", "val_manual")

# Ручной выбор цели там, где group_id разметки указывает не на неё
# (см. track_run.py --gt-first-pick). Проверено на всех восьми проездах:
# load_gt_track разбирает каждый без подсказки, поэтому словарь пуст. Если
# появится новый проезд с многобоксовой разметкой без единого group_id —
# запись сюда, иначе прогон свалится на затравку по уверенности модели.
GT_FIRST_PICK = {}

CONFIGS = {
    # имя: флаги track_run.py
    "base_A":            ["--enable-a"],
    "A_vdir":            ["--enable-a", "--enable-vdir"],
    "A_kalman":          ["--enable-a", "--filter-level", "2"],
    "A_kalman_gate":     ["--enable-a", "--filter-level", "2", "--enable-gate"],
    # Гейт ДО правок 18.08 — строка сравнения, а не кандидат в прод. Без неё
    # «стало лучше» опиралось бы на память, а не на число.
    "A_kalman_gate_legacy": ["--enable-a", "--filter-level", "2", "--enable-gate",
                              "--gate-legacy"],
    # Радиус приёма ДО правки 18.08: прижат потолком кадра вместе с окном.
    "A_kalman_gate_radlegacy": ["--enable-a", "--filter-level", "2", "--enable-gate",
                                 "--radius-legacy"],
    "A_kalman_gate_vdir": ["--enable-a", "--filter-level", "2", "--enable-gate",
                            "--enable-vdir"],
    # Финалист серии "счёт кандидата": форма 2 (анизотропный махаланобис) +
    # теневые треки + демпфирование экстраполяции. Форма выбрана стендом, а не
    # клипами: восемь исходов переобучаются мгновенно.
    "finalist": ["--enable-a", "--filter-level", "2", "--enable-gate",
                  "--score-form", "maha_aniso", "--shadows"],
    # ПРОД-конфигурация (заморозка): выбор цели базовый,
    # демпфирование, теневые треки. Механизм А и Калман сюда не входят —
    # в постановке их нет; строки ниже показывают, что они добавляют.
    "prod": ["--shadows"],
    "prod_A": ["--shadows", "--enable-a"],
    "prod_kalman": ["--shadows", "--filter-level", "2", "--enable-gate"],
    # Разложение финалиста: что даёт каждая часть по отдельности.
    "finalist_no_shadows": ["--enable-a", "--filter-level", "2", "--enable-gate",
                             "--score-form", "maha_aniso"],
    "shadows_only": ["--enable-a", "--filter-level", "2", "--enable-gate", "--shadows"],
    # ЗАМОРОЖЕННЫЙ прод-состав. Флаги перечислены полностью, хотя часть из них
    # теперь и так умолчания: строка матрицы обязана задавать конфигурацию
    # целиком, иначе смена умолчаний молча меняет смысл прежних строк.
    "prod_frozen": ["--enable-a", "--filter-level", "2", "--enable-gate", "--shadows",
                     "--score-form", "distance"],
    # Тот же прод-состав БЕЗ механизма А — проверка предложения выключить А.
    # Отличается ровно одним флагом, чтобы разницу нельзя было списать ни на
    # что другое.
    "prod_noA": ["--filter-level", "2", "--enable-gate", "--shadows",
                  "--score-form", "distance"],
    # Прод-состав, где от механизма А оставлено только ВЕТО по размеру, а
    # размерное слагаемое счёта обнулено: на стенде вето несёт всю пользу
    # (0.928 против 0.921 у полного А), а слагаемое домножается на сторону
    # окна и забивает позиционный член.
    "prod_veto_only": ["--enable-a", "--size-lambda", "0", "--filter-level", "2",
                        "--enable-gate", "--shadows", "--score-form", "distance"],
    # Пересчёт на честной линейке (после валидации). Каждая строка отличается
    # от prod_frozen ровно одним решением.
    "prod_veto15": ["--enable-a", "--size-lambda", "0", "--size-veto-ratio", "1.5",
                     "--filter-level", "2", "--enable-gate", "--shadows",
                     "--score-form", "distance"],
    "prod_no_shadows": ["--enable-a", "--filter-level", "2", "--enable-gate",
                         "--score-form", "distance"],
    "prod_birthguard": ["--enable-a", "--filter-level", "2", "--enable-gate",
                         "--shadows", "--shadow-birth-needs-pick",
                         "--score-form", "distance"],
    "prod_birthguard_noA": ["--filter-level", "2", "--enable-gate", "--shadows",
                             "--shadow-birth-needs-pick", "--score-form", "distance"],
    "prod_maha": ["--enable-a", "--filter-level", "2", "--enable-gate", "--shadows",
                   "--score-form", "maha"],
    # Ограничение убеждения полем зрения. На
    # ютубном проходе центр окна уходил за кадр на 68% тактов и не
    # возвращался; на клипах он не уходит НИ РАЗУ — но прижатие "вырезка
    # целиком в кадре" срабатывало бы здесь на 34% тактов. Значит правка на
    # клипах не нейтральна, и её обязан проверить единственный набор с
    # разметкой. Строки отличаются от prod_no_shadows ровно одним флагом.
    "clamp_off": ["--enable-a", "--filter-level", "2", "--enable-gate",
                   "--score-form", "distance", "--view-clamp", "off"],
    "clamp_frame": ["--enable-a", "--filter-level", "2", "--enable-gate",
                     "--score-form", "distance", "--view-clamp", "frame"],
    "clamp_window": ["--enable-a", "--filter-level", "2", "--enable-gate",
                      "--score-form", "distance", "--view-clamp", "window"],
}

# Метрики, попадающие в таблицу. Радиальный критерий — основной
# ("в отчёте только радиальный"), осевой остаётся в json прогона.
# Честная линейка: знаменатель всегда все GT-такты, отказ и подмена —
# РАЗНЫЕ колонки (для камеры это противоположные исходы: отказ — стоит на
# месте, подмена — уехала за чужим).
METRICS = ["on_target_all", "swap_all", "refuse_all", "lead_fraction",
           "in_window_fraction_radial", "n_losses", "n_reacquisitions",
           "miss_streak_max", "margin_frac_p50"]


def clips():
    return sorted(d for d in os.listdir(FRAMES_ROOT)
                  if os.path.isdir(os.path.join(FRAMES_ROOT, d)))


def run_one(py, weights, clip, cfg_name, tick_hz, out_dir, imgsz):
    tag = f"{cfg_name}__{tick_hz:g}hz__{clip}"
    log = os.path.join(out_dir, tag + ".jsonl")
    mp4 = os.path.join(out_dir, tag + ".mp4")
    metrics = os.path.join(out_dir, tag + ".metrics.json")
    frames = os.path.join(FRAMES_ROOT, clip)

    cmd = [py, os.path.join(HERE, "track_run.py"), "--frames-dir", frames,
           "--weights", weights, "--tick-hz", str(tick_hz), "--imgsz", str(imgsz),
           "--out", mp4, "--log-out", log] + CONFIGS[cfg_name]
    if clip in GT_FIRST_PICK:
        cmd += ["--gt-first-pick", str(GT_FIRST_PICK[clip])]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip().splitlines()[-1:] or ["?"]}

    cmd = [py, os.path.join(HERE, "track_eval.py"), "--log", log,
           "--frames-dir", frames, "--out", metrics]
    if clip in GT_FIRST_PICK:
        cmd += ["--gt-first-pick", str(GT_FIRST_PICK[clip])]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip().splitlines()[-1:] or ["?"]}

    out = json.load(open(metrics))
    rows = [json.loads(l) for l in open(log)]
    # Сколько кандидатов отсеял бы каждый способ отбора — нужно
    # сравнить махаланобисов гейт с фиксированным радиусом.
    gate_rows = [r for r in rows if r.get("n_candidates_gate") is not None]
    out["n_ticks"] = len(rows)
    out["cand_by_radius"] = sum(r["n_candidates_radius"] for r in gate_rows) or None
    out["cand_by_gate"] = sum(r["n_candidates_gate"] for r in gate_rows) or None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--tick-hz", type=float, nargs="+", default=[3.0])
    ap.add_argument("--configs", nargs="+", default=list(CONFIGS))
    ap.add_argument("--clips", nargs="+", default=None)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    todo = args.clips or clips()
    summary = {"weights": os.path.abspath(args.weights), "clips": todo,
               "configs": {c: CONFIGS[c] for c in args.configs},
               "tick_hz": args.tick_hz, "results": {}}

    total = len(args.configs) * len(args.tick_hz) * len(todo)
    done = 0
    for hz in args.tick_hz:
        for cfg_name in args.configs:
            for clip in todo:
                done += 1
                key = f"{cfg_name}|{hz:g}|{clip}"
                res = run_one(args.python, args.weights, clip, cfg_name, hz,
                               args.out_dir, args.imgsz)
                summary["results"][key] = res
                if "error" in res:
                    flag = "ОШИБКА"
                else:
                    flag = (f"на цели {res.get('on_target_all')}"
                            f"  подмена {res.get('swap_all')}"
                            f"  отказ {res.get('refuse_all')}")
                print(f"[{done}/{total}] {key}: {flag}", flush=True)
                # пишем на каждом шаге: прогон длинный, обрыв не должен
                # стоить всей уже сделанной работы
                with open(os.path.join(args.out_dir, "summary.json"), "w") as f:
                    json.dump(summary, f, indent=2, ensure_ascii=False)

    print("готово:", os.path.join(args.out_dir, "summary.json"))


if __name__ == "__main__":
    main()
