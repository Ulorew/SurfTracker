/*
 * B-G431B-ESC1: сбор данных для компенсации зубцового момента.
 *
 * Инструкция: reports/АНТИЗУБЦЫ.md, шаги 1-2.
 *
 * ГЛАВНОЕ РЕШЕНИЕ — ПРИВЯЗКА НУЛЯ φ_эл (§3.1 инструкции, раздел «грабли»).
 *
 * Таблица, снятая с одним нулём и применённая с другим, не просто не помогает
 * — на сдвиге в полпериода она УДВАИВАЕТ дрожание. Поэтому:
 *
 *   φ_эл = fmod(sensor.getMechanicalAngle() * POLE_PAIRS, 2π)
 *
 * Именно getMechanicalAngle(), а НЕ getAngle(). Разница решающая:
 *   getAngle()          — накопленный угол ОТ ВКЛЮЧЕНИЯ. Его ноль плавает от
 *                         запуска к запуску, и таблица протухает молча.
 *   getMechanicalAngle()— абсолютный угол внутри оборота, от собственного нуля
 *                         AS5048. Датчик абсолютный, значит ноль один и тот же
 *                         при каждом включении, пока не переставили магнит.
 *
 * Эту же формулу обязана считать боевая прошивка. Не «такую же» — ту же, из
 * общего заголовка, иначе расхождение появится при первой же правке.
 *
 * ПОТОКОМ, а не в буфер: 20 оборотов на 0.15 рад/с это 14 минут, при 50 Гц
 * 42 тысячи отсчётов — в 32 КБ ОЗУ не помещается даже близко. Канал держит
 * 11.5 КБ/с, поток занимает 1.5.
 *
 * НАПРЯЖЕНИЕ. По правилу владельца: долгая непрерывная работа — не выше 2.0 В.
 * Сбор занимает 28 минут, поэтому основная калибровка идёт на 2.0 В. Сверка по
 * §5 на 2.5 В — отдельным укороченным прогоном, до 10 минут.
 * Скетч ОГРАНИЧЕН ПО ВРЕМЕНИ и по окончании снимает напряжение сам: оставлять
 * мотор под током после конца работы нельзя, а полагаться на то, что кто-то
 * вспомнит, — тем более.
 */
#include <SimpleFOC.h>

// ---- параметры прогона ----
#ifndef CAL_VOLTS
#define CAL_VOLTS 2.0f
#endif
#ifndef CAL_REVS
#define CAL_REVS 20
#endif
static const float    CAL_W    = 0.15f;      // рад/с, §1.1
static const uint16_t FS_HZ    = 50;
static const uint32_t HARD_LIMIT_MS = 40UL * 60UL * 1000UL;   // страховка

static const uint8_t POLE_PAIRS = 11;
static const float   SUPPLY_V   = 12.0f;
static const unsigned long SENS_MIN_US = 3;
static const unsigned long SENS_MAX_US = 919;
static const float TWO_PI_F = 6.28318530718f;

MagneticSensorPWM sensor = MagneticSensorPWM(PB8, SENS_MIN_US, SENS_MAX_US);
void doPWM() { sensor.handlePWM(); }

BLDCMotor      motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver6PWM driver = BLDCDriver6PWM(A_PHASE_UH, A_PHASE_UL,
                                       A_PHASE_VH, A_PHASE_VL,
                                       A_PHASE_WH, A_PHASE_WL);

static void blink(uint8_t n, uint16_t ms) {
  for (uint8_t i = 0; i < n; i++) {
    digitalWrite(LED_BUILTIN, HIGH); delay(ms);
    digitalWrite(LED_BUILTIN, LOW);  delay(ms);
  }
}

/** φ_эл по АБСОЛЮТНОМУ углу датчика. Та же формула пойдёт в боевую прошивку. */
static inline float phiEl() {
  float m = sensor.getMechanicalAngle();          // [0, 2π) от нуля AS5048
  float p = fmodf(m * POLE_PAIRS, TWO_PI_F);
  return p < 0 ? p + TWO_PI_F : p;
}

/** Крутит заданное число оборотов на скорости w, печатая поток. */
static void run_dir(float w, int revs, const char *label) {
  const uint32_t step_us = 1000000UL / FS_HZ;
  const float target_rad = revs * TWO_PI_F;

  Serial.print(F("#НАПРАВЛЕНИЕ ")); Serial.print(label);
  Serial.print(F(" w=")); Serial.print(w, 4);
  Serial.print(F(" оборотов=")); Serial.print(revs);
  Serial.print(F(" volts=")); Serial.print(CAL_VOLTS, 2);
  Serial.print(F(" fs=")); Serial.println(FS_HZ);
  Serial.println(F("t_ms,theta_acc,phi_el,w_cmd"));

  float w_ramp = 0.0f;
  uint32_t prev = micros();
  uint32_t t0 = millis();
  uint32_t next = micros();
  float a0 = sensor.getAngle();
  bool started = false;

  while (true) {
    uint32_t now = micros();
    float dt = (now - prev) * 1e-6f; prev = now;
    if (dt < 0 || dt > 0.05f) dt = 0;
    float st = 1.0f * dt;                    // рампа 1 рад/с^2
    if (w_ramp < w) w_ramp = min(w_ramp + st, w);
    else if (w_ramp > w) w_ramp = max(w_ramp - st, w);

    motor.loopFOC();
    motor.move(w_ramp);
    sensor.update();

    // Отсчёт пути начинаем ПОСЛЕ выхода рампы на полку: иначе разгон съедает
    // часть оборотов и усреднение выходит меньше заявленного.
    if (!started && fabsf(w_ramp - w) < 1e-4f) {
      started = true;
      a0 = sensor.getAngle();
    }

    if ((int32_t)(micros() - next) >= 0) {
      next += step_us;
      Serial.print(millis() - t0); Serial.print(',');
      Serial.print(sensor.getAngle(), 5); Serial.print(',');
      Serial.print(phiEl(), 5); Serial.print(',');
      Serial.println(w_ramp, 4);
    }

    if (started && fabsf(sensor.getAngle() - a0) >= target_rad) break;
    if (millis() - t0 > HARD_LIMIT_MS) {
      Serial.println(F("#СТОП по жёсткому лимиту времени"));
      break;
    }
  }
  // Тормозим рампой, а не обрывом: скачок уставки сорвал бы синхронизм и
  // испортил бы конец записи.
  while (fabsf(w_ramp) > 1e-3f) {
    uint32_t now = micros();
    float dt = (now - prev) * 1e-6f; prev = now;
    if (dt < 0 || dt > 0.05f) dt = 0;
    float st = 1.0f * dt;
    if (w_ramp > 0) w_ramp = max(w_ramp - st, 0.0f);
    else w_ramp = min(w_ramp + st, 0.0f);
    motor.loopFOC(); motor.move(w_ramp); sensor.update();
  }
  Serial.println(F("#КОНЕЦ"));
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  blink(5, 80);

  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) { }
  Serial.println();
  Serial.println(F("=== калибровка зубцового момента: сбор данных ==="));
  Serial.print(F("напряжение ")); Serial.print(CAL_VOLTS, 2);
  Serial.print(F(" В, скорость ")); Serial.print(CAL_W, 3);
  Serial.print(F(" рад/с, оборотов ")); Serial.print(CAL_REVS);
  Serial.println(F(" в каждую сторону"));
  Serial.print(F("ожидаемая длительность ~"));
  Serial.print((int)(2.0f * CAL_REVS * TWO_PI_F / CAL_W / 60.0f));
  Serial.println(F(" мин"));

  sensor.init();
  sensor.enableInterrupt(doPWM);

  driver.voltage_power_supply = SUPPLY_V;
  driver.voltage_limit = SUPPLY_V;
  driver.init();
  motor.linkDriver(&driver);
  motor.controller = MotionControlType::velocity_openloop;
  motor.voltage_limit = CAL_VOLTS;
  motor.init();

  digitalWrite(LED_BUILTIN, HIGH);
  run_dir(+CAL_W, CAL_REVS, "ВПЕРЁД");

  // Пауза между направлениями: дать мотору отдать тепло и не смешивать
  // разогрев с эффектом направления.
  digitalWrite(LED_BUILTIN, LOW);
  Serial.println(F("#ПАУЗА 30 с"));
  uint32_t tp = millis();
  while (millis() - tp < 30000) { motor.loopFOC(); motor.move(0.0f); sensor.update(); }

  blink(2, 300);
  run_dir(-CAL_W, CAL_REVS, "НАЗАД");

  // ОБЯЗАТЕЛЬНО: снять напряжение по окончании. Правило владельца — не
  // оставлять мотор под током после работы, и полагаться на то, что кто-то
  // вспомнит, нельзя.
  motor.move(0.0f);
  motor.voltage_limit = 0.0f;
  motor.disable();
  Serial.println(F("#ВСЁ напряжение снято"));
  while (1) blink(1, 60);
}

void loop() { }
