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
