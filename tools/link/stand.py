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

    def probe_version(self, timeout_s=3.0):
        """Какой длины DIAG говорит плата. -> (длина или None, пояснение).

        ЗАЧЕМ ОТДЕЛЬНАЯ ПРОВЕРКА. Прежний различитель прошивки — «отвечает ли
        плата на команду 0xC3» — стал негодным: на неё отвечают ОБЕ новые
        версии, и та, что несёт подтверждение уставок, и та, что нет.

        А несовпадение версий даёт не мусор, а МОЛЧАНИЕ: 24-байтные кадры не
        добирают до 27, CRC не сходится, приёмник вечно ресинхронизируется.
        Снаружи это выглядит как «связь пропала», и искать будут в радио, в
        питании, в чём угодно — но не в том, что стороны собирают кадры разной
        длины. Дешевле спросить один раз на старте.
        """
        self.cmd(CMD_DIAG, 1.0)
        time.sleep(0.2)
        self.drain()                      # выбросить ответ на саму команду
        self.buf.clear()
        self.req(0.0, 0.0)
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            try:
                chunk = self.s.recv(4096)
                if chunk:
                    self.buf += chunk
            except (BlockingIOError, socket.timeout):
                pass
            if self.buf and self.buf[0] == MAGIC_DIAG and len(self.buf) >= DIAG_LEN:
                n = len(self.buf)
                self.cmd(CMD_DIAG, 0.0)
                self.buf.clear()
                return DIAG_LEN, f"плата ответила диагностикой, кадр >= {DIAG_LEN} байт"
            if self.buf and self.buf[0] == MAGIC_TEL and len(self.buf) >= TEL_LEN:
                # Диагностику не включила вовсе — команду не поняла.
                self.buf.clear()
                return None, "плата ответила телеметрией: команду диагностики не поняла"
            time.sleep(0.01)
        self.cmd(CMD_DIAG, 0.0)
        got = len(self.buf)
        self.buf.clear()
        if got:
            return got, (f"накопилось {got} байт и кадр не собрался: похоже на "
                         f"СТАРУЮ прошивку с кадром {got} байт против наших {DIAG_LEN}")
        return None, "плата не ответила вовсе"

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
    mism = []
    nxt = t0
    while True:
        now = time.time()
        if now - t0 >= seconds:
            break
        if now - last_cmd >= CMD_REFRESH_S:
            # ПЕРЕОТПРАВЛЯЮТСЯ ВСЕ УСТАВКИ, а не один режим. Сторож платы
            # гасит ЧЕТЫРЕ переключателя разом (режим, скорость, сырой угол,
            # диагностику), а поддержка взводила один. Одна задержка канала
            # больше 2 с — и дальше отрезок шёл бы так: диагностика выключена
            # (в лог молча идёт меньше строк, ответы становятся телеметрией и
            # отбрасываются), скорость обнулена, а режим взведён обратно. То
            # есть НЕПОДВИЖНОЕ ПОЛЕ с постоянным током в одну пару обмоток —
            # ровно тот отказ, ради которого написан блок питания фаз.
            link.cmd(CMD_VOLT, volt)
            link.cmd(CMD_SPIN, spin)
            link.cmd(CMD_RAW, 1.0 if raw else 0.0)
            link.cmd(CMD_DIAG, 1.0)
            link.cmd(CMD_MODE, float(mode))
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
            # СЛИЧЕНИЕ ЗАКАЗАННОГО С ПРИМЕНЁННЫМ. Колонки mode/volt/spin —
            # это то, что хост ПОПРОСИЛ; ack_* — то, в чём плата была на
            # момент отсчёта. Пока подтверждения не было, расхождение
            # выглядело неотличимо от нормы ПО ПОСТРОЕНИЮ: потерянная или
            # отклонённая команда давала замер в чужом режиме под правильной
            # подписью.
            if p2.get("ack_mode") is not None:
                # НАПРЯЖЕНИЕ И СКОРОСТЬ СЛИЧАЮТСЯ ТОЛЬКО В СТЕНДОВЫХ РЕЖИМАХ.
                # В боевом плата их не применяет вовсе: напряжение берётся
                # боевое, скорость приходит уставкой. Плата честно отвечает
                # своим хранимым stand_volt (умолчание 2.0 В), а хост просит
                # 0.0 — и первая же редакция проверки объявила расхождением
                # ВСЕ 1762 пробы подряд. Проверка, которая срабатывает всегда,
                # ничем не лучше проверки, которая не срабатывает никогда.
                mism_mode = p2["ack_mode"] != mode
                mism_par = (mode != MODE_FIGHT
                            and (abs(p2.get("ack_volt", 0.0) - volt) > 0.011
                                 or abs(p2.get("ack_spin", 0.0) - spin) > 0.011))
                if mism_mode or mism_par:
                    mism.append((p2["t_host"], p2.get("ack_mode_name"),
                                 p2.get("ack_volt"), p2.get("ack_spin")))
            w.writerow(p2)
            n_ok += 1
        time.sleep(0.001)

    # ОЖИДАЕМОЕ ЧИСЛО ПРОБ СЛИЧАЕТСЯ С ПОЛУЧЕННЫМ. Раньше n_ok возвращался и
    # нигде не сравнивался ни с чем — величина посчитана и не прочитана. А
    # тихая недостача строк это и есть признак того, что диагностика на плате
    # погасла посреди отрезка.
    want = int(seconds * hz)
    if n_ok < want * 0.8:
        print(f"      ВНИМАНИЕ: проб {n_ok}, ожидалось около {want}"
              f" ({100.0 * n_ok / want:.0f}%) — часть отрезка диагностики не было")
    if mism:
        t, m, v, sp = mism[0]
        print(f"      РАСХОЖДЕНИЕ ЗАКАЗА И ПОДТВЕРЖДЕНИЯ: {len(mism)} проб из"
              f" {n_ok}; первое на {t:.2f} с — плата была в режиме {m},"
              f" U={v}, w={sp}")
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
    # ЛЕСТНИЦА СКОРОСТЕЙ — контрольный замер для замкнутого контура.
    #
    # Меряет ПЛАВНОСТЬ разомкнутого контура на малых скоростях: именно её
    # замыкание должно улучшить, и без этой кривой сравнивать будет не с чем.
    # Заход на каждую скорость СНИЗУ, через остановку — так же, как разгоняется
    # трекер, и так же, как возбуждается задокументированная раскачка.
    "ladder": [],          # собирается ниже
    # Тест А без рук: между замерами вал переставляется вращением.
    "positions": [],       # собирается ниже, чтобы не плодить копипасту
}
for _w in (0.05, 0.08, 0.12, 0.16, 0.20, 0.25, 0.30, 0.40, 0.50):
    PLANS["ladder"] += [(f"w{int(_w * 100):03d}", MODE_SPIN, 2.0, _w, 30)]

for _i in range(6):
    PLANS["positions"] += [
        (f"move_{_i}",  MODE_SPIN, 2.0, 0.15, 4),
        (f"pos_{_i}",   MODE_HOLD, 2.0, 0.0,  12),
    ]

# ЗАКАЗАННОЕ И ПРИМЕНЁННОЕ ЛЕЖАТ РЯДОМ И РАЗНЫМИ ИМЕНАМИ. mode/volt/spin —
# просьба хоста, ack_* — ответ платы. Складывать их в одну колонку значило бы
# снова сделать расхождение невидимым.
FIELDS = ["label", "mode", "volt", "spin",
          "ack_mode_name", "ack_volt", "ack_spin",
          "t_host", "seq", "t_us",
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

    # СВЕРКА ВЕРСИЙ ДО ПЕРВОГО ЗАМЕРА, а не после. Прогон на несовпадающих
    # версиях даёт пустой файл и час поисков не там.
    n, why = link.probe_version()
    if n != DIAG_LEN:
        link.close()
        sys.exit(f"ОТКАЗ: версии прошивки и хоста расходятся.\n  {why}\n"
                 f"  Хост ждёт кадр {DIAG_LEN} байт. Залейте текущую прошивку:\n"
                 f"    ./stm/build.sh phone_link --прошить")
    print(f"  версия сошлась: {why}")
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
