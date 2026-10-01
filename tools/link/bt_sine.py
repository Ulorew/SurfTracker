#!/usr/bin/env python3
"""Прогон связи телефон -> Bluetooth -> field_link -> мотор, целиком.

Телефон (BtLinkActivity) шлёт синус уставок, плата крутит вал и отвечает
телеметрией. Скрипт стягивает лог телефона и проверяет не «связь есть», а
пять вещей, каждая числом:
  1. потери кадров и задержка (RTT p50/p95/max);
  2. сторож не взводился посреди прогона (связь не рвалась);
  3. детектор срыва молчит;
  4. вал СЛЕДУЕТ команде: угол из телеметрии против интеграла ω_ramp.
     Наклон около +1 — знак согласован и вал идёт; около -1 — рассогласован
     знак угла с командой; около 0 — вал стоит;
  5. цикл мотора при подключённом Bluetooth (loop_hz, худший такт) — берётся
     с USB одним запросом STATE посреди прогона.

Открытие USB-порта ПЕРЕЗАГРУЖАЕТ плату, поэтому порт открывается ДО старта
приложения и держится открытым до конца.

    bt_sine.py --adb <IP>:PORT [--amp 0.2 --period 8 --sec 30]
"""
import argparse, csv, glob, io, json, math, subprocess, sys, time, re
import numpy as np, serial

ADB = __import__("os").path.expanduser("~/Android/sdk/platform-tools/adb")
PKG = "com.surftracker.camfps"
DIRP = f"/storage/emulated/0/Android/data/{PKG}/files/link"

SERIAL = None   # КОНКРЕТНОЕ устройство: телефон бывает виден в adb дважды
                # (mdns и явное подключение), и adb shell без -s отказывает.
                # Пустой ответ читался как «телефон спит» — ложный отказ.

def adb(*a, t=60):
    cmd = [ADB] + (["-s", SERIAL] if SERIAL else []) + list(a)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=t)
    return r.stdout + r.stderr

def usb_open():
    port = glob.glob("/dev/serial/by-id/*CP2102*")[0]
    s = serial.Serial(); s.port = port; s.baudrate = 115200; s.timeout = 0.2
    s.dtr = False; s.rts = False; s.open()
    s.rts = True; time.sleep(0.1); s.rts = False
    buf = ""; t0 = time.time()
    while "ГОТОВ" not in buf and time.time() - t0 < 15:
        buf += s.read(4096).decode("utf-8", "replace")
    line = [l for l in buf.splitlines() if "ГОТОВ" in l]
    if not line: sys.exit("ОТКАЗ: плата не доложила ГОТОВ")
    print(line[0].strip())
    return s

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adb", required=True)
    ap.add_argument("--amp", type=float, default=0.2)
    ap.add_argument("--period", type=float, default=8.0)
    ap.add_argument("--sec", type=int, default=30)
    ap.add_argument("--out", default="runs/link_field")
    # MAC платы — явно. Телефон с ESP32 не сопряжён, а поиск по имени идёт
    # только среди сопряжённых: без MAC приложение пишет «устройство не
    # найдено» в JSON и молча отдаёт пустой CSV.
    ap.add_argument("--mac", default="38:18:2B:30:7D:86")
    a = ap.parse_args()

    global SERIAL
    adb("connect", a.adb)
    if f"{a.adb}\tdevice" not in adb("devices"):
        sys.exit("ОТКАЗ: телефон не подключился по adb")
    SERIAL = a.adb
    pw = adb("shell", "dumpsys", "power")
    m = re.search(r"mWakefulness=(\w+)", pw)
    if not m:
        sys.exit(f"ОТКАЗ: состояние экрана не прочитано, ответ adb: {pw.strip()[:120]}")
    if m.group(1) != "Awake":
        sys.exit(f"ОТКАЗ: телефон спит ({m.group(1)})")
    tag = time.strftime("bt_%m%d_%H%M%S")
    print(f"прогон {tag}: синус {a.amp} рад/с, период {a.period} с, {a.sec} с")
    print("вал не трогать")

    s = usb_open()
    adb("shell", "am", "force-stop", PKG)
    adb("shell", "am", "start", "-n", f"{PKG}/.BtLinkActivity",
        "--es", "tag", tag, "--es", "mode", "sine", "--es", "mac", a.mac,
        "--ef", "amp", str(a.amp), "--ef", "period", str(a.period),
        "--ei", "hz", "10", "--ei", "seconds", str(a.sec))
    time.sleep(a.sec / 2)
    s.write(b"STATE\n"); time.sleep(0.5)
    st = s.read(4096).decode("utf-8", "replace")
    st = [l for l in st.splitlines() if "STATE" in l]
    kv = dict(re.findall(r"(\w+)=(-?[\d.]+)", st[0])) if st else {}
    time.sleep(a.sec / 2 + 6)
    s.close()

    import os
    os.makedirs(a.out, exist_ok=True)
    for ext in ("csv", "json"):
        adb("pull", f"{DIRP}/{tag}.{ext}", f"{a.out}/{tag}.{ext}", t=120)
    if not os.path.exists(f"{a.out}/{tag}.csv"):
        sys.exit(f"ОТКАЗ: лог телефона не найден ({DIRP}/{tag}.csv)")
    rows = list(csv.DictReader(open(f"{a.out}/{tag}.csv", encoding="utf-8")))
    # JSON приложения читается как текст: при отказе он приходит битым
    # («{,"ok_flag":false,...}»), а причина отказа нужна именно тогда.
    js = open(f"{a.out}/{tag}.json", encoding="utf-8").read()
    if '"ok_flag":false' in js or not rows:
        sys.exit(f"ОТКАЗ ПРИЛОЖЕНИЯ: {js.strip()[:200]}")

    print("\n=== СВЯЗЬ ===")
    got = [r for r in rows if r.get("rtt_ms") not in (None, "", "nan")]
    rtt = np.array([float(r["rtt_ms"]) for r in got])
    print(f"кадров отправлено {len(rows)}, ответов {len(got)}, потерь {len(rows)-len(got)}")
    if len(rtt):
        print(f"RTT p50 {np.percentile(rtt,50):.1f} мс, p95 {np.percentile(rtt,95):.1f}, макс {rtt.max():.1f}")
    stt = np.array([int(r["статус"]) for r in got])
    wd = (stt & 1).astype(bool); slip = ((stt >> 5) & 1).astype(bool); enc = ((stt >> 3) & 1).astype(bool)
    print(f"сторож взведён в {wd[3:].sum()} ответах (кроме первых трёх), срыв в {slip.sum()}, "
          f"энкодер жив в {enc.mean():.0%}")

    print("\n=== ВАЛ СЛЕДУЕТ КОМАНДЕ? ===")
    t = np.array([float(r["t_приёма_ns"]) for r in got]) * 1e-9
    th = np.array([float(r["θ_enc"]) for r in got])
    w = np.array([float(r["ω_ramp"]) for r in got])
    integ = np.concatenate([[0], np.cumsum(0.5 * (w[1:] + w[:-1]) * np.diff(t))])
    k = np.polyfit(integ, th - th[0], 1)
    res = (th - th[0]) - np.polyval(k, integ)
    print(f"наклон угла к интегралу команды: {k[0]:+.3f}  (норма +1)")
    print(f"размах команды {w.min():+.3f}..{w.max():+.3f} рад/с, путь вала {math.degrees(np.ptp(th)):.1f}°, "
          f"остаток {math.degrees(res.std()):.2f}° СКО")

    print("\n=== ЦИКЛ МОТОРА ПРИ ПОДКЛЮЧЁННОМ BT ===")
    if kv:
        print(f"link={kv.get('link')} loop_hz={kv.get('loop_hz')} худший такт {kv.get('loop_max_us')} мкс, "
              f"rx={kv.get('rx')} tx={kv.get('tx')} crc={kv.get('crc')}")
    else:
        print("STATE не получен")
    print(f"\nлоги: {a.out}/{tag}.csv, .json")

if __name__ == "__main__":
    main()
