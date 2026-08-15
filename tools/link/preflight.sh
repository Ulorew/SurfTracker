#!/bin/bash
# ПРЕДПОЛЁТНАЯ ПРОВЕРКА перед любым прогоном слежения.
#
# Дважды подряд прогон был потерян из-за того, что на плате лежал
# диагностический скетч, а не боевая прошивка: телеметрия ноль, мотор не
# крутится, и выясняется это только по результату — то есть после того, как
# Hero отстоял перед камерой полминуты.
#
# Проверка стоит двадцать секунд и снимает весь класс отказа.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MAC=${1:-38:18:2B:30:7D:86}

echo "1/2 плата отвечает по протоколу?"
"$ROOT/.venv/bin/python" - "$MAC" << 'PY'
import sys, time, socket
sys.path.insert(0, "tools/link")
from proto_v2 import *
mac = sys.argv[1]
try:
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    s.settimeout(15.0); s.connect((mac, 1)); s.setblocking(False)
except Exception as e:
    print("   BT НЕ ПОДНЯЛСЯ:", e); sys.exit(2)
time.sleep(0.3)
try:
    while s.recv(4096): pass
except Exception: pass
rx = bytearray(); got = 0
for i in range(20):
    s.sendall(build_req(i & 0x7F, 0.0, 0.0))          # НОЛЬ: вал не тронется
    t = time.perf_counter()
    while time.perf_counter() - t < 0.1:
        try: c = s.recv(512)
        except Exception: c = b""
        if c:
            rx.extend(c)
            while len(rx) >= TEL_LEN:
                if rx[0] != MAGIC_TEL: rx.pop(0); continue
                p = parse_tel(bytes(rx[:TEL_LEN]))
                if p is None: rx.pop(0); continue
                del rx[:TEL_LEN]; got += 1
                last = p
        else: time.sleep(0.002)
s.close()
if got < 15:
    print(f"   ОТВЕТОВ {got}/20 — на плате не боевая прошивка либо канал мёртв")
    sys.exit(3)
print(f"   ответов {got}/20, энкодер {'жив' if last[3] & ST_ENC_OK else 'МОЛЧИТ'}, "
      f"угол {last[1]:+.3f} рад")
PY
rc=$?
[ $rc -ne 0 ] && { echo "ПРЕДПОЛЁТ НЕ ПРОЙДЕН. Залейте phone_link:"; \
  echo "  cd $ROOT/stm && arduino-cli upload -b STMicroelectronics:stm32:Disco:pnum=B_G431B_ESC1,upload_method=swdMethod -p /dev/ttyACM0 phone_link"; exit $rc; }

echo "2/2 телефон на связи?"
. ~/Android/env.sh 2>/dev/null
adb devices 2>/dev/null | grep -q "device$" || { echo "   ТЕЛЕФОН НЕ ВИДЕН"; exit 4; }
echo "   виден"
echo "ПРЕДПОЛЁТ ПРОЙДЕН"
