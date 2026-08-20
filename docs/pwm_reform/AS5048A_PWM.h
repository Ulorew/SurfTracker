// AS5048A_PWM.h
// Чтение AS5048A по PWM на STM32G431 (B-G431B-ESC1), три режима для сравнения.
//
//   MODE_NAIVE   — прерывание + micros(), только длительность импульса,
//                  масштаб из калибровочных min/max. Это "как было".
//   MODE_ISR     — прерывание + micros(), но с измерением полного периода.
//                  Отделяет вклад ошибки масштаба от вклада дрожания.
//   MODE_CAPTURE — аппаратный захват TIM2 в режиме PWM input на PA15. Это "как стало".
//
// Сигнал энкодера должен быть подан одновременно на PB8 (для ISR-режимов)
// и на PA15 (для захвата). Один выход на два входа — это нормально.

#pragma once
#include <Arduino.h>
#include <SimpleFOC.h>

// ---------------------------------------------------------------- константы

// Кадр AS5048A: init 12 + error 4 + data 4095 + exit 8 = 4119 тактов.
// Высокий уровень держится (16 + data) тактов, поэтому:
//     data = duty * 4119 - 16
#define AS5048_FRAME_CLK   4119.0f
#define AS5048_OFFSET_CLK    16.0f
#define AS5048_SPAN        4096.0f

// Тактовая TIM2 на G431 = 170 МГц при PSC = 0.
#define TIM2_HZ            170000000UL

// Допустимый разброс периода кадра: паспорт ±10%, берём с запасом.
#define FRAME_US_MIN        800UL
#define FRAME_US_MAX       1250UL

// ------------------------------------------------------- сырой ISR-приёмник

namespace pwm_isr {
  volatile uint32_t t_rise = 0;
  volatile uint32_t period_us = 1000;
  volatile uint32_t high_us   = 0;
  volatile uint32_t edges     = 0;
  int pin = PB8;

  inline void onEdge() {
    uint32_t now = micros();
    if (digitalRead(pin)) {          // нарастающий фронт = начало кадра
      period_us = now - t_rise;
      t_rise    = now;
    } else {                          // спадающий = конец импульса
      high_us   = now - t_rise;
    }
    edges++;
  }

  inline void begin(int p) {
    pin = p;
    pinMode(pin, INPUT);
    attachInterrupt(digitalPinToInterrupt(pin), onEdge, CHANGE);
  }
}

// -------------------------------------------- аппаратный захват TIM2 / PA15

namespace pwm_cap {

  // Режим PWM input: сигнал заходит на TI1, внутри разводится на IC1 и IC2.
  //   IC1 — нарастающий фронт, сбрасывает счётчик, CCR1 = период кадра
  //   IC2 — спадающий фронт,                        CCR2 = длительность импульса
  // Оба значения аппаратные, задержка обработчика на точность не влияет.
  inline void begin() {
    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_TIM2_CLK_ENABLE();

    // PA15 после сброса отдан под JTDI. Отладка по SWD (PA13/PA14) не страдает,
    // но пин нужно явно перевести в альтернативную функцию AF1 = TIM2_CH1.
    GPIO_InitTypeDef g = {0};
    g.Pin       = GPIO_PIN_15;
    g.Mode      = GPIO_MODE_AF_PP;
    g.Pull      = GPIO_NOPULL;
    g.Speed     = GPIO_SPEED_FREQ_HIGH;
    g.Alternate = GPIO_AF1_TIM2;
    HAL_GPIO_Init(GPIOA, &g);

    TIM2->CR1 = 0;
    TIM2->PSC = 0;              // 170 МГц, шаг 5.9 нс
    TIM2->ARR = 0xFFFFFFFF;     // TIM2 32-битный, кадр в 1 мс не переполнит

    // CC1S=01 (IC1<-TI1), IC1F=0010 (фильтр N=4)
    // CC2S=10 (IC2<-TI1), IC2F=0010
    // Фильтр задерживает оба фронта одинаково, на отношение не влияет.
    TIM2->CCMR1 = (1u << 0) | (2u << 4) | (2u << 8) | (2u << 12);

    // CC1 по нарастающему, CC2 по спадающему
    TIM2->CCER = TIM_CCER_CC1E | TIM_CCER_CC2E | TIM_CCER_CC2P;

    // TS=00101 (TI1FP1), SMS=0100 (reset mode)
    TIM2->SMCR = (5u << TIM_SMCR_TS_Pos) | (4u << TIM_SMCR_SMS_Pos);

    TIM2->CR1 |= TIM_CR1_CEN;
  }

  // Возвращает false, если свежего кадра не было или кадр битый.
  inline bool read(uint32_t &period, uint32_t &high, bool &fresh) {
    fresh = (TIM2->SR & TIM_SR_CC1IF) != 0;   // чтение CCR1 ниже сбросит флаг

    uint32_t p1 = TIM2->CCR1;
    uint32_t h  = TIM2->CCR2;
    uint32_t p2 = TIM2->CCR1;
    if (p1 != p2) {            // граница кадра попала между чтениями
      p1 = TIM2->CCR1;
      h  = TIM2->CCR2;
    }
    period = p1;
    high   = h;

    uint32_t lo = FRAME_US_MIN * (TIM2_HZ / 1000000UL);
    uint32_t hi = FRAME_US_MAX * (TIM2_HZ / 1000000UL);
    return (period > lo) && (period < hi) && (high > 0) && (high < period);
  }
}

// ----------------------------------------------------- класс датчика SimpleFOC

enum PwmMode { MODE_NAIVE, MODE_ISR, MODE_CAPTURE };

class AS5048A_PWM : public Sensor {
public:
  AS5048A_PWM(PwmMode m = MODE_CAPTURE) : mode(m) {}

  // Калибровка для MODE_NAIVE: длительность импульса в мкс на краях диапазона.
  // Номинал при кадре 1000 мкс: min = 16/4119*1000 = 3.9, max = 4111/4119*1000 = 998.
  // Реальные значения снять примером find_raw_min_max.
  float naive_min_us = 3.9f;
  float naive_max_us = 998.0f;

  // Подгоняемые константы кадра. Номинал = 4119 / 16, уточняются тестом 2.
  float frame_clk  = AS5048_FRAME_CLK;
  float offset_clk = AS5048_OFFSET_CLK;

  uint32_t bad_frames = 0;
  uint32_t stale      = 0;

  void init() {
    if (mode == MODE_CAPTURE) pwm_cap::begin();
    else                      pwm_isr::begin(PB8);
    this->Sensor::init();
  }

  float getSensorAngle() override {
    float raw;

    if (mode == MODE_CAPTURE) {
      uint32_t p, h; bool fresh;
      if (!pwm_cap::read(p, h, fresh)) { bad_frames++; return last; }
      if (!fresh) stale++;
      raw = (float)h * frame_clk / (float)p - offset_clk;

    } else if (mode == MODE_ISR) {
      noInterrupts();
      uint32_t p = pwm_isr::period_us, h = pwm_isr::high_us;
      interrupts();
      if (p < FRAME_US_MIN || p > FRAME_US_MAX || h == 0 || h >= p) {
        bad_frames++; return last;
      }
      raw = (float)h * frame_clk / (float)p - offset_clk;

    } else {  // MODE_NAIVE — период игнорируется, масштаб из калибровки
      noInterrupts();
      uint32_t h = pwm_isr::high_us;
      interrupts();
      raw = ((float)h - naive_min_us) * AS5048_SPAN
            / (naive_max_us - naive_min_us);
    }

    if (raw < 0.0f)          raw = 0.0f;
    if (raw > AS5048_SPAN-1) raw = AS5048_SPAN - 1.0f;

    last = raw * (_2PI / AS5048_SPAN);
    return last;
  }

  PwmMode mode;
private:
  float last = 0.0f;
};

// ----------------------------------------------------------- формат лога

// Пишется в двоичном виде, по одной записи на итерацию цикла логирования.
// Оба тракта читаются одновременно, поэтому отсчёты строго парные:
// разница между методами не смешана с разницей между прогонами.
struct __attribute__((packed)) PwmRec {
  uint16_t sync;         // 0xA55A
  uint16_t seq;
  uint32_t t_us;
  uint32_t cap_period;   // тики 170 МГц
  uint32_t cap_high;
  uint32_t isr_period;   // мкс
  uint32_t isr_high;     // мкс
  float    ref;          // опорная величина: заданный угол, ток, что угодно
  uint16_t flags;        // b0 cap_bad, b1 isr_bad, b2 stale, b3 motor_on
  uint16_t crc;          // сумма байт записи без crc
};

inline uint16_t pwm_crc(const PwmRec &r) {
  const uint8_t *p = (const uint8_t*)&r;
  uint16_t s = 0;
  for (size_t i = 0; i < sizeof(PwmRec) - 2; i++) s += p[i];
  return s;
}
