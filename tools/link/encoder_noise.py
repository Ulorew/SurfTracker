#!/usr/bin/env python3
"""Шум энкодера на неподвижном валу — базовая линия до и после реформы.

ЗАЧЕМ. Разрешение тракта ограничено не энкодером, а micros(): один шаг
12-битного AS5048A длится 0.225 мкс при кадре 921 мкс, то есть вчетверо с
половиной короче микросекундного тика. Замер это показывает прямо — по
ИНТЕРВАЛУ МЕЖДУ НАБЛЮДАЕМЫМИ УРОВНЯМИ, а не по СКО.

Базовая линия 21.08.2026 (до реформы), 1165 отсчётов за 60 с:
    СКО 0.234°, размах 1.57°, уровней 5, интервал между уровнями 0.3925°
Предсказание цены тика micros(): 360/(919-3) = 0.3930° — сходится на 0.1%.

После реформы тот же замер обязан дать интервал 0.0879° (истинный шаг 12 бит)
и СКО около 0.05°. ЕСЛИ ИНТЕРВАЛ НЕ ИЗМЕНИЛСЯ — реформа не подключилась, и
это видно сразу, без разбора логов.

Вал должен быть НЕПОДВИЖЕН и обесточен: уставка нулевая, драйвер гаснет сам
через IDLE_OFF_MS. Тогда весь разброс показаний — это шум тракта целиком.

    encoder_noise.py [секунд] [MAC]
"""
import sys, time, socket, statistics as st, math
sys.path.insert(0, "tools/link")
from proto_v2 import *
s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
MAC = sys.argv[2] if len(sys.argv) > 2 else "38:18:2B:30:7D:86"
СЕК = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
s.settimeout(12.0); s.connect((MAC, 1)); s.setblocking(False)
time.sleep(0.3)
try:
    while s.recv(4096): pass
except Exception: pass
rx=bytearray(); ang=[]; t0=time.time(); i=0
while time.time()-t0 < СЕК:
    s.sendall(build_req(i & 0x7F, 0.0, 0.0)); i+=1
    t=time.perf_counter()
    while time.perf_counter()-t < 0.05:
        try: c=s.recv(512)
        except Exception: c=b""
        if c:
            rx.extend(c)
            while len(rx)>=TEL_LEN:
                if rx[0]!=MAGIC_TEL: rx.pop(0); continue
                p=parse_tel(bytes(rx[:TEL_LEN]))
                if p is None: rx.pop(0); continue
                del rx[:TEL_LEN]; ang.append(p[1])
        else: time.sleep(0.002)
s.close()

d=[math.degrees(a) for a in ang]
print(f"отсчётов {len(d)} за {СЕК:.0f} с")
if len(d)>10:
    print(f"  среднее {st.mean(d):+.4f}°")
    print(f"  СКО      {st.pstdev(d):.4f}°")
    print(f"  размах   {max(d)-min(d):.4f}°")
    print(f"  p99.9    {sorted(d)[int(0.999*len(d))-1]-st.mean(d):+.4f}°")
    sh=[abs(d[k+1]-d[k]) for k in range(len(d)-1)]
    print(f"  шаг между соседними: медиана {st.median(sh):.4f}°, макс {max(sh):.4f}°")
    ур=sorted(set(round(x,4) for x in d))
    print(f"  различных уровней: {len(ур)}")
    if len(ур)>1:
        dif=[ур[k+1]-ур[k] for k in range(len(ур)-1)]
        print(f"  минимальный интервал между уровнями: {min(dif):.4f}° (шаг 12 бит = 0.0879°)")
