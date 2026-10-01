#!/usr/bin/env python3
"""Сводка матрицы трекинга в markdown.

    python track_matrix_report.py output/matrix_night/summary.json [...] > table.md

Основная метрика — доля тактов на цели по РАДИАЛЬНОМУ критерию
("в отчёте только радиальный"). Осевой остаётся в json прогонов: он неверен на
вытянутой рамке паруса, и держать оба в таблице значит предлагать выбрать
тот, который больше нравится.
"""
import json
import sys

SHORT = {
    "VID_20230624_145515_t58-65": "тлф-1 t58",
    "VID_20250822_163723_t27-32": "тлф-2 t27",
    "YT_Primbee_Speed_Windsurfing_8bYtDBZkrpM_t115-125": "Primbee t115",
    "YT_Primbee_Speed_Windsurfing_8bYtDBZkrpM_t321-325": "Primbee t321",
    "YT_best_windsurf_racing_bp6nX64OeI0_t159-166": "racing t159",
    "YT_best_windsurf_racing_bp6nX64OeI0_t322-332": "racing t322",
    "YT_best_windsurf_racing_bp6nX64OeI0_t415-418": "racing t415",
    "YT_best_windsurf_racing_bp6nX64OeI0_t585-595": "racing t585",
}


def load(paths):
    results, meta = {}, None
    for p in paths:
        d = json.load(open(p))
        meta = meta or d
        results.update(d["results"])
    return meta, results


def cell(res):
    """Ячейка честной линейки: на цели / подмена / отказ, знаменатель один.

    Три числа, а не одно: конфигурация, которая перестала вести цель,
    прежней метрикой получала за это премию (racing t322 входил как 1.000,
    посчитанная по 6 тактам из 30). Отказ и подмена для камеры —
    противоположные исходы, и в одной колонке им не место.
    """
    if res is None or "error" in res:
        return "—"
    v = res.get("on_target_all")
    if v is None:
        return "нет истины"
    return f"{v:.2f} / {res.get('swap_all', 0):.2f} / {res.get('refuse_all', 0):.2f}"


def main():
    meta, results = load(sys.argv[1:])
    configs = list(meta["configs"])
    clips = meta["clips"]
    hzs = sorted({float(k.split("|")[1]) for k in results})

    print(f"Веса: `{meta['weights'].split('/')[-1]}` (один детектор во всех строках)\n")
    print("В ячейке: **на цели / подмена / отказ** — доли ВСЕХ размеченных "
          "тактов (сумма = 1). Радиальный критерий.\n")
    print("Отказ и подмена разведены намеренно: для камеры это противоположные "
          "исходы. Отказ — камера стоит, цель, скорее всего, ещё в кадре. "
          "Подмена — камера уехала за чужим.\n")

    for hz in hzs:
        print(f"\n### Такт {hz:g} Гц\n")
        print("| проезд | " + " | ".join(configs) + " |")
        print("|---" * (len(configs) + 1) + "|")
        for clip in clips:
            row = [SHORT.get(clip, clip)]
            for c in configs:
                row.append(cell(results.get(f"{c}|{hz:g}|{clip}")))
            print("| " + " | ".join(row) + " |")

        avg = ["**среднее на цели**"]
        for c in configs:
            vals = [results[f"{c}|{hz:g}|{clip}"].get("on_target_all")
                    for clip in clips
                    if f"{c}|{hz:g}|{clip}" in results
                    and "error" not in results[f"{c}|{hz:g}|{clip}"]]
            vals = [v for v in vals if v is not None]
            avg.append(f"**{sum(vals) / len(vals):.3f}**" if vals else "—")
        print("| " + " | ".join(avg) + " |")

        for label, key in (("**среднее подмена**", "swap_all"),
                            ("**среднее отказ**", "refuse_all"),
                            ("**среднее ведение**", "lead_fraction")):
            row = [label]
            for c in configs:
                vals = [results[f"{c}|{hz:g}|{clip}"].get(key) for clip in clips
                        if f"{c}|{hz:g}|{clip}" in results
                        and "error" not in results[f"{c}|{hz:g}|{clip}"]]
                vals = [v for v in vals if v is not None]
                row.append(f"**{sum(vals) / len(vals):.3f}**" if vals else "—")
            print("| " + " | ".join(row) + " |")

        tot = ["тактов: на цели / подмена / отказ"]
        for c in configs:
            rs = [results[k] for k in results
                  if k.startswith(f"{c}|{hz:g}|") and "error" not in results[k]]
            tot.append(f"{sum(r.get('n_hit', 0) or 0 for r in rs)} / "
                       f"{sum(r.get('n_swap', 0) or 0 for r in rs)} / "
                       f"{sum(r.get('n_refuse', 0) or 0 for r in rs)}")
        print("| " + " | ".join(tot) + " |")

    # Гейт против фиксированного радиуса
    gate_rows = [(k, v) for k, v in results.items()
                 if isinstance(v, dict) and v.get("cand_by_gate") is not None]
    if gate_rows:
        print("\n### Махаланобисов гейт против фиксированного радиуса\n")
        print("Отрицательное \"отсечено\" означает, что гейт оказался ШИРЕ радиуса: "
              "после затравки и после пропусков ковариация велика, и он "
              "принимает то, что фиксированный радиус отверг бы.\n")
        print("| конфигурация | такт | кандидатов радиусом | кандидатов гейтом | отсечено |")
        print("|---|---|---|---|---|")
        agg = {}
        for k, v in gate_rows:
            c, hz, _ = k.split("|")
            a = agg.setdefault((c, hz), [0, 0])
            a[0] += v["cand_by_radius"] or 0
            a[1] += v["cand_by_gate"] or 0
        for (c, hz), (rad, gate) in sorted(agg.items()):
            cut = (1 - gate / rad) * 100 if rad else 0
            print(f"| {c} | {hz} Гц | {rad} | {gate} | {rad - gate:+d} ({cut:+.0f}%) |")


if __name__ == "__main__":
    main()
