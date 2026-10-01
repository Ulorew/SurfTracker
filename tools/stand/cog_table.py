#!/usr/bin/env python3
"""Съём таблицы зубцов и разложение по порядкам. Пишет в runs/, а не в /tmp.

ПОЧЕМУ ЭТО ВАЖНО. Первый съём 5 сентября печатался в терминал, а сырые корзины
клались в /tmp — и пропали при первой же перезагрузке. Уцелело только то, что
успели скопировать из терминала. Таблица переснимается за две минуты, но разбор, который
на неё опирался, пришлось бы делать заново.

ЧТО МЕРЯЕТСЯ. Выход регулятора Uq как функция АБСОЛЮТНОГО механического угла.
Именно Uq, а не остаток угла: остаток показывает то, что контур НЕ ДОДАВИЛ,
то есть помеху уже после фильтрации чувствительностью контура, и зависит от
коэффициентов. Uq показывает саму помеху.

НУЛЕВОЙ ТЕСТ ОБЯЗАТЕЛЕН. Проекция на порядок 70.7, которого физически не
существует, показывает, сколько метод даёт из ничего. Без него амплитуда
22-го порядка — просто число.
"""
import argparse, math, os, re, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tf_met_sweep import открыть, команда

# Конфиг ЭТОГО мотора. Держится здесь же, рядом со сверкой, чтобы не
# разъехался с тем, что проверяется.
КОНФИГ = "P=4.0 I=5 D=0 Tf=0.01 MET=0.002 FFA=0 FFB=0"
СВЕРКА = [("P", 4.0), ("I", 5.0), ("Tf", 0.01), ("MET", 0.002)]

ПОРЯДКИ = [11, 22, 33, 44, 66, 132]
НУЛЕВЫЕ = [70.7, 17.3]          # не целые кратные: физически невозможны


def снять(s, v, sec):
    """-> (углы град, среднее Uq, отсчётов в корзине)."""
    s.reset_input_buffer()
    s.write(f"COG v={v} sec={sec}\n".encode())
    buf, t0 = "", time.time()
    while time.time() - t0 < sec + 30:
        buf += s.read(8192).decode("utf-8", "replace")
        if "#ЗУБЦЫ_КОНЕЦ" in buf:
            break
    ang, uq, n, беру = [], [], [], False
    for line in buf.splitlines():
        line = line.strip()
        if line.startswith("#ЗУБЦЫ_НАЧАЛО"):
            беру = True; continue
        if line.startswith("#ЗУБЦЫ_КОНЕЦ"):
            break
        if беру and line:
            p = line.split()
            if len(p) == 3:
                try:
                    ang.append(float(p[0])); uq.append(float(p[1])); n.append(int(p[2]))
                except ValueError:
                    pass
    return np.array(ang), np.array(uq), np.array(n)


def разложить(ang, uq, n):
    """-> {порядок: (амплитуда В, фаза град)}. Проекция на cos/sin(k*theta)."""
    m = n > 0
    th = np.deg2rad(ang[m])
    u = uq[m] - uq[m].mean()
    out = {}
    for k in ПОРЯДКИ + НУЛЕВЫЕ:
        c = (u * np.cos(k * th)).mean() * 2
        sn = (u * np.sin(k * th)).mean() * 2
        out[k] = (math.hypot(c, sn), math.degrees(math.atan2(sn, c)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--скорости", default="0.628,0.628,1.571",
                    help="повтор одной скорости даёт цену повторения")
    ap.add_argument("--секунд", type=float, default=60.0)
    ap.add_argument("--каталог", default=None)
    a = ap.parse_args()

    кат = a.каталог or f"runs/cog_{time.strftime('%Y-%m-%d_%H%M')}"
    os.makedirs(кат, exist_ok=True)
    скор = [float(x) for x in a.скорости.split(",")]

    print(f"ТАБЛИЦА ЗУБЦОВ -> {кат}")
    print("вал не трогать\n")
    s = открыть()
    таблицы = []
    try:
        # КОНФИГ ВЫСТАВЛЯЕТСЯ ЦЕЛИКОМ И СВЕРЯЕТСЯ ПО ОТВЕТУ ПЛАТЫ.
        #
        # Прежде здесь стояли только Tf и MET, а P и I брались из умолчаний
        # скетча — то есть от ДРУГОГО мотора. 18 сентября это дало две
        # негодные таблицы подряд: съём шёл при P=2.0/I=80, на которых 3506
        # трясёт, и оператор заметил большую амплитуду шума раньше, чем
        # инструмент. Печать конфига не спасла бы: её никто не читает.
        # Поэтому не печать, а ГЕЙТ — расходится, значит не снимаем.
        команда(s, f"SET {КОНФИГ}", 0.8)
        ответ = команда(s, "STATE", 0.8)
        for поле, ждём in СВЕРКА:
            m = re.search(rf"{поле}=([\d.]+)", ответ)
            if not m or abs(float(m.group(1)) - ждём) > 1e-3:
                print(f"ОТКАЗ: плата отвечает {поле}={m.group(1) if m else '?'}, "
                      f"а нужно {ждём}. Съём не начат.")
                print(f"  ответ платы: {ответ.strip()[-160:]}")
                return
        print(f"  конфиг сверен: {КОНФИГ}")
        for i, v in enumerate(скор):
            ang, uq, n = снять(s, v, a.секунд)
            if len(ang) < 300:
                print(f"  прогон {i}: таблица не собралась ({len(ang)} корзин)")
                continue
            путь = os.path.join(кат, f"cog_{i}_v{v:.3f}.csv")
            with open(путь, "w") as f:
                f.write("угол_град,uq_среднее,отсчётов\n")
                for A, U, N in zip(ang, uq, n):
                    f.write(f"{A:.2f},{U:.6f},{N}\n")
            print(f"  прогон {i}: v={v:.3f}, заполнено {int((n>0).sum())}/{len(n)} корзин, "
                  f"{n[n>0].mean():.0f} отсчётов на корзину -> {os.path.basename(путь)}")
            таблицы.append((v, ang, uq, n))
            time.sleep(3.0)
    finally:
        команда(s, "OFF", 0.5); s.close(); print("\nполе снято")

    if not таблицы:
        return
    свод = os.path.join(кат, "гармоники.csv")
    with open(свод, "w") as f:
        f.write("скорость,порядок,амплитуда_В,фаза_град,нулевой\n")
        print("\nРАЗЛОЖЕНИЕ ПО ПОРЯДКАМ (амплитуда Uq, В)")
        for v, ang, uq, n in таблицы:
            g = разложить(ang, uq, n)
            стр = f"  v={v:.3f}: "
            for k in ПОРЯДКИ + НУЛЕВЫЕ:
                amp, ph = g[k]
                f.write(f"{v},{k},{amp:.5f},{ph:.1f},{int(k in НУЛЕВЫЕ)}\n")
                if k in (11, 22, 44) or k in НУЛЕВЫЕ:
                    стр += f"{k}:{amp:.4f}В@{ph:+6.1f}°  "
            print(стр)
    print(f"  гармоники -> {свод}")

    if len(таблицы) >= 2:
        (_, _, u1, n1), (_, _, u2, n2) = таблицы[0], таблицы[1]
        m = (n1 > 0) & (n2 > 0)
        r = float(np.corrcoef(u1[m], u2[m])[0, 1])
        print(f"\nПОВТОРЯЕМОСТЬ: корреляция {r:.3f}, СКО разности "
              f"{np.std(u1[m]-u2[m]):.4f} В при размахе {np.ptp(u1[m]):.4f} В")
        if r < 0.9:
            print("  ВНИМАНИЕ: корреляция низкая — таблица не воспроизводится,")
            print("  компенсировать по ней нельзя")


if __name__ == "__main__":
    main()
