#!/usr/bin/env python3
"""Сборка видео прогона слежения с наложенной логикой.

Кадры записаны в том виде, в каком их видела модель (кроп и уменьшение до
640), а поверх рисуется то, что она из них вывела: рамка детекции, окно,
ошибка наведения, команда, состояние механизмов.

Смысл именно в совмещении. Лог отвечает на «что произошло», кадр — на
«почему», и порознь они не отвечают ни на что: сегодняшняя потеря выглядела
в логе как «уверенность упала до 0.44», а на кадре оказалась торсом во весь
экран, который детектор людей и не обязан узнавать.

    render_run.py <папка_кадров> <csv> <выход.mp4>
"""
import sys, os, csv, math
import numpy as np
import cv2

BAND = (0.20, 0.30)          # полоса раскачки, подсвечивается на шкале


def draw(img, r, prev_hit, fps):
    h, w = img.shape[:2]
    hit = r["есть_цель"] == "1"
    conf = float(r["conf"])
    # РАМКА ДЕТЕКЦИИ в координатах тензора — рисуется только при попадании,
    # иначе показывали бы прошлую рамку как текущую.
    if hit:
        bx, by = float(r["bx"]), float(r["by"])
        bw, bh = float(r["bw"]), float(r["bh"])
        p1 = (int(bx - bw / 2), int(by - bh / 2))
        p2 = (int(bx + bw / 2), int(by + bh / 2))
        cv2.rectangle(img, p1, p2, (0, 230, 0), 2)
        cv2.putText(img, f"{conf:.2f}", (p1[0], max(14, p1[1] - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 230, 0), 1, cv2.LINE_AA)
    else:
        # Потеря отмечается рамкой по краю: её видно даже при беглом просмотре,
        # а искать по подписи пришлось бы глазами.
        cv2.rectangle(img, (2, 2), (w - 3, h - 3), (0, 0, 235), 4)
        cv2.putText(img, "ЦЕЛЬ ПОТЕРЯНА", (12, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 235), 2, cv2.LINE_AA)

    # центр кадра — куда контур сводит цель
    cv2.line(img, (w // 2, 0), (w // 2, h), (200, 200, 200), 1)

    # ПАНЕЛЬ ЛОГИКИ снизу
    pan = np.zeros((120, w, 3), np.uint8)
    err = float(r["ошибка_град"]); om = float(r["ω_уставка"])
    sc = float(r.get("Sc") or 0); shr = float(r.get("ужатие") or 1)
    txt = [
        f"t={float(r['t_ms'])/1000:6.2f} c    ошибка {err:+6.1f} гр    команда {om:+6.3f} рад/с",
        f"окно Sc={sc:.0f}   сглаж {float(r.get('ω_сглаж') or 0):.3f}   "
        f"ужатие {shr:.3f}{'  <- ПРЕДЕЛ' if shr < 0.999 else ''}",
    ]
    for i, s in enumerate(txt):
        cv2.putText(pan, s, (10, 26 + i * 26), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (235, 235, 235), 1, cv2.LINE_AA)

    # шкала команды с подсвеченной полосой раскачки
    x0, x1, y = 10, w - 10, 100
    cv2.line(pan, (x0, y), (x1, y), (90, 90, 90), 1)
    span = 0.8
    def px(v): return int(x0 + (v + span) / (2 * span) * (x1 - x0))
    for sgn in (1, -1):
        cv2.rectangle(pan, (px(sgn * BAND[0]), y - 6), (px(sgn * BAND[1]), y + 6),
                      (0, 120, 200), -1)
    cv2.line(pan, (px(0), y - 10), (px(0), y + 10), (150, 150, 150), 1)
    cv2.circle(pan, (px(max(-span, min(span, om))), y), 7, (0, 230, 0)
               if abs(om) < BAND[0] else (0, 165, 255), -1)
    return np.vstack([img, pan])


def main(frames_dir, csv_path, out_path):
    rows = list(csv.DictReader(open(csv_path)))
    files = sorted(f for f in os.listdir(frames_dir) if f.endswith(".jpg"))
    if not files:
        print("кадров нет"); return 1
    # Кадры именованы по номеру такта, поэтому сопоставление прямое, а не по
    # порядку файлов: пропущенный кадр не должен сдвигать всю дорожку.
    by_idx = {int(os.path.splitext(f)[0]): f for f in files}
    t = [float(r["t_ms"]) / 1000 for r in rows]
    fps = max(1.0, (len(t) - 1) / max(1e-6, t[-1] - t[0]))
    first = cv2.imread(os.path.join(frames_dir, files[0]))
    h, w = first.shape[:2]
    vw = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h + 120))
    n = 0
    prev_hit = False
    for i, r in enumerate(rows):
        f = by_idx.get(i)
        if f is None:
            continue
        img = cv2.imread(os.path.join(frames_dir, f))
        if img is None:
            continue
        vw.write(draw(img, r, prev_hit, fps))
        prev_hit = r["есть_цель"] == "1"
        n += 1
    vw.release()
    print(f"собрано {n} кадров при {fps:.2f} к/с -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
