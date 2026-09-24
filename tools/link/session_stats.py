#!/usr/bin/env python3
"""Сводка полевой сессии слежения по log.csv приложения (TrackActivity).

На вход — папка сессии, стянутая с телефона (track/<метка>_<тег>/), или сам
log.csv; можно несколько. На выход — по сессии и итогом:

  СЛЕЖЕНИЕ   доля времени «вед», число потерь цели, их длительность
             (медиана, максимум), самое длинное непрерывное ведение;
  НАВЕДЕНИЕ  ошибка_град при ведении: p50 / p95 / макс по модулю;
  МОТОР      уставка ω (p95 модуля, максимум), доли тактов с флагами
             сторож / срыв / энкодер-не-жив / кламп / рампа-насыщена;
  ТЕЛЕФОН    такт и инференс p50/p95, фактическая частота кадров.

ГЕЙТ ПЕРЕД ЦИФРАМИ. Сначала log_health: пустая или тождественно постоянная
колонка, на которой строится сводка, превращает её цифру в выдумку. Такая
цифра не печатается вовсе, вместо неё — «НЕТ ДАННЫХ (колонка пуста)».
Ноль в отчёте должен означать ноль, а не отсутствие измерения.

    session_stats.py runs/field/2609*_*/   [--csv итог.csv]
"""
import argparse, csv, math, os, sys
import numpy as np


def load(path):
    p = os.path.join(path, "log.csv") if os.path.isdir(path) else path
    with open(p, encoding="utf-8") as f:
        return p, list(csv.DictReader(f))


def col(rows, name):
    """-> float-массив или None, если колонка пуста/отсутствует/постоянна."""
    if not rows or name not in rows[0]:
        return None
    v = []
    for r in rows:
        x = (r.get(name) or "").strip()
        try:
            v.append(float(x))
        except ValueError:
            v.append(np.nan)
    a = np.array(v)
    return None if np.isnan(a).all() else a


def pct(a, q):
    a = a[~np.isnan(a)]
    return float(np.percentile(a, q)) if len(a) else float("nan")


def segments(mask):
    """-> список (начало, конец) подряд идущих True."""
    out, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            out.append((i, j)); i = j
        else:
            i += 1
    return out


def stats(path):
    p, rows = load(path)
    if len(rows) < 10:
        return {"сессия": path, "ошибка": f"строк {len(rows)} — нечего считать"}
    t = col(rows, "t_ms")
    if t is None:
        return {"сессия": path, "ошибка": "нет колонки t_ms"}
    t = (t - t[0]) / 1000.0
    dur = float(t[-1])
    st = [(r.get("состояние") or "").strip() for r in rows]
    ved = np.array([s.startswith("вед") for s in st])
    s = {"сессия": os.path.basename(os.path.dirname(p)) or p,
         "длит_с": round(dur, 1), "кадров": len(rows),
         "fps": round(len(rows) / dur, 1) if dur > 0 else float("nan")}

    # --- слежение ---
    s["доля_ведения"] = round(float(ved.mean()), 3)
    seg_v = segments(ved)
    seg_l = [g for g in segments(~ved) if g[0] > 0]      # потери ПОСЛЕ первого захвата
    if seg_v:
        s["первый_захват_с"] = round(float(t[seg_v[0][0]]), 1)
        s["макс_ведение_с"] = round(max(t[min(b, len(t)-1)] - t[a] for a, b in seg_v), 1)
    s["потерь"] = len([g for g in seg_l if g[0] > (seg_v[0][0] if seg_v else 0)])
    if seg_l:
        d = np.array([t[min(b, len(t)-1)] - t[a] for a, b in seg_l])
        s["потеря_медиана_с"] = round(float(np.median(d)), 2)
        s["потеря_макс_с"] = round(float(d.max()), 2)

    # --- наведение ---
    e = col(rows, "ошибка_град")
    if e is None:
        s["ошибка"] = "НЕТ ДАННЫХ (колонка пуста)"
    elif ved.any():
        ae = np.abs(e[ved])
        s["ошибка_p50_град"] = round(pct(ae, 50), 2)
        s["ошибка_p95_град"] = round(pct(ae, 95), 2)
        s["ошибка_макс_град"] = round(float(np.nanmax(ae)), 2)

    # --- мотор ---
    w = col(rows, "ω_уставка")
    if w is not None:
        s["ω_p95"] = round(pct(np.abs(w), 95), 3)
        s["ω_макс"] = round(float(np.nanmax(np.abs(w))), 3)
    enc = col(rows, "энкодер")
    if enc is None or np.nanmax(enc) == 0:
        # Ни одного такта с живым энкодером: либо мотор не подключён (сухой
        # прогон), либо связь не шла. Флаги мотора тогда ничего не значат.
        s["мотор"] = "НЕТ СВЯЗИ С МОТОРОМ (энкодер ни разу не жив)"
    else:
        for name in ("watchdog", "срыв", "кламп", "рампа"):
            a = col(rows, name)
            if a is not None:
                s[f"доля_{name}"] = round(float(np.nanmean(a > 0)), 4)
        s["доля_энкодер_мёртв"] = round(float(np.nanmean(enc == 0)), 4)

    # --- телефон ---
    for name, key in (("такт_мс", "такт"), ("инференс_мс", "инференс")):
        a = col(rows, name)
        if a is not None:
            s[f"{key}_p50_мс"] = round(pct(a, 50), 1)
            s[f"{key}_p95_мс"] = round(pct(a, 95), 1)
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("пути", nargs="+")
    ap.add_argument("--csv", default=None)
    a = ap.parse_args()
    все = [stats(p) for p in a.пути]
    for s in все:
        print(f"\n== {s['сессия']} ==")
        for k, v in s.items():
            if k != "сессия":
                print(f"  {k:<22} {v}")
    ok = [s for s in все if "ошибка" not in s or isinstance(s.get("ошибка"), str) is False]
    if len(все) > 1:
        dur = sum(s.get("длит_с", 0) for s in все)
        vt = sum(s.get("длит_с", 0) * s.get("доля_ведения", 0) for s in все)
        print(f"\n== ИТОГО: {len(все)} сессий, {dur/60:.1f} мин, на цели {vt/60:.1f} мин "
              f"({vt/dur:.0%}), потерь {sum(s.get('потерь', 0) for s in все)} ==")
    if a.csv:
        keys = sorted({k for s in все for k in s})
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(все)
        print(f"\nтаблица: {a.csv}")


if __name__ == "__main__":
    main()
