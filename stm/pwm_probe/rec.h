// Формат записи лога — ОТДЕЛЬНЫМ ЗАГОЛОВКОМ, и это не вкусовщина.
//
// Arduino генерирует прототипы функций сам и вставляет их В НАЧАЛО файла,
// то есть ДО определения структур скетча. Функция с типом Rec в сигнатуре
// из-за этого не собирается: «'Rec' does not name a type». Заголовок
// включается препроцессором раньше прототипов, и проблема снимается.
#pragma once
#include <Arduino.h>

// Двоичный формат, а не текстовый: printf съел бы ровно те микросекунды,
// которые мы меряем. 26 байт на кадр при 1 кГц — 26 кБ/с.
struct __attribute__((packed)) Rec {
  uint16_t sync;        // 0xA55A
  uint16_t seq;
  uint32_t t_us;
  uint32_t cap_period;  // тики 170 МГц
  uint32_t cap_high;
  uint32_t isr_period;  // мкс
  uint32_t isr_high;    // мкс
  uint8_t  flags;       // b0 cap_bad, b1 isr_bad, b2 stale
  uint8_t  crc;         // сумма байт записи без crc
};

inline uint8_t rec_crc(const Rec &r) {
  const uint8_t *p = (const uint8_t*)&r;
  uint8_t s = 0;
  for (size_t i = 0; i < sizeof(Rec) - 1; i++) s += p[i];
  return s;
}
