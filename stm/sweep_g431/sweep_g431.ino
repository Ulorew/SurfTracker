/*
 * B-G431B-ESC1: развёртки для скользящего Фурье.
 *
 * Идея Hero, и она сильнее дискретной матрицы: если плавно менять скорость и
 * смотреть спектрограмму, две гипотезы разделяются глазом, без статистики.
 *
 *   зубцовый момент  -> частота ПРОПОРЦИОНАЛЬНА скорости. На спектрограмме
 *                       это лучи из начала координат, а в координатах
 *                       «циклов за оборот» — горизонтальные линии на целых
 *                       кратностях числа пар полюсов.
 *   резонанс         -> частота ПОСТОЯННА. Горизонталь в герцах и гипербола
 *                       в циклах за оборот.
 *
 * Дискретная матрица этого различить не смогла: по одной выборке на ячейку,
 * повторяемость неизвестна, и «сильнейший пик» скакал между гармониками.
 *
 * ДВЕ РАЗВЁРТКИ:
 *   A. скорость 0.05 -> 1.5 рад/с за 100 с при постоянных 2.0 В;
 *   B. напряжение 1.0 -> 3.0 В за 40 с при постоянной 1.0 рад/с.
 *
 * Развёртка B отвечает на отдельный вопрос: гармоники слабеют с напряжением
 * (тогда виноваты искажения мёртвого времени, чей вес падает как 1/U) или
 * стоят на месте (тогда чистый зубцовый момент).
 *
 * ПОТОКОМ, А НЕ В БУФЕР. 200 Гц по ~18 байт это 3.6 КБ/с, канал держит 11.5.
 * Буфер на 140 секунд занял бы 112 КБ при 32 КБ ОЗУ — не помещается вовсе.
 *
 * Формат строки: миллисекунды, угол, команда, напряжение. Всё, что нужно для
 * разбора, есть в самой строке: потребитель не должен восстанавливать
 * развёртку по номеру отсчёта.
 */
#include <SimpleFOC.h>

static const uint16_t FS_HZ = 200;

static const float    A_W0 = 0.05f, A_W1 = 1.5f;
static const uint32_t A_MS = 100000;
static const float    A_VOLTS = 2.0f;

static const float    B_V0 = 1.0f, B_V1 = 3.0f;
static const uint32_t B_MS = 40000;
static const float    B_W = 1.0f;

static const uint8_t POLE_PAIRS = 11;
static const float   SUPPLY_V   = 12.0f;
static const unsigned long SENS_MIN_US = 3;
static const unsigned long SENS_MAX_US = 919;

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

/** Крутит w и печатает отсчёты, пока не истечёт ms. w и volts — функции
 *  времени, поэтому передаются началом и концом, а не значением. */
static void sweep(float w0, float w1, float v0, float v1, uint32_t ms) {
  const uint32_t step_us = 1000000UL / FS_HZ;
  uint32_t t0 = millis();
  uint32_t next = micros();
  while (millis() - t0 < ms) {
    float u = (millis() - t0) / (float)ms;
    float w = w0 + (w1 - w0) * u;
    float v = v0 + (v1 - v0) * u;
    motor.voltage_limit = v;
    // Рампы здесь нет намеренно: уставка и так меняется медленно, а рампа
    // добавила бы своё запаздывание в то, что мы измеряем.
    motor.loopFOC();
    motor.move(w);
    sensor.update();
    if ((int32_t)(micros() - next) >= 0) {
      next += step_us;
      Serial.print(millis() - t0); Serial.print(',');
      Serial.print(sensor.getAngle(), 5); Serial.print(',');
      Serial.print(w, 4); Serial.print(',');
      Serial.println(v, 2);
    }
  }
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  blink(5, 80);

  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) { }
  Serial.println();
  Serial.println(F("=== развёртки для спектрограммы ==="));

  sensor.init();
  sensor.enableInterrupt(doPWM);

  driver.voltage_power_supply = SUPPLY_V;
  driver.voltage_limit = SUPPLY_V;
  driver.init();
  motor.linkDriver(&driver);
  motor.controller = MotionControlType::velocity_openloop;
  motor.voltage_limit = A_VOLTS;
  motor.init();

  // Разгон до начала развёртки A, чтобы первые секунды не содержали пуск.
  digitalWrite(LED_BUILTIN, HIGH);
  uint32_t ts = millis();
  while (millis() - ts < 2000) { motor.loopFOC(); motor.move(A_W0); sensor.update(); }

  Serial.print(F("#РАЗВЁРТКА A скорость ")); Serial.print(A_W0, 3);
  Serial.print(F("..")); Serial.print(A_W1, 3);
  Serial.print(F(" рад/с, напряжение ")); Serial.print(A_VOLTS, 2);
  Serial.print(F(" В, ")); Serial.print(A_MS / 1000); Serial.print(F(" с, fs="));
  Serial.println(FS_HZ);
  Serial.println(F("t_ms,theta,w_cmd,volts"));
  sweep(A_W0, A_W1, A_VOLTS, A_VOLTS, A_MS);
  Serial.println(F("#КОНЕЦ"));

  digitalWrite(LED_BUILTIN, LOW); delay(300); blink(2, 250);
  digitalWrite(LED_BUILTIN, HIGH);
  ts = millis();
  while (millis() - ts < 2000) { motor.loopFOC(); motor.move(B_W); sensor.update(); }

  Serial.print(F("#РАЗВЁРТКА B напряжение ")); Serial.print(B_V0, 2);
  Serial.print(F("..")); Serial.print(B_V1, 2);
  Serial.print(F(" В, скорость ")); Serial.print(B_W, 3);
  Serial.print(F(" рад/с, ")); Serial.print(B_MS / 1000); Serial.print(F(" с, fs="));
  Serial.println(FS_HZ);
  Serial.println(F("t_ms,theta,w_cmd,volts"));
  sweep(B_W, B_W, B_V0, B_V1, B_MS);
  Serial.println(F("#КОНЕЦ"));

  motor.move(0.0f);
  motor.disable();
  digitalWrite(LED_BUILTIN, LOW);
  Serial.println(F("#ВСЁ"));
  while (1) blink(1, 60);
}

void loop() { }
