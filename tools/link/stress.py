#!/usr/bin/env python3
"""Стресс-инжекция канала: доказательство анти-рывка перечислением.

Каждый сценарий — строка таблицы §8 спецификации. Проверка двойная:

  ЧИСЛОМ  — ω_ramp не меняется быстрее, чем позволяет рампа. Это и есть
            анти-рывок: канал дёргается, вал не может. Порог берётся из
            MAX_ACCEL с запасом на дискретность телеметрии.
  ГЛАЗАМИ — видео вала, снимает Hero. Число не покажет вибрацию, которой
            нет в команде; глаз не покажет скачок в 40 мс между кадрами
            телеметрии. Нужны оба.

Мотор ОБЯЗАН быть запитан, иначе проверяется половина цепи.

    python stress.py --mac 38:18:2B:30:7D:86 --scenario all
"""
import argparse
import json
import socket
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from proto_v2 import (MAGIC_TEL, TEL_LEN, ST_WATCHDOG, ST_EXTRAP_CAP,
                       ST_RAMP_SAT, ST_CLAMP, ST_SLIP, build_req, parse_tel,
                       self_check)

MAX_ACCEL = 1.0          # рад/с^2, как в прошивке
SETPOINT_LIMIT = 2.0


class Link:
    def __init__(self, mac, channel=1):
        self.s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                                socket.BTPROTO_RFCOMM)
        self.s.settimeout(15.0)
        self.s.connect((mac, channel))
        self.s.setblocking(False)
        self.rx = bytearray()
        self.log = []            # (t_приёма, seq, theta, w_ramp, status)
        self.sent_at = {}        # seq -> момент ОТПРАВКИ
        self.t0 = time.perf_counter()
        self.drain()

    def drain(self):
        try:
            while self.s.recv(4096):
                pass
        except Exception:
            pass

    def send(self, seq, w, wdot=0.0, corrupt=False):
        f = bytearray(build_req(seq, w, wdot))
        if corrupt:
            f[5] ^= 0x01          # порча одного байта тела
        self.sent_at[seq & 0x7F] = time.perf_counter() - self.t0
        self.s.sendall(bytes(f))

    def poll(self, seconds):
        """Читать телеметрию заданное время, копя лог."""
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            try:
                c = self.s.recv(512)
            except Exception:
                c = b""
            if c:
                self.rx.extend(c)
                while len(self.rx) >= TEL_LEN:
                    if self.rx[0] != MAGIC_TEL:
                        self.rx.pop(0)
                        continue
                    p = parse_tel(bytes(self.rx[:TEL_LEN]))
                    if p is None:
                        self.rx.pop(0)
                        continue
                    del self.rx[:TEL_LEN]
                    self.log.append((time.perf_counter() - self.t0,) + p)
            else:
                time.sleep(0.002)

    def hold(self, seq, w, seconds, hz=10.0):
        """Держать уставку, ПРОДОЛЖАЯ слать кадры.

        Не тишина: молчание дольше 300 мс само срабатывает watchdog, и тогда
        бит поднимался бы во всех сценариях подряд, ничего не различая.
        Именно на этом первая редакция обвязки и ошиблась.
        """
        n = max(1, int(seconds * hz))
        for i in range(n):
            self.send((seq + i) & 0x7F, w)
            self.poll(1.0 / hz)
        return (seq + n) & 0x7F

    def close(self):
        self.s.close()


# Верхняя оценка расхождения доставки двух соседних ответов. RTT p95 по BT с
# телефона — 75 мс; берём удвоенное, потому что разойтись могут оба конца.
DELIVERY_SLOP_S = 0.15


def check_no_jerk(log, sent_at, label, margin=1.5):
    """ω_ramp не обязан меняться быстрее рампы.

    Интервал берётся по времени ОТПРАВКИ соответствующих кадров, а не по
    времени прихода ответов. По Bluetooth два ответа приходят вплотную,
    интервал прихода стремится к нулю, и отношение взлетает до десятков
    рад/с² — это дрожание доставки, а не поведение вала. Первая редакция
    мерила именно его и обвиняла прошивку в рывках, которых не было.

    Запас margin мал (1.5), потому что база теперь честная: прошивка меняет
    w_ramp ровно на MAX_ACCEL*dt своего цикла, а разница времён отправки —
    верхняя оценка того же интервала.
    """
    # Ищем худшее ОТНОШЕНИЕ шага к разрешённому, а не превышение. Первая
    # редакция сравнивала превышение и, поскольку превышений нет, не
    # записывала НИЧЕГО: тест печатал 0.0 из 0.0 и проходил впустую. Такая
    # проверка хуже отсутствующей — она создаёт видимость покрытия.
    worst_ratio, worst_d, worst_allow, worst_at = -1.0, 0.0, 0.0, None
    pairs = 0
    for i in range(1, len(log)):
        s0, s1 = log[i - 1][1], log[i][1]
        t0, t1 = sent_at.get(s0), sent_at.get(s1)
        if t0 is None or t1 is None or t1 < t0:
            continue
        pairs += 1
        # Сколько прошло на СТОРОНЕ STM между формированием двух ответов, мы
        # знаем лишь с точностью до расхождения доставки: времени STM в
        # протоколе нет намеренно (см. спецификацию §4.2). Поэтому проверяем
        # АБСОЛЮТНОЕ изменение против верхней оценки интервала, а не скорость
        # изменения: делить на неизвестный интервал — значит мерить дрожание
        # доставки, что первая редакция и делала.
        allow = MAX_ACCEL * margin * ((t1 - t0) + DELIVERY_SLOP_S)
        d = abs(log[i][3] - log[i - 1][3])
        r = d / allow if allow > 0 else float("inf")
        if r > worst_ratio:
            worst_ratio, worst_d, worst_allow, worst_at = r, d, allow, (s0, s1)
    # Пар не нашлось — проверять было нечего, и говорить «без рывка» нельзя.
    ok = pairs > 0 and worst_ratio <= 1.0
    return {"сценарий": label, "пар": pairs,
            "макс_шаг_команды": round(worst_d, 4),
            "разрешено": round(worst_allow, 4),
            "занято_от_предела": round(worst_ratio, 3) if pairs else None,
            "без_рывка": ok, "кадров": len(log), "худшая_пара_seq": worst_at}


def bits(log, mask):
    return sum(1 for r in log if r[4] & mask)


def shaft_speed(log, sent_at, skip_s=1.5):
    """Фактическая скорость вала по углу из телеметрии, рад/с.

    Первые skip_s секунд отбрасываются: там идёт разгон рампой, и включать его
    в среднее значило бы занижать скорость тем сильнее, чем длиннее рампа.

    Время берётся по МОМЕНТУ ОТПРАВКИ запроса, а не приёма ответа: интервал
    доставки по Bluetooth гуляет на десятки миллисекунд, и по времени приёма
    скорость получилась бы с той же ошибкой. Момент отправки известен точно.
    """
    pts = []
    for r in log:
        t = sent_at.get(r[1])
        if t is not None:
            pts.append((t, r[2]))          # (время, угол)
    if len(pts) < 5:
        return None
    pts.sort()
    t0 = pts[0][0] + skip_s
    pts = [p for p in pts if p[0] >= t0]
    if len(pts) < 5:
        return None
    dt = pts[-1][0] - pts[0][0]
    if dt <= 0:
        return None
    return (pts[-1][1] - pts[0][1]) / dt


# ------------------------- сценарии -------------------------
def s_normal(lk, seq):
    """Штатный обмен: малый шаг уставки, рампа отрабатывает."""
    for i in range(30):
        lk.send((seq + i) & 0x7F, 0.3)
        lk.poll(0.1)
    return seq + 30


def s_late(lk, seq):
    """Кадр опоздал: пауза 116 мс — верхний хвост BT, измеренный с телефона."""
    for i in range(10):
        lk.send((seq + i) & 0x7F, 0.3)
        lk.poll(0.1 if i % 3 else 0.116)
    return seq + 10


def s_fade(lk, seq):
    """Замирание радио 150-300 мс: потолок экстраполяции держит ω константой."""
    lk.send(seq, 0.3, wdot=0.5)
    lk.poll(0.28)                      # молчим дольше потолка, но меньше watchdog
    lk.send((seq + 1) & 0x7F, 0.3, wdot=0.5)
    lk.poll(0.3)
    return seq + 2


def s_burst(lk, seq):
    """Пачка из 3 кадров после замирания: применяется только последний."""
    lk.poll(0.25)
    for k in range(3):
        lk.send((seq + k) & 0x7F, 0.1 * (k + 1))
    lk.poll(0.5)
    return seq + 3


def s_break(lk, seq):
    """Обрыв > 300 мс: watchdog, рампа к нулю, НЕ обрыв тока."""
    lk.send(seq, 0.5)
    lk.poll(0.3)
    lk.poll(1.2)                       # молчание
    lk.send((seq + 1) & 0x7F, 0.5)     # восстановление
    lk.poll(1.0)
    return seq + 2


def s_corrupt(lk, seq):
    """Битые байты: CRC режет, уставка прежняя."""
    lk.send(seq, 0.3)
    lk.poll(0.2)
    for k in range(5):
        lk.send((seq + 1 + k) & 0x7F, 2.0, corrupt=True)
        lk.poll(0.1)
    lk.poll(0.3)
    return seq + 6


def s_restart(lk, seq):
    """Перезапуск телефона: seq скачет, кадр принимается безусловно."""
    lk.send(seq, 0.3)
    lk.poll(0.3)
    lk.send((seq + 100) & 0x7F, 0.3)   # вне окна свежести
    lk.poll(0.5)
    return seq + 101


def s_wdot_huge(lk, seq):
    """ω̇ ошибочно большой: потолок ограничивает интеграл, кламп — исполнение.

    Кадры идут с интервалом 250 мс: дольше потолка 150 мс, но короче watchdog
    300 мс. Иначе наблюдать нечего — телеметрия приходит только в ответ, а в
    тишине watchdog обнуляет уставку раньше, чем потолок себя проявит.
    """
    for k in range(8):
        lk.send((seq + k) & 0x7F, 0.0, wdot=50.0)
        lk.poll(0.25)
    return seq + 8


def s_clamp(lk, seq):
    """Уставка выше предела: клампится с битом статуса И НА ВАЛУ.

    Удержание, а не один кадр: рампе нужно дойти до предела при ускорении
    1.0 рад/с^2, и всё это время связь обязана жить. Предел уставки после
    разбора дрожания снижен с 2.0 до 1.2 рад/с (граница срыва при 2.5 В
    лежит около 1.45), так что 2.5 с с запасом хватает.

    Проверка биту НЕ доверяет. Бит говорит лишь, что приёмник СЧИТАЕТ, будто
    ограничил; сломанный кламп поднимал бы бит и всё равно разгонял вал.
    Поэтому в разборе отдельно смотрится фактическая скорость вала по
    телеметрии — см. shaft_speed().
    """
    return lk.hold(seq, 5.0, 2.5)


SCENARIOS = [
    ("штатный", s_normal, {}),
    ("опоздание", s_late, {}),
    ("замирание", s_fade, {"ждём": ST_EXTRAP_CAP}),
    ("пачка", s_burst, {}),
    ("обрыв", s_break, {"ждём": ST_WATCHDOG}),
    ("битые байты", s_corrupt, {}),
    ("перезапуск", s_restart, {}),
    ("большой wdot", s_wdot_huge, {"ждём": ST_EXTRAP_CAP}),
    # SETPOINT_LIMIT прошивки = 1.2 рад/с (снижен с 2.0 после разбора
    # дрожания: граница срыва при 2.5 В лежит около 1.45).
    ("кламп", s_clamp, {"ждём": ST_CLAMP, "вал_не_выше": 1.2}),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mac", default="38:18:2B:30:7D:86")
    ap.add_argument("--scenario", default="all")
    ap.add_argument("--out", default="tools/link/stress.json")
    args = ap.parse_args()
    self_check(verbose=False)

    lk = Link(args.mac)
    results = []
    seq = 0
    try:
        for name, fn, exp in SCENARIOS:
            if args.scenario != "all" and args.scenario != name:
                continue
            # Приводим к нулю УДЕРЖАНИЕМ, а не молчанием, и только потом
            # начинаем считать: иначе в каждый сценарий попадал бы watchdog
            # от самой обвязки.
            seq = lk.hold(seq, 0.0, 0.8)
            start = len(lk.log)
            seq = fn(lk, seq) & 0x7F
            seq = lk.hold(seq, 0.0, 1.5)
            log = lk.log[start:]
            r = check_no_jerk(log, lk.sent_at, name)
            r["watchdog"] = bits(log, ST_WATCHDOG)
            r["потолок"] = bits(log, ST_EXTRAP_CAP)
            r["рампа_насыщена"] = bits(log, ST_RAMP_SAT)
            r["кламп"] = bits(log, ST_CLAMP)
            r["срыв"] = bits(log, ST_SLIP)
            r["w_ramp_макс"] = round(max((abs(x[3]) for x in log), default=0), 3)

            # Кламп проверяется НА ВАЛУ, а не по биту. Бит говорит лишь, что
            # приёмник считает, будто ограничил; сломанный кламп поднимал бы
            # бит и всё равно разгонял вал, и такая проверка была бы хуже
            # отсутствующей — она создавала бы видимость покрытия.
            if exp.get("вал_не_выше") is not None:
                sp = shaft_speed(log, lk.sent_at)
                r["скорость_вала"] = round(sp, 4) if sp is not None else None
                lim = exp["вал_не_выше"]
                r["предел_вала"] = lim
                # Допуск 15%: телеметрия идёт 10 Гц, угол медианно фильтрован,
                # и на коротком участке оценка скорости несёт свою ошибку.
                # Ловим разгон в разы, а не проценты.
                r["вал_в_пределе"] = (sp is not None and abs(sp) <= lim * 1.15)

            if "ждём" in exp:
                key = {ST_EXTRAP_CAP: "потолок", ST_WATCHDOG: "watchdog",
                       ST_CLAMP: "кламп"}[exp["ждём"]]
                r["ожидаемый_бит_поднялся"] = r[key] > 0
            results.append(r)
            print(json.dumps(r, ensure_ascii=False))
    finally:
        lk.send(seq, 0.0)
        lk.poll(1.5)
        lk.close()

    json.dump({"результаты": results,
                "лог": [{"t": round(t, 4), "seq": s, "theta": th,
                          "w_ramp": w, "st": st} for t, s, th, w, st in lk.log]},
               open(args.out, "w"), ensure_ascii=False)
    bad = [r for r in results if not r["без_рывка"]]
    miss = [r for r in results if r.get("ожидаемый_бит_поднялся") is False]
    shaft_bad = [r["сценарий"] for r in results
                  if r.get("вал_в_пределе") is False]
    if shaft_bad:
        print(f"ВАЛ ВЫШЕЛ ЗА ПРЕДЕЛ в сценариях: {shaft_bad}")
    print(f"\nсценариев {len(results)}, рывков {len(bad)}, "
          f"неподнятых ожидаемых битов {len(miss)}")
    return 1 if (bad or miss) else 0


if __name__ == "__main__":
    sys.exit(main())
