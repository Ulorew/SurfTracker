/*
 * Протокол v2: кадрирование и CRC. ОДНА реализация на боевую прошивку и на
 * тест векторов — иначе тест проверял бы не тот код, который поедет.
 *
 * Спецификация: docs/ПРОТОКОЛ_ТЕЛЕФОН_STM32.md
 *
 * CRC-8/MAXIM, полином 0x31 ОТРАЖЁННЫЙ (0x8C при счёте с младшего бита).
 * Это ДРУГОЙ CRC, чем в v1 (там 0x07, без отражения). Отражение — то место,
 * где табличная реализация на хосте и побитовая на MCU расходятся чаще
 * всего, поэтому векторы гоняются на целевом железе, а не только на хосте.
 */
#pragma once
#include <stdint.h>
#include <string.h>

namespace proto {

static const uint8_t MAGIC_REQ = 0xA5;
static const uint8_t MAGIC_TEL = 0x5A;
static const uint8_t REQ_LEN   = 11;
static const uint8_t TEL_LEN   = 12;
static const uint8_t VERSION   = 0;

// ---------------------------------------------------------------- СТЕНД
//
// ОТДЕЛЬНЫЕ ТИПЫ ПАКЕТОВ, а не новые поля в существующих. Боевой путь
// «уставка -> телеметрия» не тронут ни на бит: прошивка со стендовыми
// командами отвечает старому телефону ровно так же, как отвечала, а телефон
// без стенда никогда не пришлёт CMD. Это единственный способ иметь ОДНУ
// прошивку на бой и на замеры, не заводя ветку, которая разойдётся.
//
// Длина CMD совпадает с REQ (11 байт) намеренно: приёмник накапливает кадр
// одинаково, различая типы только по первому байту.
static const uint8_t MAGIC_CMD  = 0xC3;
static const uint8_t MAGIC_DIAG = 0x5C;
static const uint8_t CMD_LEN    = 11;
static const uint8_t DIAG_LEN   = 24;

// Коды команд. Параметр — float, смысл зависит от кода.
static const uint8_t CMD_MODE = 0x01;   // 0 бой, 1 удержание, 2 вращение
static const uint8_t CMD_RAW  = 0x02;   // 1 = слать в телеметрию СЫРОЙ угол
static const uint8_t CMD_DIAG = 0x03;   // 1 = отвечать диагностикой вместо телеметрии
static const uint8_t CMD_VOLT = 0x04;   // напряжение стенда, В (жёстко ограничено)
static const uint8_t CMD_SPIN = 0x05;   // скорость для MODE_SPIN, рад/с

static const uint8_t MODE_FIGHT = 0;    // боевой: как будто стенда нет
static const uint8_t MODE_HOLD  = 1;    // поле стоит, ток течёт, вал удерживается
static const uint8_t MODE_SPIN  = 2;    // вращение с заданной скоростью

// Биты статуса. Свободных НЕТ: заняты все восемь.
static const uint8_t ST_WATCHDOG   = 1 << 0;
static const uint8_t ST_EXTRAP_CAP = 1 << 1;
static const uint8_t ST_RAMP_SAT   = 1 << 2;
static const uint8_t ST_ENC_OK     = 1 << 3;
static const uint8_t ST_CLAMP      = 1 << 4;
static const uint8_t ST_SLIP       = 1 << 5;
static const uint8_t ST_CRC_SHIFT  = 6;      // биты 6-7, счётчик по модулю 4

inline uint8_t crc8(const uint8_t *d, uint8_t n) {
  uint8_t c = 0x00;
  while (n--) {
    c ^= *d++;
    for (uint8_t i = 0; i < 8; i++)
      c = (c & 1) ? (uint8_t)((c >> 1) ^ 0x8C) : (uint8_t)(c >> 1);
  }
  return c;
}

/** Собрать кадр уставки. Нужен только тестам и заглушкам: на STM он
 *  разбирается, а не строится. */
inline void buildReq(uint8_t *out, uint8_t seq, float w, float wdot,
                      uint8_t version = VERSION) {
  out[0] = MAGIC_REQ;
  out[1] = (uint8_t)(((version & 1) << 7) | (seq & 0x7F));
  memcpy(out + 2, &w, 4);
  memcpy(out + 6, &wdot, 4);
  out[REQ_LEN - 1] = crc8(out, REQ_LEN - 1);
}

/** Разобрать кадр уставки. -> true, если магик и CRC сошлись. */
inline bool parseReq(const uint8_t *f, uint8_t *seq, float *w, float *wdot,
                      uint8_t *version = 0) {
  if (f[0] != MAGIC_REQ) return false;
  if (crc8(f, REQ_LEN - 1) != f[REQ_LEN - 1]) return false;
  if (version) *version = (uint8_t)(f[1] >> 7);
  *seq = (uint8_t)(f[1] & 0x7F);
  memcpy(w, f + 2, 4);
  memcpy(wdot, f + 6, 4);
  return true;
}

// ---- команда стенда --------------------------------------------------------
inline void buildCmd(uint8_t *out, uint8_t seq, uint8_t code, float param,
                      uint8_t version = VERSION) {
  out[0] = MAGIC_CMD;
  out[1] = (uint8_t)(((version & 1) << 7) | (seq & 0x7F));
  out[2] = code;
  memcpy(out + 3, &param, 4);
  out[7] = 0; out[8] = 0; out[9] = 0;          // резерв под будущее
  out[CMD_LEN - 1] = crc8(out, CMD_LEN - 1);
}

inline bool parseCmd(const uint8_t *f, uint8_t *seq, uint8_t *code, float *param,
                      uint8_t *version = nullptr) {
  if (f[0] != MAGIC_CMD) return false;
  if (crc8(f, CMD_LEN - 1) != f[CMD_LEN - 1]) return false;
  if (seq)  *seq  = f[1] & 0x7F;
  if (version) *version = (f[1] >> 7) & 1;
  if (code) *code = f[2];
  if (param) memcpy(param, f + 3, 4);
  return true;
}

// ---- диагностика: СЫРЫЕ величины обоих трактов -----------------------------
//
// Угол здесь НЕ считается: идут тики захвата и микросекунды прерывания как
// есть. Пусть формулу применяет разборщик на большой машине — тогда видно,
// какой именно формулой получено число, и её можно поменять, не перешивая
// плату. Плюс сравнение трактов остаётся честным: обе величины сняты в одном
// проходе, из одного кадра.
inline void buildDiag(uint8_t *out, uint8_t seq, uint32_t t_us,
                       uint32_t isr_high, uint32_t isr_period,
                       uint32_t cap_high, uint32_t cap_period, uint8_t flags) {
  out[0] = MAGIC_DIAG;
  out[1] = seq & 0x7F;
  memcpy(out + 2,  &t_us, 4);
  memcpy(out + 6,  &isr_high, 4);
  memcpy(out + 10, &isr_period, 4);
  memcpy(out + 14, &cap_high, 4);
  memcpy(out + 18, &cap_period, 4);
  out[22] = flags;
  out[DIAG_LEN - 1] = crc8(out, DIAG_LEN - 1);
}

inline void buildTel(uint8_t *out, uint8_t seq, float theta, float w_ramp,
                      uint8_t status) {
  out[0] = MAGIC_TEL;
  out[1] = (uint8_t)(seq & 0x7F);
  memcpy(out + 2, &theta, 4);
  memcpy(out + 6, &w_ramp, 4);
  out[10] = status;
  out[TEL_LEN - 1] = crc8(out, TEL_LEN - 1);
}

/**
 * Свежесть seq по модулю 128, окно вперёд 1..64.
 * Пачка после замирания: применяется только последний кадр, остальные
 * устаревшие. Окно ровно половина периода — иначе «вперёд» и «назад»
 * неразличимы.
 */
inline bool isFresher(uint8_t seq, uint8_t last) {
  uint8_t d = (uint8_t)((seq - last) & 0x7F);
  return d >= 1 && d <= 64;
}

}  // namespace proto
