#ifndef CAPTURE_SENSOR_HOST_H
#define CAPTURE_SENSOR_HOST_H
/*
 * Заглушки для сборки CaptureSensor обычным g++ на ноутбуке.
 *
 * ПОДМЕНЯЕТСЯ РОВНО ДВА: регистры таймера и базовый класс Sensor. Сам
 * CaptureSensor.cpp компилируется БЕЗ ЕДИНОЙ ПРАВКИ — иначе стенд проверял бы
 * не тот код, который поедет на плату.
 *
 * РЕГИСТР СЧИТАЕТ ОБРАЩЕНИЯ. HostReg ведёт себя как uint32_t (неявное
 * преобразование при чтении, присваивание при записи), но пересчитывает
 * каждое обращение. Это даёт стенду проверить требование «в контуре ждать в
 * цикле нельзя»: чтение угла обязано стоить ФИКСИРОВАННОЕ число обращений к
 * регистрам, и любой цикл ожидания это число взорвёт.
 */
#include <stdint.h>
#include <stddef.h>

struct HostReg {
  uint32_t v = 0;
  static unsigned long reads;
  static unsigned long writes;
  operator uint32_t() const { reads++; return v; }
  HostReg &operator=(uint32_t x) { writes++; v = x; return *this; }
  HostReg &operator|=(uint32_t x) { reads++; writes++; v |= x; return *this; }
  // Подложка ставит значения этим методом: он счётчики НЕ трогает, потому что
  // это действие стенда, а не проверяемого кода.
  void set(uint32_t x) { v = x; }
  uint32_t get() const { return v; }
};

// Раскладка полей неважна: код обращается к ним по именам. Важно, что имена и
// смысл те же, что у настоящего TIM_TypeDef.
struct TIM_TypeDef {
  HostReg CR1, SR, CCMR1, CCER, SMCR, PSC, ARR, CCR1, CCR2, CNT;
};

// Биты — с теми же позициями, что в CMSIS STM32G431 (сверено по
// .arduino15/.../Include/stm32g431xx.h).
#define TIM_CR1_CEN        (1U << 0)
#define TIM_SR_CC2IF       (1U << 2)
#define TIM_SR_CC1OF       (1U << 9)
#define TIM_CCER_CC1E      (1U << 0)
#define TIM_CCER_CC2E      (1U << 4)
#define TIM_CCER_CC2P      (1U << 5)
#define TIM_SMCR_SMS_Pos   0U
#define TIM_SMCR_TS_Pos    4U

// На хосте таймера по умолчанию нет: экземпляр подаёт стенд.
#define CAPSENS_DEFAULT_TIM nullptr

// ---- базовый класс SimpleFOC ----------------------------------------------
//
// Повторяет форму настоящего Sensor из
// ~/Arduino/libraries/Simple_FOC/src/common/base_classes/Sensor.h: тот же
// чисто виртуальный getSensorAngle(), тот же init(), дёргающий его четырежды.
// Скорость и обороты стенду не нужны и не воспроизводятся — проверяется
// getSensorAngle() и годность, а не арифметика базового класса.
enum Direction : int8_t { CW = 1, CCW = -1, UNKNOWN = 0 };

class Sensor {
 public:
  virtual ~Sensor() {}
  virtual int needsSearch() { return 0; }
  float min_elapsed_time = 0.0001f;

 protected:
  virtual float getSensorAngle() = 0;
  virtual void init() {
    getSensorAngle();
    vel_angle_prev = getSensorAngle();
    getSensorAngle();
    angle_prev = getSensorAngle();
  }
  float velocity = 0.0f;
  float angle_prev = 0.0f;
  long angle_prev_ts = 0;
  float vel_angle_prev = 0.0f;
  long vel_angle_prev_ts = 0;
  int32_t full_rotations = 0;
  int32_t vel_full_rotations = 0;
};

#endif  // CAPTURE_SENSOR_HOST_H
