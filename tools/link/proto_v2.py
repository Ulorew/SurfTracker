#!/usr/bin/env python3
"""Протокол v2: кадрирование, CRC и контрольные векторы — одной реализацией.

Общий модуль для клиента и тестов. Спецификация: docs/ПРОТОКОЛ_ТЕЛЕФОН_STM32.md

CRC другой, чем в v1 (там был CRC-8/ATM, полином 0x07). Векторы v1 непригодны.
"""
import struct

MAGIC_REQ, MAGIC_TEL = 0xA5, 0x5A
REQ_LEN, TEL_LEN = 11, 12
VERSION = 0

# --- СТЕНД: отдельные типы пакетов, боевой путь не тронут -------------------
# Длина CMD равна REQ намеренно: приёмник накапливает кадр одинаково и
# различает типы только по первому байту.
MAGIC_CMD, MAGIC_DIAG = 0xC3, 0x5C
CMD_LEN, DIAG_LEN = 11, 24

CMD_MODE, CMD_RAW, CMD_DIAG, CMD_VOLT, CMD_SPIN = 0x01, 0x02, 0x03, 0x04, 0x05
MODE_FIGHT, MODE_HOLD, MODE_SPIN = 0, 1, 2

# Флаги диагностики
DF_CAP_BAD  = 1 << 0    # захват не дал годного кадра
DF_ISR_BAD  = 1 << 1    # импульс вне границ датчика
DF_CAP_LOST = 1 << 2    # CC1OF: кадр пропущен аппаратно
DF_NO_EDGES = 1 << 3    # фронтов с прошлой записи НЕ БЫЛО — тракт мёртв

ST_WATCHDOG   = 1 << 0
ST_EXTRAP_CAP = 1 << 1
ST_RAMP_SAT   = 1 << 2
ST_ENC_OK     = 1 << 3
ST_CLAMP      = 1 << 4
ST_SLIP       = 1 << 5
ST_CRC_SHIFT  = 6          # биты 6-7: счётчик CRC-ошибок по модулю 4


def crc8(data):
    """CRC-8/MAXIM: полином 0x31 отражённый (0x8C с младшего бита), init 0."""
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0x8C if c & 1 else c >> 1
    return c


def build_req(seq, omega, omega_dot, version=VERSION):
    body = bytes([MAGIC_REQ, ((version & 1) << 7) | (seq & 0x7F)])
    body += struct.pack("<ff", omega, omega_dot)
    return body + bytes([crc8(body)])


def parse_tel(buf):
    """-> (seq, theta, omega_ramp, status) | None. Строго как в прошивке."""
    if len(buf) != TEL_LEN or buf[0] != MAGIC_TEL or crc8(buf[:-1]) != buf[-1]:
        return None
    th, wr = struct.unpack("<ff", buf[2:10])
    return buf[1] & 0x7F, th, wr, buf[10]


def build_cmd(seq, code, param, version=VERSION):
    """Команда стенда. Телефон её не шлёт никогда — только стендовый скрипт."""
    body = bytes([MAGIC_CMD, ((version & 1) << 7) | (seq & 0x7F), code])
    body += struct.pack("<f", param) + bytes(3)      # резерв под будущее
    return body + bytes([crc8(body)])


def parse_diag(buf):
    """-> dict | None. СЫРЫЕ величины обоих трактов, угол НЕ посчитан.

    Угол сознательно не считается на плате: формулу применяет разборщик, и
    тогда видно, какой именно формулой получено число, а поменять её можно
    без перепрошивки.

    ВНИМАНИЕ на поле isr_period: прошивка период ISR-тракта НЕ МЕРЯЕТ (боевой
    MagneticSensorPWM его не использует), и на этом месте идёт ВОЗРАСТ
    ЗАХВАТА в тиках TIM2 — сколько прошло с начала кадра. Имя поля осталось
    от раскладки пакета; читать его как период нельзя.
    """
    if len(buf) != DIAG_LEN or buf[0] != MAGIC_DIAG or crc8(buf[:-1]) != buf[-1]:
        return None
    t_us, isr_high, cap_age, cap_high, cap_period = struct.unpack("<IIIII", buf[2:22])
    return dict(seq=buf[1] & 0x7F, t_us=t_us, isr_high=isr_high,
                cap_age=cap_age, cap_high=cap_high, cap_period=cap_period,
                flags=buf[22])


def build_tel(seq, theta, omega_ramp, status):
    """Только для тестов и заглушек: телеметрию строит STM."""
    body = bytes([MAGIC_TEL, seq & 0x7F]) + struct.pack("<ff", theta, omega_ramp)
    body += bytes([status])
    return body + bytes([crc8(body)])


# Контрольные векторы из спецификации. Обе стороны обязаны пройти их ДО
# первого живого обмена: расхождение таблиц CRC выглядит как «канал молчит».
VECTORS_REQ = [
    ((0, 0.0, 0.0), "A5 00 00 00 00 00 00 00 00 00 17"),
    ((1, 1.0, 0.1), "A5 01 00 00 80 3F CD CC CC 3D 44"),
    ((127, -2.0, 0.5), "A5 7F 00 00 00 C0 00 00 00 3F CD"),
]
VECTORS_TEL = [
    ((0, 0.0, 0.0, 0x08), "5A 00 00 00 00 00 00 00 00 00 08 D5"),
    ((42, 0.1745329, 0.5, 0x2A), "5A 2A C1 B8 32 3E 00 00 00 3F 2A 43"),
    ((127, -1.0, 0.0, 0x19), "5A 7F 00 00 80 BF 00 00 00 00 19 21"),
]


def self_check(verbose=True):
    assert crc8(b"123456789") == 0xA1, "реализация CRC-8/MAXIM неверна"
    for args, want in VECTORS_REQ:
        got = " ".join(f"{x:02X}" for x in build_req(*args))
        assert got == want, f"уставка {args}: {got} вместо {want}"
    for args, want in VECTORS_TEL:
        got = " ".join(f"{x:02X}" for x in build_tel(*args))
        assert got == want, f"телеметрия {args}: {got} вместо {want}"
        assert parse_tel(build_tel(*args)) is not None, "свой же кадр не разобрался"
    if verbose:
        print("векторы протокола v2 сошлись")
    return True


if __name__ == "__main__":
    self_check()
