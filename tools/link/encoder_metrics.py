#!/usr/bin/env python3
"""Метрики тракта энкодера: разрешение, шум, спектр. Сырой вариант.

    encoder_metrics.py [секунд] [--сохранить файл.csv]

ЗАЧЕМ ОТДЕЛЬНО ОТ jerk_metric.py. Тот меряет ВАЛ и ради этого ВЫЧИТАЕТ
шумовой пол датчика — иначе мерил бы датчик, как однажды уже вышло. Здесь
задача обратная: пол и есть предмет измерения, потому что реформа PWM меняет
именно его. Вычитать его тут значило бы стереть весь эффект.

ЧТО СЧИТАЕТСЯ И ЗАЧЕМ КАЖДОЕ

  РАЗРЕШЕНИЕ — минимальный интервал между наблюдаемыми уровнями угла. Главная
  метрика реформы и единственная, которую видно сразу, без вращения и спектра.
  На неподвижном валу измерено 0.3925° при истинном шаге 12 бит 0.0879°:
  разрешение ограничено не датчиком, а micros() (шаг энкодера 0.225 мкс
  короче микросекундного тика вчетверо с половиной). После реформы обязано
  стать 0.0879°; если не изменилось — реформа не подключилась.

  СКО и размах — грубая оценка шума. Осторожно: на неподвижном валу они
  почти целиком определяются квантованием, то есть меряют то же разрешение
  другими словами.

  СПЕКТР в полосе — где именно сидит шум. Нужен, чтобы отличить квантование
  (широкополосное) от наводки силовой части (узкие пики на частоте ШИМ и её
  долях) и от механики.

ПОЛОСА ЗАМЕРА И ЧЕМ ОНА ОГРАНИЧЕНА. Телеметрия при частом опросе идёт на
199 Гц (измерено), то есть Найквист около 100 Гц. НО в боевой прошивке угол
проходит через медиану пяти отсчётов с шагом 20 мс — окно 100 мс, и это
фильтр низких частот примерно на 5 Гц. Полосу выше него этим замером НЕ
получить: нужна сборка с TELEMETRY_RAW_THETA 1, она в прошивке предусмотрена.
Инструмент печатает это ограничение сам, чтобы вывод не приняли за свойство
датчика.

ВАЛ ДОЛЖЕН БЫТЬ НЕПОДВИЖЕН для метрик разрешения и шума: уставка нулевая,
драйвер гаснет через IDLE_OFF_MS сам. Тогда весь разброс есть шум тракта.
"""
import sys, time, socket, math, statistics as st

sys.path.insert(0, "tools/link")
from proto_v2 import *

MAC = "38:18:2B:30:7D:86"
ШАГ_12БИТ = 360.0 / 4096          # 0.0879° — истинный шаг датчика
ТИК_MICROS = 360.0 / (919 - 3)    # 0.3930° — цена микросекунды при этой калибровке


def снять(секунд, mac=MAC):
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    s.settimeout(12.0); s.connect((mac, 1)); s.setblocking(False)
    time.sleep(0.3)
    try:
        while s.recv(4096): pass
    except Exception:
        pass
    rx = bytearray(); проба = []; t0 = time.perf_counter(); i = 0
    while time.perf_counter() - t0 < секунд:
        s.sendall(build_req(i & 0x7F, 0.0, 0.0))   # НОЛЬ: вал не тронется
        i += 1
        t = time.perf_counter()
        while time.perf_counter() - t < 0.005:
            try: c = s.recv(512)
            except Exception: c = b""
            if not c: continue
            rx.extend(c)
            while len(rx) >= TEL_LEN:
                if rx[0] != MAGIC_TEL: rx.pop(0); continue
                p = parse_tel(bytes(rx[:TEL_LEN]))
                if p is None: rx.pop(0); continue
                del rx[:TEL_LEN]
                проба.append((time.perf_counter() - t0, math.degrees(p[1]), p[3]))
    s.close()
    return проба


def спектр(t, y, чmax=None):
    """Односторонний спектр амплитуд на равномерной сетке."""
    import numpy as np
    if len(t) < 32: return [], []
    fs = (len(t) - 1) / (t[-1] - t[0])
    сетка = np.arange(t[0], t[-1], 1.0 / fs)
    yy = np.interp(сетка, t, y)
    yy = yy - np.polyval(np.polyfit(np.arange(len(yy)), yy, 1), np.arange(len(yy)))
    окно = np.hanning(len(yy))
    F = np.abs(np.fft.rfft(yy * окно)) / (len(yy) / 4)
    ч = np.fft.rfftfreq(len(yy), 1.0 / fs)
    if чmax:
        м = ч <= чmax
        return ч[м], F[м]
    return ч, F


def метрики(проба):
    t = [p[0] for p in проба]; d = [p[1] for p in проба]
    st_ok = sum(1 for p in проба if p[2] & ST_ENC_OK)
    n = len(d)
    if n < 20:
        print("мало отсчётов"); return
    fs = (n - 1) / (t[-1] - t[0])
    print(f"отсчётов {n} за {t[-1]:.1f} с — частота {fs:.1f} Гц, "
          f"Найквист {fs/2:.0f} Гц")
    print(f"энкодер жив на {100*st_ok/n:.0f}% отсчётов")
    print()

    # --- РАЗРЕШЕНИЕ: главная метрика реформы
    уровни = sorted(set(round(x, 4) for x in d))
    print(f"РАЗРЕШЕНИЕ")
    print(f"  различных уровней:        {len(уровни)}")
    if len(уровни) > 1:
        инт = sorted(уровни[k+1] - уровни[k] for k in range(len(уровни)-1))
        шаг = инт[0]
        print(f"  минимальный интервал:     {шаг:.4f}°")
        print(f"  истинный шаг 12 бит:      {ШАГ_12БИТ:.4f}°")
        print(f"  цена тика micros():       {ТИК_MICROS:.4f}°")
        if abs(шаг - ТИК_MICROS) < 0.02:
            print(f"  ВЫВОД: упирается в micros() — реформа НЕ подключена")
        elif abs(шаг - ШАГ_12БИТ) < 0.02:
            print(f"  ВЫВОД: упирается в сам датчик — реформа работает")
        else:
            print(f"  ВЫВОД: не совпало ни с тем, ни с другим — разбираться")
    print()

    # --- ШУМ
    print(f"ШУМ (на неподвижном валу это в основном то же квантование)")
    print(f"  СКО:      {st.pstdev(d):.4f}°")
    print(f"  размах:   {max(d)-min(d):.4f}°")
    print(f"  p99.9:    {sorted(d)[int(0.999*n)-1] - st.mean(d):+.4f}°")
    print()

    # --- СПЕКТР
    try:
        ч, F = спектр(t, d, чmax=min(50, fs/2))
        if len(ч):
            print(f"СПЕКТР (полоса до {ч[-1]:.0f} Гц)")
            for lo, hi in ((0.5, 2), (2, 5), (5, 10), (10, 20), (20, 50)):
                м = [(f, a) for f, a in zip(ч, F) if lo <= f < hi]
                if м:
                    print(f"  {lo:>4.1f}-{hi:<4.0f} Гц: СКО {math.sqrt(sum(a*a for _, a in м)/2):.4f}°"
                          f"   пик {max(a for _, a in м):.4f}° на {max(м, key=lambda x: x[1])[0]:.1f} Гц")
            print()
            print("  ОГОВОРКА: боевая прошивка фильтрует угол медианой пяти")
            print("  отсчётов с шагом 20 мс — окно 100 мс режет всё выше ~5 Гц.")
            print("  Полосу выше нужно снимать сборкой с TELEMETRY_RAW_THETA 1.")
    except ImportError:
        print("(numpy нет — спектр пропущен)")


if __name__ == "__main__":
    сек = float(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else 30.0
    проба = снять(сек)
    метрики(проба)
    if "--сохранить" in sys.argv:
        путь = sys.argv[sys.argv.index("--сохранить") + 1]
        with open(путь, "w") as f:
            f.write("t_s,угол_град,статус\n")
            for a, b, c in проба:
                f.write(f"{a:.6f},{b:.6f},{c}\n")
        print(f"\nсырые отсчёты: {путь} ({len(проба)} строк)")
