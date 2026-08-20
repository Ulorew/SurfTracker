#!/usr/bin/env python3
"""Управление стендом энкодера: гонит плату по плану и пишет сырой DIAG в CSV.

ЧТО ДЕЛАЕТ. Держит режим (командой раз в полсекунды, иначе сработает сторож
платы через 2 с), опрашивает плату запросами и складывает ответы как есть.
Углы здесь НЕ СЧИТАЮТСЯ: в файл идут тики и микросекунды, формулу применяет
разборщик. Так формулу можно поменять, не перемеряя.

ПРЕДОХРАНИТЕЛИ. Потолки напряжения и скорости продублированы на клиенте — не
потому что плате не верю, а потому что ошибка в плане не должна доезжать до
обмоток. Выход из режима гарантирован через finally: даже по Ctrl-C и по
исключению плата возвращается в боевой режим, а поле гаснет.

ЗАЧЕМ СТОЛЬКО ПРО ВОЗВРАТ. Однажды стендовый режим уже оставил фазы под
напряжением до сброса платы, причём поле стояло неподвижно — постоянный ток в
одну пару обмоток. Нагрев локальный, и это хуже, чем вращение.
"""
import argparse, csv, os, socket, struct, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proto_v2 import (MAGIC_DIAG, MAGIC_TEL, DIAG_LEN, TEL_LEN, crc8,
                      build_req, build_cmd, parse_diag, parse_tel,
                      CMD_MODE, CMD_RAW, CMD_DIAG, CMD_VOLT, CMD_SPIN,
                      MODE_FIGHT, MODE_HOLD, MODE_SPIN)

MAC = "38:18:2B:30:7D:86"

# Потолки клиента. Держатся НИЖЕ или вровень с платой намеренно: плата — это
# последний рубеж, а не первый.
VOLT_MAX = 2.0          # правило владельца для долгой работы подряд
SPIN_MAX = 1.0          # рад/с по валу
CMD_REFRESH_S = 0.5     # сторож платы 2 с; обновляем вчетверо чаще

MODE_NAME = {MODE_FIGHT: "fight", MODE_HOLD: "hold", MODE_SPIN: "spin"}
NAME_MODE = {v: k for k, v in MODE_NAME.items()}


class Link:
    def __init__(self, mac=MAC, timeout=10.0):
        self.s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                               socket.BTPROTO_RFCOMM)
        self.s.settimeout(timeout)
        self.s.connect((mac, 1))
        # ПРИЁМ НЕБЛОКИРУЮЩИЙ. С таймаутом 0.5 с каждый проход цикла, куда
        # ещё не пришли данные, ЖДАЛ полсекунды — опрос падал с заданных
        # 50 Гц до 5.8, и в лог шло вчетверо меньше проб, чем заказано.
        # Снаружи это выглядело как «плата медленная», а медленным был клиент.
        self.s.setblocking(False)
        self.buf = bytearray()
        self.seq = 0
        self.crc_bad = 0
        self.junk = 0

    def _next_seq(self):
        self.seq = (self.seq + 1) & 0x7F
        return self.seq

    def _send(self, data):
        """Отправка на неблокирующем сокете: буфер передатчика может быть полон."""
        for _ in range(200):
            try:
                self.s.sendall(data)
                return
            except BlockingIOError:
                time.sleep(0.001)
        raise OSError("передатчик не разгружается 200 мс")

    def cmd(self, code, param):
        self._send(build_cmd(self._next_seq(), code, float(param)))

    def req(self, omega=0.0, omega_dot=0.0):
        self._send(build_req(self._next_seq(), omega, omega_dot))

    def drain(self):
        """Забрать всё, что пришло, и разобрать на кадры. -> список пакетов.

        Кадрирование то же, что на плате: ищем начало, проверяем CRC, при
        несовпадении сдвигаемся на байт. Иначе один потерянный байт увёл бы
        разбор навсегда, и это выглядело бы как «плата замолчала».
        """
        try:
            while True:
                chunk = self.s.recv(4096)
                if not chunk:
                    break
                self.buf += chunk
        except (BlockingIOError, socket.timeout):
            pass
        out = []
        while self.buf:
            b0 = self.buf[0]
            if b0 == MAGIC_DIAG:
                need, parse = DIAG_LEN, parse_diag
            elif b0 == MAGIC_TEL:
                need, parse = TEL_LEN, parse_tel
            else:
                del self.buf[0]
                self.junk += 1
                continue
            if len(self.buf) < need:
                break
            got = parse(bytes(self.buf[:need]))
            if got is None:
                del self.buf[0]
                self.crc_bad += 1
                continue
            out.append(got if isinstance(got, dict) else {"tel": got})
            del self.buf[:need]
        return out

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def segment(link, w, label, mode, volt, spin, seconds, hz, raw=False):
    """Один отрезок плана. Возвращает (принято, отбраковано)."""
    volt = min(float(volt), VOLT_MAX)
    spin = max(-SPIN_MAX, min(float(spin), SPIN_MAX))
    print(f"  [{label}] режим={MODE_NAME[mode]} U={volt:.2f}В "
          f"w={spin:+.2f}рад/с {seconds:.0f}с", flush=True)

    # ПОРЯДОК ВАЖЕН: сначала параметры, потом режим. Иначе между включением
    # режима и приходом напряжения плата успела бы такт-другой поработать на
    # прошлых значениях.
    link.cmd(CMD_VOLT, volt)
    link.cmd(CMD_SPIN, spin)
    link.cmd(CMD_RAW, 1.0 if raw else 0.0)
    link.cmd(CMD_DIAG, 1.0)
    link.cmd(CMD_MODE, float(mode))
    time.sleep(0.05)
    link.drain()                       # выбросить ответы на сами команды

    t0 = time.time()
    last_cmd = t0
    period = 1.0 / hz
    n_ok = 0
    nxt = t0
    while True:
        now = time.time()
        if now - t0 >= seconds:
            break
        if now - last_cmd >= CMD_REFRESH_S:
            link.cmd(CMD_MODE, float(mode))     # обновить сторож
            last_cmd = now
        if now >= nxt:
            link.req(0.0, 0.0)
            nxt = now + period
        for p in link.drain():
            if "t_us" not in p:
                continue
            p2 = dict(p)
            p2.update(label=label, mode=MODE_NAME[mode], volt=volt,
                      spin=spin, t_host=round(now - t0, 4))
            w.writerow(p2)
            n_ok += 1
        time.sleep(0.001)
    return n_ok


def safe_off(link):
    """Возврат в боевой режим. Зовётся всегда, в том числе из finally."""
    for _ in range(3):
        try:
            link.cmd(CMD_SPIN, 0.0)
            link.cmd(CMD_MODE, float(MODE_FIGHT))
            link.cmd(CMD_RAW, 0.0)
            link.cmd(CMD_DIAG, 0.0)
            time.sleep(0.1)
        except OSError as e:
            print("  возврат: ошибка канала:", e)
            return False
    # Сторож платы сам сбросит всё через 2 с, если команды не дошли.
    time.sleep(0.3)
    link.drain()
    print("  возврат в боевой режим отправлен")
    return True


PLANS = {
    # Контроль: то же, что мерилось ночью. Поле выключено, вал свободен.
    "baseline": [("baseline_off", MODE_FIGHT, 0.0, 0.0, 30)],
    # Тест Б: наводка под током без вращения.
    "hold":     [("baseline_off", MODE_FIGHT, 0.0, 0.0, 20),
                 ("hold_2v",      MODE_HOLD,  2.0, 0.0, 30),
                 ("after_off",    MODE_FIGHT, 0.0, 0.0, 20)],
    # Короткая проба перед полным вращением: убедиться, что вал ИДЁТ и
    # ничего не срывается, прежде чем крутить минуту.
    "probe":    [("probe_spin",   MODE_SPIN,  2.0, 0.30, 10)],
    # Тест В: вращение. Даёт линейность и мост между шкалами трактов.
    "spin":     [("spin_slow",    MODE_SPIN,  2.0, 0.15, 60)],
    # Тест А без рук: между замерами вал переставляется вращением.
    "positions": [],       # собирается ниже, чтобы не плодить копипасту
}
for _i in range(6):
    PLANS["positions"] += [
        (f"move_{_i}",  MODE_SPIN, 2.0, 0.15, 4),
        (f"pos_{_i}",   MODE_HOLD, 2.0, 0.0,  12),
    ]

FIELDS = ["label", "mode", "volt", "spin", "t_host", "seq", "t_us",
          "isr_high", "cap_age", "cap_high", "cap_period", "flags"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", choices=sorted(PLANS))
    ap.add_argument("--out", default=None)
    ap.add_argument("--hz", type=float, default=50.0)
    ap.add_argument("--mac", default=MAC)
    a = ap.parse_args()

    out = a.out or f"runs/stand_{a.plan}.csv"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    print(f"план '{a.plan}', опрос {a.hz:.0f} Гц -> {out}")
    link = Link(a.mac)
    total = 0
    try:
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            for label, mode, volt, spin, secs in PLANS[a.plan]:
                total += segment(link, w, label, mode, volt, spin, secs, a.hz)
                f.flush()
    finally:
        safe_off(link)
        link.close()
    print(f"итого пакетов: {total}, брак CRC: {link.crc_bad}, мусор: {link.junk}")
    print(f"файл: {out}")


if __name__ == "__main__":
    main()
