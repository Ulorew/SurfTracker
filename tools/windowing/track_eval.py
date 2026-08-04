#!/usr/bin/env python3
"""Метрики прогона трекера (тикет "трекинг", п.5-6) — из JSONL-лога
track_run.py + опционально ручная разметка (X-AnyLabeling json, строго
2fps, рядом с кадрами — там, где уже размечено).

Без разметки считает всё, что не требует истины (миссы/потери/повтор.
захваты/запас — целиком из лога); "доля тактов с целью в окне" остаётся
null. Число подмен цели — ВСЕГДА null, тикет требует считать его руками по
визуализации (см. докстринг модуля трекинга).

    python track_eval.py --log run.jsonl --frames-dir DIR [--out metrics.json]
"""
import argparse
import json
import math
import os
import statistics

import config
import tracking_config as tcfg


def _load_labeled_shapes(json_path):
    """Rectangle-шейпы кадра с их group_id. dataset_gen.load_frame_boxes
    координаты берёт из тех же points, но group_id не сохраняет — здесь он
    нужен, чтобы отличать трекуемую цель от прочих сёрферов в кадре
    (разметка часто многобоксовая — не одна цель на кадр, см. тикет-чат)."""
    d = json.load(open(json_path))
    out = []
    for s in d["shapes"]:
        if s["shape_type"] != "rectangle":
            continue
        (x1, y1), (x2, y2) = s["points"][0], s["points"][2]
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        if max(x2 - x1, y2 - y1) < config.MIN_TARGET_SIZE:
            continue  # ignore-зона, тот же порог, что везде в проекте
        out.append({"cx": (x1 + x2) / 2.0, "cy": (y1 + y2) / 2.0,
                    "w": x2 - x1, "h": y2 - y1, "group_id": s.get("group_id")})
    return out


def load_gt_candidates(frames_dir):
    """[(timestamp_sec, frame_name, [shape, ...]), ...] по всем размеченным
    кадрам папки, где есть хотя бы один windsurf-бокс — БЕЗ выбора цели
    среди нескольких (см. resolve_gt_track)."""
    manifest = json.load(open(os.path.join(frames_dir, "manifest.json")))
    out = []
    for rec in manifest["frames"]:
        json_path = os.path.join(frames_dir, os.path.splitext(rec["name"])[0] + ".json")
        if not os.path.exists(json_path):
            continue
        shapes = _load_labeled_shapes(json_path)
        if shapes:
            out.append((rec["timestamp_sec"], rec["name"], shapes))
    out.sort(key=lambda r: r[0])
    return out


def resolve_gt_track(candidates, group_id=None, manual_first_pick_index=None):
    """-> ([(timestamp_sec,cx,cy,w,h), ...], warnings).

    group_id задан -> берём шейп с этим group_id на каждом кадре (кадры без
    него в разметке — пропускаются, не считаем промахом разметки).

    Иначе — manual_first_pick_index (индекс бокса на ПЕРВОМ кадре из
    candidates) стартует цепочку: дальше на каждом следующем кадре берём
    бокс, ближайший к позиции на предыдущем УЖЕ РАЗРЕШЁННОМ кадре — то же
    рассуждение, что select_target в самом трекере, только к прошлой GT-
    позиции, не к предсказанию фильтра. warnings — кадры со скачком центра
    заметно больше типичного (>3x медианы или >30px) — вероятно, цепочка
    перескочила на другого сёрфера при пересечении, нужна ручная проверка.
    """
    if group_id is not None:
        track = []
        for t, name, shapes in candidates:
            match = next((s for s in shapes if s["group_id"] == group_id), None)
            if match:
                track.append((t, match["cx"], match["cy"], match["w"], match["h"]))
        return track, []

    if manual_first_pick_index is None:
        raise ValueError("нужен либо group_id, либо manual_first_pick_index — цель неоднозначна")

    track, warnings, jumps = [], [], []
    prev = None
    prev_size = None
    for i, (t, name, shapes) in enumerate(candidates):
        if i == 0:
            chosen = shapes[manual_first_pick_index]
        else:
            chosen = min(shapes, key=lambda s: (s["cx"] - prev[0]) ** 2 + (s["cy"] - prev[1]) ** 2)
            d = ((chosen["cx"] - prev[0]) ** 2 + (chosen["cy"] - prev[1]) ** 2) ** 0.5
            # Отсечка: если ближайший бокс всё равно неправдоподобно далеко,
            # цель на этом кадре просто НЕ размечена — рвём цепочку, а не
            # перепрыгиваем на соседа. Без отсечки пропуск цели в разметке
            # молча превращает истину в траекторию другого сёрфера, и метрика
            # начинает мерить не то (реальный случай: в Primbee t115-125
            # дальний сёрфер размечен не на всех кадрах).
            size = max(chosen["w"], chosen["h"])
            ratio = size / max(prev_size, 1.0)
            too_far = d > MAX_GT_CHAIN_JUMP_FRAC * max(prev_size, 1.0)
            too_different = ratio > MAX_GT_CHAIN_SIZE_RATIO or ratio < 1.0 / MAX_GT_CHAIN_SIZE_RATIO
            if too_far or too_different:
                warnings.append((name, d))
                continue
            jumps.append((name, d))
        track.append((t, chosen["cx"], chosen["cy"], chosen["w"], chosen["h"]))
        prev = (chosen["cx"], chosen["cy"])
        prev_size = max(chosen["w"], chosen["h"])

    if jumps:
        med = statistics.median(d for _, d in jumps)
        thresh = max(med * 3, 30.0)
        warnings = [(name, d) for name, d in jumps if d > thresh]
    return track, warnings


def load_gt_track(frames_dir, group_id=None, manual_first_pick_index=None):
    """Удобная обёртка: если group_id не передан явно, но в разметке папки
    он используется единообразно (все непустые group_id одинаковы) — берём
    его сам. Иначе требует manual_first_pick_index."""
    candidates = load_gt_candidates(frames_dir)
    if not candidates:
        return []
    # Явно указанный индекс цели ВЫИГРЫВАЕТ у автоопределения group_id:
    # иначе им невозможно переопределить случай, когда группа проставлена,
    # но не на том сёрфере (реальный случай в Primbee t115-125).
    if group_id is None and manual_first_pick_index is None:
        seen = {s["group_id"] for _, _, shapes in candidates for s in shapes if s["group_id"] is not None}
        if len(seen) == 1:
            group_id = next(iter(seen))
    track, warnings = resolve_gt_track(candidates, group_id=group_id,
                                        manual_first_pick_index=manual_first_pick_index)
    for name, d in warnings:
        print(f"WARNING: {frames_dir}: подозрительный скачок GT-цепочки на {name} ({d:.0f}px)")
    return track


def _hermite_1d(p1: float, p2: float, m1: float, m2: float, u: float) -> float:
    h00 = 2 * u**3 - 3 * u**2 + 1
    h10 = u**3 - 2 * u**2 + u
    h01 = -2 * u**3 + 3 * u**2
    h11 = u**3 - u**2
    return h00 * p1 + h10 * m1 + h01 * p2 + h11 * m2


def _catmull_rom_1d(t0, p0, t1: float, p1: float, t2: float, p2: float, t3, p3, t: float) -> float:
    """p(t) в [t1,t2] по опорным (t0,p0)..(t3,p3) — неравномерный
    (по реальным таймстампам, не по номеру точки) Catmull-Rom/кардинальный
    сплайн через касательные Эрмита. t0/p0 или t3/p3 = None у границы
    последовательности — касательная там оценивается односторонне (секущая
    p2-p1), что ВЫРОЖДАЕТСЯ в обычную линейную интерполяцию, если опорных
    точек всего 2 (тикет: "лучше интерполяцию посильнее" — сплайн вместо
    линейной для 3+ соседних рамок, срезание угла на манёврах меньше)."""
    dt = t2 - t1
    if dt <= 0:
        return p1
    m1 = (p2 - p0) / (t2 - t0) * dt if t0 is not None else (p2 - p1)
    m2 = (p3 - p1) / (t3 - t1) * dt if t3 is not None else (p2 - p1)
    u = (t - t1) / dt
    return _hermite_1d(p1, p2, m1, m2, u)


def gt_box_at(gt_track, t):
    """Сплайн (неравномерный Catmull-Rom по таймстампам) между соседними
    опорными рамками (тикет "Материал и истина"). None вне диапазона
    разметки — не экстраполируем. При ровно 2 соседних опорных точках без
    дальних соседей ведёт себя как линейная интерполяция (см. _catmull_rom_1d)."""
    if not gt_track or t < gt_track[0][0] or t > gt_track[-1][0]:
        return None
    if t == gt_track[0][0]:
        return gt_track[0][1:]
    n = len(gt_track)
    for i in range(n - 1):
        t1, *p1 = gt_track[i]
        t2, *p2 = gt_track[i + 1]
        if not (t1 <= t <= t2):
            continue
        if t2 - t1 > tcfg.GT_MAX_GAP_SEC:
            return None  # дыра в разметке: истины здесь нет, а не «плавно между»
        t0, p0 = (gt_track[i - 1][0], gt_track[i - 1][1:]) if i - 1 >= 0 else (None, None)
        t3, p3 = (gt_track[i + 2][0], gt_track[i + 2][1:]) if i + 2 < n else (None, None)
        out = []
        for k in range(4):  # cx, cy, w, h — каждая координата отдельно
            v0 = p0[k] if p0 is not None else None
            v3 = p3[k] if p3 is not None else None
            out.append(_catmull_rom_1d(t0, v0, t1, p1[k], t2, p2[k], t3, v3, t))
        return tuple(out)
    return None


def is_hit(pred_cx, pred_cy, gt_box, scale=tcfg.GT_HIT_BOX_SCALE):
    """Критерий попадания ПОосевой: центр внутри опорной рамки *scale.

    ВНИМАНИЕ, расхождение в тикете. Тикет задаёт критерий двумя способами:
    "центр предсказания внутри опорной рамки x1.2" и "(эквивалент:
    расстояние центров < ~0.6 размера рамки)". Эквивалентны они только для
    КВАДРАТНОЙ рамки. Рамка паруса вытянутая (w/h ~ 0.13-0.5), и на реальных
    данных две формулировки дают разные ответы (напр. 0.85 против 1.00).
    Поэтому считаются ОБЕ (см. is_hit_radial), выбор оставлен за человеком.
    """
    cx, cy, w, h = gt_box
    hw, hh = (w * scale) / 2.0, (h * scale) / 2.0
    return (cx - hw <= pred_cx <= cx + hw) and (cy - hh <= pred_cy <= cy + hh)


def is_hit_radial(pred_cx, pred_cy, gt_box, frac=tcfg.GT_HIT_RADIAL_FRAC):
    """Вторая формулировка того же критерия из тикета: расстояние центров
    меньше frac от РАЗМЕРА рамки (размер = max(w,h), как везде в проекте)."""
    cx, cy, w, h = gt_box
    return math.hypot(pred_cx - cx, pred_cy - cy) <= frac * max(w, h)


# Во сколько размеров цели может уехать бокс между соседними опорными
# кадрами, прежде чем считать, что это уже ДРУГОЙ объект, а не та же цель.
MAX_GT_CHAIN_JUMP_FRAC = 3.0
# И во сколько раз может измениться его размер за шаг. Одного расстояния мало:
# мелкая дальняя цель и крупная ближняя могут оказаться рядом в кадре, и
# цепочка перепрыгнет, оставаясь в пределах допуска по расстоянию. Скачок
# размера (30px -> 454px за шаг) выдаёт подмену однозначно.
MAX_GT_CHAIN_SIZE_RATIO = 2.0


def _chosen_center(chosen):
    return ((chosen[0] + chosen[2]) / 2.0, (chosen[1] + chosen[3]) / 2.0)


def count_swap_runs(on_target_flags, min_run=2):
    """Число серий подряд идущих тактов "взяли не ту цель" длиной >= min_run.

    Это автоматический счётчик подмены цели — главного отказа по тикету п.5,
    который там предлагалось считать руками по визуализации. Одиночный
    промах не считаем подменой: это чаще дрожание рамки, а не уехавший
    захват.
    """
    runs, cur = 0, 0
    for ok in on_target_flags:
        if ok is False:
            cur += 1
        else:
            if cur >= min_run:
                runs += 1
            cur = 0
    if cur >= min_run:
        runs += 1
    return runs


OUTCOME_CLEAN = "чисто"
OUTCOME_SWAP = "подмена"
OUTCOME_LOST = "потеря"

# Допуск на выпадение из критерия с возвратом к ИСХОДНОЙ цели (тикет "подмены
# v2", п.1). Одиночное-двойное выпадение — дрожание рамки, а не уехавший
# захват.
CLEAN_TOLERANCE_TICKS = 2
# Сколько подряд тактов стабильного ВЕДЕНИЯ мимо цели считать подменой.
SWAP_MIN_RUN = 3


def classify_episode(rows, gt_track):
    """Исход эпизода по клипу (тикет "подмены v2", п.1).

    -> (исход, такт первого расхождения | None, флаги по тактам)

    Такты без истины (дыра в разметке) в классификации не участвуют: по ним
    нельзя сказать, ту цель ведём или нет.
    """
    flags = []          # (индекс такта, on_target|None) — None = промах
    for i, r in enumerate(rows):
        gt = gt_box_at(gt_track, r["timestamp_sec"]) if gt_track else None
        if gt is None:
            continue
        if r["chosen"] is None:
            flags.append((i, None))
        else:
            ccx, ccy = _chosen_center(r["chosen"])
            flags.append((i, is_hit_radial(ccx, ccy, gt)))

    first_div = next((i for i, ok in flags if ok is False), None)

    # потеря: был переход в lost и после него ни разу не вернулись на цель
    lost_idx = next((i for i, r in enumerate(rows) if r["lost_transition"]), None)
    if lost_idx is not None:
        recovered = any(ok for i, ok in flags if i > lost_idx and ok)
        if not recovered:
            return OUTCOME_LOST, first_div, flags

    # подмена: SWAP_MIN_RUN подряд тактов ВЕДЕНИЯ (chosen != None) мимо цели.
    # Обратный перескок исход не улучшает — поэтому просто ищем такой прогон
    # где угодно по клипу.
    run = 0
    for _, ok in flags:
        if ok is False:
            run += 1
            if run >= SWAP_MIN_RUN:
                return OUTCOME_SWAP, first_div, flags
        elif ok is True:
            run = 0
        # ok is None (промах) прогон не удлиняет, но и не сбрасывает: цель
        # просто не видна, ведение чужой цели этим тактом не подтверждается

    # чисто: выпадения не длиннее допуска и с возвратом на исходную цель
    return OUTCOME_CLEAN, first_div, flags


def miss_streaks(rows):
    """Длины серий подряд идущих тактов с chosen=None (промах, включая
    такты уже в lost, пока не случится повторный захват)."""
    streaks = []
    cur = 0
    for r in rows:
        if r["chosen"] is None:
            cur += 1
        else:
            if cur > 0:
                streaks.append(cur)
            cur = 0
    if cur > 0:
        streaks.append(cur)
    return streaks


def compute_metrics(rows, gt_track):
    n_ticks = len(rows)
    n_losses = sum(1 for r in rows if r["lost_transition"])
    n_reacq = sum(1 for r in rows if r["reacquired"])
    streaks = miss_streaks(rows)

    margins = []
    for r in rows:
        if r["chosen"] is not None and r["window_side"] > 0:
            margins.append(r["chosen_dist"] / r["window_side"])

    # Попадание ПРЕДСКАЗАНИЯ (метрика тикета) — по обеим его формулировкам,
    # они неэквивалентны на вытянутой рамке паруса (см. is_hit).
    pred_axis, pred_radial = [], []
    # Попадание ВЫБРАННОЙ ДЕТЕКЦИИ — "ведём ли мы вообще ту цель". Отделено
    # от предсказания намеренно: на первом такте после захвата скорость ещё
    # нулевая, предсказание закономерно отстаёт, но захват при этом может
    # стоять ровно на цели — это разные отказы и лечатся они по-разному.
    on_target_flags = []
    if gt_track:
        for r in rows:
            gt = gt_box_at(gt_track, r["timestamp_sec"])
            if gt is None:
                continue
            pred_axis.append(is_hit(r["predicted_cx"], r["predicted_cy"], gt))
            pred_radial.append(is_hit_radial(r["predicted_cx"], r["predicted_cy"], gt))
            if r["chosen"] is None:
                on_target_flags.append(None)  # промах — не подмена, считается отдельно
            else:
                ccx, ccy = _chosen_center(r["chosen"])
                on_target_flags.append(is_hit_radial(ccx, ccy, gt))

    def pctl(vals, p):
        if not vals:
            return None
        vals = sorted(vals)
        i = min(len(vals) - 1, max(0, round(p / 100.0 * (len(vals) - 1))))
        return vals[i]

    graded = [f for f in on_target_flags if f is not None]

    return {
        "n_ticks": n_ticks,
        "n_gt_ticks": len(pred_axis),
        # основная метрика тикета, обе формулировки критерия
        "in_window_fraction": (sum(pred_axis) / len(pred_axis)) if pred_axis else None,
        "in_window_fraction_radial": (sum(pred_radial) / len(pred_radial)) if pred_radial else None,
        # ведём ли ту цель вообще (подмена)
        "on_target_fraction": (sum(graded) / len(graded)) if graded else None,
        "n_target_swaps_auto": count_swap_runs(on_target_flags) if graded else None,
        "n_target_swaps": None,  # ручной счётчик по визуализации, тикет п.5
        "miss_streak_median": statistics.median(streaks) if streaks else 0,
        "miss_streak_max": max(streaks) if streaks else 0,
        "n_losses": n_losses,
        "n_reacquisitions": n_reacq,
        "margin_frac_p50": pctl(margins, 50),
        "margin_frac_p95": pctl(margins, 95),
        "n_margin_samples": len(margins),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True, help="JSONL от track_run.py")
    ap.add_argument("--frames-dir", required=True, help="та же папка, что была у track_run.py")
    ap.add_argument("--out", default=None)
    ap.add_argument("--gt-group-id", type=int, default=None,
                     help="явный group_id трекуемой цели (по умолчанию — автоопределение, "
                          "если в разметке один непустой group_id на всю папку)")
    ap.add_argument("--gt-first-pick", type=int, default=None,
                     help="индекс бокса трекуемой цели на ПЕРВОМ размеченном кадре — нужен, "
                          "если group_id не проставлен и не единственный; дальше цель "
                          "разрешается цепочкой по ближайшему боксу")
    args = ap.parse_args()

    rows = [json.loads(line) for line in open(args.log) if line.strip()]
    gt_track = load_gt_track(args.frames_dir, group_id=args.gt_group_id,
                              manual_first_pick_index=args.gt_first_pick)

    m = compute_metrics(rows, gt_track)
    m["source"] = os.path.basename(args.frames_dir.rstrip("/"))
    m["n_gt_labeled_frames"] = len(gt_track)

    print(json.dumps(m, indent=2, ensure_ascii=False))
    if not gt_track:
        print("\n(разметки в этой папке нет — in_window_fraction недоступна, "
              "остальное посчитано из лога)")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(m, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
