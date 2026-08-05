#!/usr/bin/env python3
"""Синтетический стенд петли слежения (тикет "счёт кандидата, теневые треки,
синтетический стенд", п.3).

N точечных целей с заданными траекториями В УГЛАХ, «детекции» = положение +
гауссов шум + размер + уверенность, подаются в петлю напрямую — без видео,
без модели, без разметки. Смысл: восемь размеченных проездов переобучаются
мгновенно, отличить +0.04 от нуля на них нельзя, а здесь сценариев можно
сделать сколько нужно, и в каждом ИЗВЕСТНО, кто был целью.

Воспроизводимость: всё от seed. Сценарий — это код и параметры, а не
записанные данные: записанные пришлось бы перегенерировать при каждом
изменении формата детекции, и они бы незаметно устарели.

    python track_bench.py --forms maha maha_aniso maha_vdir --runs 600
    python track_bench.py --sensitivity 0.6 0.7 0.85

Что стенд ЭМУЛИРУЕТ, а что нет. Эмулируется окно: петля видит только те
детекции, чей центр попал в текущее окно слежения, — иначе она получала бы
кадр целиком, чего на видео не бывает никогда. НЕ эмулируются: пропуски
детектора по вине модели (задаются сценарием явно), форма рамки (цели
точечные, рамка квадратная), фон.
"""

import argparse
import json
import math
import random
import statistics as st
import sys
import types

import angles as ang
import tracking_config as base_cfg
from track_logic import STATUS_TRACKING, TrackState
from track_score import ALL_FORMS

# «Камера» стенда — те же числа, что у дронового клипа: угловые пределы окна
# должны быть теми же, что в реальном прогоне, иначе стенд меряет другую петлю.
INTR = ang.from_fov(1920, 1080, 75.0, "стенд")
MIN_WINDOW = ang.px_size_to_angle(640, INTR)
MAX_WINDOW = ang.px_size_to_angle(1080, INTR)


def cfg_with(**overrides):
    d = {k: getattr(base_cfg, k) for k in dir(base_cfg) if k.isupper()}
    d.update(overrides)
    return types.SimpleNamespace(**d)


class Target:
    """Точечная цель с прямолинейным движением в углах.

    Прямая, а не произвольная траектория: манёвр цели уже заложен в шум
    процесса фильтра, и добавлять его ещё и в стенд значило бы проверять
    фильтр на данных, под которые он не строился. Событие «пересечение»
    делается расположением прямых, а не изломом.
    """

    def __init__(self, tid, x0, y0, vx, vy, size, t_from=0.0, t_to=1e9,
                 gap=None, conf=(0.5, 0.95)):
        self.tid = tid
        self.x0, self.y0 = x0, y0
        self.vx, self.vy = vx, vy
        self.size = size
        self.t_from, self.t_to = t_from, t_to
        self.gap = gap          # (t_start, t_end) — окно, где цель не детектируется
        self.conf = conf

    def position(self, t):
        return (self.x0 + self.vx * t, self.y0 + self.vy * t)

    def visible(self, t):
        if not (self.t_from <= t <= self.t_to):
            return False
        if self.gap and self.gap[0] <= t < self.gap[1]:
            return False
        return True


def detect(target, t, rng, cfg):
    """Детекция цели: положение + гауссов шум sigma = 0.3 * углового размера
    (то же R, что предполагает фильтр), размер — с логнормальным шумом того же
    масштаба, что KALMAN_R_LOGH. Иначе стенд мерил бы фильтр на данных,
    которым он не соответствует, и результат говорил бы о рассогласовании
    модели, а не о форме счёта."""
    x, y = target.position(t)
    sigma = cfg.KALMAN_R_POS_SIZE_FRAC * target.size
    x += rng.gauss(0, sigma)
    y += rng.gauss(0, sigma)
    size = target.size * math.exp(rng.gauss(0, cfg.KALMAN_R_LOGH))
    conf = rng.uniform(*target.conf)
    return (x - size / 2, y - size / 2, x + size / 2, y + size / 2, conf, target.tid)


JUNK_TID = -1


def junk_detections(rng, cfg, n, cx, cy, side, size):
    """Мусор детектора: низкая уверенность, случайное место в окне. Должен
    проходить сквозь петлю бесследно — в том числе НЕ рождать теневых."""
    out = []
    for _ in range(n):
        x = cx + rng.uniform(-side / 2, side / 2)
        y = cy + rng.uniform(-side / 2, side / 2)
        s = size * rng.uniform(0.5, 1.5)
        conf = rng.uniform(0.08, cfg.SHADOW_BIRTH_CONF - 0.05)
        out.append((x - s / 2, y - s / 2, x + s / 2, y + s / 2, conf, JUNK_TID))
    return out


class BenchResult:
    def __init__(self, ticks):
        self.ticks = ticks          # [{"t","chosen_tid","status","n_dets",...}]

    @property
    def swap_runs(self):
        """Серии подряд идущих тактов на ЧУЖОЙ цели длиной >= 2.

        Одиночный такт на соседе — это шум измерения, петля возвращается сама;
        подмена — это когда она там осталась. Порог 2 тот же, что в
        track_eval.count_swap_runs, чтобы стенд и клипы мерили одно и то же.
        """
        runs, cur = 0, 0
        for r in self.ticks:
            wrong = r["chosen_tid"] is not None and r["chosen_tid"] != 0
            cur = cur + 1 if wrong else 0
            if cur == 2:
                runs += 1
        return runs

    @property
    def survived(self):
        return self.swap_runs == 0

    @property
    def ends_on_target(self):
        for r in reversed(self.ticks):
            if r["chosen_tid"] is not None:
                return r["chosen_tid"] == 0
        return False

    def on_target_fraction(self):
        graded = [r for r in self.ticks if r["chosen_tid"] is not None]
        if not graded:
            return 0.0
        return sum(1 for r in graded if r["chosen_tid"] == 0) / len(graded)


def run_scenario(targets, cfg, seed, tick_hz=3.0, n_ticks=24, junk_per_tick=0):
    """Прогон одного сценария. targets[0] — ЦЕЛЬ, остальные — соседи."""
    rng = random.Random(seed)
    dt = 1.0 / tick_hz
    tgt = targets[0]
    x0, y0 = tgt.position(0.0)
    state = TrackState(cfg, x0, y0, tgt.size, MIN_WINDOW, MAX_WINDOW)

    ticks = []
    for i in range(1, n_ticks + 1):
        t = i * dt
        cx, cy, side = state.plan_window(dt)
        dets = [detect(tg, t, rng, cfg) for tg in targets if tg.visible(t)]
        if junk_per_tick:
            dets += junk_detections(rng, cfg, junk_per_tick, cx, cy, side, tgt.size)
        # петля видит только то, что попало в ОКНО — как и на видео
        dets = [d for d in dets
                if abs((d[0] + d[2]) / 2 - cx) <= side / 2
                and abs((d[1] + d[3]) / 2 - cy) <= side / 2]
        rng.shuffle(dets)   # порядок детекций не должен влиять на исход
        r = state.step(dt, dets)
        ticks.append({
            "t": t, "status": r.status,
            "chosen_tid": None if r.chosen is None else r.chosen[5],
            "n_dets": len(dets), "n_shadows": r.n_shadows,
            "n_taken": r.n_taken_by_shadows,
            "pred": (r.predicted_cx, r.predicted_cy),
            # оценка скорости фильтра — предел улёта считается от НЕЁ, а не от
            # истинной скорости цели: экстраполирует петля тем, что оценила
            "vel": (state.filter.vx, state.filter.vy),
            # положение фильтра ПОСЛЕ такта — от него и отсчитывается улёт:
            # предсказание того же такта построено до обновления и точкой
            # отсчёта быть не может
            "pos": (state.filter.cx, state.filter.cy),
            "true": tgt.position(t),
        })
    return BenchResult(ticks)


# --- сценарии -------------------------------------------------------------
# Каждый возвращает список целей; targets[0] — та, которую ведём.

def scen_crossing(rng, angle_deg=None, size=None):
    """Пересечение под углом: сосед идёт наперерез и проходит через цель."""
    size = size or rng.uniform(0.02, 0.06)
    speed = rng.uniform(0.05, 0.25)
    a = math.radians(angle_deg if angle_deg is not None else rng.uniform(30, 150))
    t_cross = rng.uniform(2.0, 5.0)
    tgt = Target(0, -speed * t_cross, 0.0, speed, 0.0, size)
    mx, my = tgt.position(t_cross)
    vx, vy = speed * math.cos(a), speed * math.sin(a)
    other = Target(1, mx - vx * t_cross, my - vy * t_cross, vx, vy,
                   size * rng.uniform(0.8, 1.25))
    return [tgt, other]


def scen_overtake(rng):
    """Обгон вплотную: сосед идёт почти параллельно и обгоняет впритирку."""
    size = rng.uniform(0.02, 0.05)
    speed = rng.uniform(0.05, 0.20)
    lateral = size * rng.uniform(0.6, 1.5)
    tgt = Target(0, 0.0, 0.0, speed, 0.0, size)
    other = Target(1, -speed * 2.0, lateral, speed * rng.uniform(1.3, 1.8), 0.0,
                   size * rng.uniform(0.9, 1.1))
    return [tgt, other]


def scen_size_crossing(rng):
    """Пересечение целей РАЗНОГО размера: та же геометрия, что crossing, но
    сосед втрое крупнее. Отличить их можно только по третьей координате
    (log h) — контрольный сценарий для формы 1 против двухкоординатной."""
    tgt, other = scen_crossing(rng, size=rng.uniform(0.015, 0.03))
    other.size = tgt.size * rng.uniform(2.5, 4.0)
    return [tgt, other]


def scen_disappear(rng, k_ticks=4, tick_hz=3.0):
    """Цель пропадает на K тактов (закрыта волной/парусом)."""
    size = rng.uniform(0.02, 0.05)
    speed = rng.uniform(0.08, 0.25)
    t0 = rng.uniform(1.5, 3.0)
    tgt = Target(0, 0.0, 0.0, speed, 0.0, size,
                 gap=(t0, t0 + k_ticks / tick_hz))
    return [tgt]


def scen_crossing_gap(rng, tick_hz=3.0):
    """Пересечение, на котором детектор ТЕРЯЕТ цель на 1-2 такта.

    Это не выдуманное усложнение, а воспроизведение реального отказа с клипов
    (racing t159/t322): подмена случается не тогда, когда сосед просто рядом,
    а тогда, когда цели в этот момент нет, и единственный кандидат в окне —
    сосед. Без пропуска пересечение переживается почти всегда, и стенд ничего
    не различает.
    """
    tgt, other = scen_crossing(rng)
    t_cross = -tgt.x0 / tgt.vx
    k = rng.choice((1, 2))
    tgt.gap = (t_cross - 0.5 / tick_hz, t_cross - 0.5 / tick_hz + k / tick_hz)
    return [tgt, other]


def scen_head_on(rng):
    """Встречное сближение вплотную: сосед идёт НАВСТРЕЧУ и проходит впритирку.

    Ключевой сценарий для сравнения форм 2 и 3. Анизотропная Q симметрична и
    "вперёд" от "назад" не отличает; направленный член — отличает. Если форма
    3 обходит форму 2 именно здесь, значит анизотропия как модель механизма В
    неверна (прямое требование тикета доложить об этом).
    """
    size = rng.uniform(0.02, 0.05)
    speed = rng.uniform(0.08, 0.22)
    lateral = size * rng.uniform(0.3, 1.0)
    tgt = Target(0, 0.0, 0.0, speed, 0.0, size)
    t_meet = rng.uniform(2.0, 4.0)
    mx, _ = tgt.position(t_meet)
    other = Target(1, mx + speed * t_meet, lateral, -speed, 0.0,
                   size * rng.uniform(0.9, 1.1))
    return [tgt, other]


def scen_third_born(rng):
    """Рождение третьего: посреди прогона появляется ещё один сосед."""
    tgt, other = scen_crossing(rng)
    third = Target(2, tgt.x0 + 0.05, 0.08, -0.05, -0.03, tgt.size * 1.1,
                   t_from=rng.uniform(1.0, 2.5))
    return [tgt, other, third]


def scen_junk(rng):
    """Один сосед + мусорные детекции с низкой уверенностью."""
    return scen_crossing(rng)


SCENARIOS = {
    "пересечение": (scen_crossing, {}),
    "пересечение+пропуск": (scen_crossing_gap, {}),
    "встречный": (scen_head_on, {}),
    "обгон": (scen_overtake, {}),
    "разный размер": (scen_size_crossing, {}),
    "исчезновение": (scen_disappear, {}),
    "рождение третьего": (scen_third_born, {}),
    "мусор": (scen_junk, {"junk_per_tick": 3}),
}


def evaluate(form, runs, seed0=0, shadows=True, taken_ratio=None, scenarios=None,
              filter_level=2):
    """-> {сценарий: {"survived": доля, "on_target": средняя доля тактов}}"""
    over = {"SCORE_FORM": form, "FILTER_LEVEL": filter_level,
            "ENABLE_SHADOW_TRACKS": shadows,
            "KALMAN_ANISOTROPIC_Q": form == "maha_aniso"}
    if taken_ratio is not None:
        over["SHADOW_TAKEN_RATIO"] = taken_ratio
    out = {}
    for name in (scenarios or SCENARIOS):
        maker, kw = SCENARIOS[name]
        surv, frac = 0, []
        for i in range(runs):
            rng = random.Random(seed0 + i)
            targets = maker(rng)
            res = run_scenario(targets, cfg_with(**over), seed=seed0 + i, **kw)
            surv += res.survived
            frac.append(res.on_target_fraction())
        out[name] = {"survived": surv / runs, "on_target": st.mean(frac), "runs": runs}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forms", nargs="+", default=list(ALL_FORMS))
    ap.add_argument("--runs", type=int, default=500)
    ap.add_argument("--seed0", type=int, default=0)
    ap.add_argument("--no-shadows", action="store_true")
    ap.add_argument("--sensitivity", type=float, nargs="+", default=None,
                     help="прогнать чувствительность к SHADOW_TAKEN_RATIO (напр. 0.6 0.7 0.85)")
    ap.add_argument("--filter-level", type=int, default=2)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    report = {"runs": args.runs, "forms": {}, "sensitivity": {}}
    names = list(SCENARIOS)

    if args.sensitivity:
        form = args.forms[0]
        for ratio in args.sensitivity:
            report["sensitivity"][str(ratio)] = evaluate(
                form, args.runs, args.seed0, shadows=True, taken_ratio=ratio,
                filter_level=args.filter_level)
        print(f"чувствительность к SHADOW_TAKEN_RATIO (форма {form}, {args.runs} прогонов)\n")
        head = "| порог | " + " | ".join(names) + " | среднее |"
        print(head); print("|---" * (len(names) + 2) + "|")
        for ratio, res in report["sensitivity"].items():
            vals = [res[n]["survived"] for n in names]
            print(f"| {ratio} | " + " | ".join(f"{v:.3f}" for v in vals)
                  + f" | **{st.mean(vals):.3f}** |")
    else:
        for form in args.forms:
            report["forms"][form] = evaluate(form, args.runs, args.seed0,
                                              shadows=not args.no_shadows,
                                              filter_level=args.filter_level)
        print(f"доля прогонов без подмены ({args.runs} на сценарий, "
              f"теневые {'выкл' if args.no_shadows else 'вкл'})\n")
        print("| форма | " + " | ".join(names) + " | среднее |")
        print("|---" * (len(names) + 2) + "|")
        for form, res in report["forms"].items():
            vals = [res[n]["survived"] for n in names]
            print(f"| {form} | " + " | ".join(f"{v:.3f}" for v in vals)
                  + f" | **{st.mean(vals):.3f}** |")

    if args.out:
        json.dump(report, open(args.out, "w"), indent=2, ensure_ascii=False)
        print("\nподробности:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
