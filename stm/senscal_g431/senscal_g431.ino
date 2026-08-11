/*
 * B-G431B-ESC1: калибровка САМОГО ДАТЧИКА по механическому углу.
 *
 * Чиним прибор, а не объект. Развилка показала, что доминирующая часть
 * измеряемого «дрожания» — не движение вала, а ошибка отображения
 * «длительность импульса -> угол»: первый порядок около 1.1 град НЕ падает с
 * ростом напряжения, а настоящий момент обязан падать обратно жёсткости
 * (22-й порядок это и делает: 0.661 при предсказанных 0.667).
 *
 * МЕТОД. При равномерном вращении истинный угол линеен по ВРЕМЕНИ. Значит
 * отклонение показания датчика от прямой и есть искомая поправка — ничего
 * дополнительно измерять не нужно, эталоном служит само равномерное движение
 * поля в разомкнутом контуре.
 *
 * ДВА НАПРАВЛЕНИЯ — не для симметрии, а как встроенная проверка. Ошибка
 * отображения привязана к положению и от направления не зависит, поэтому
 * таблицы, снятые вперёд и назад, ОБЯЗАНЫ совпасть. Не совпали — значит в
 * измерение затекло что-то направленное (трение, фазовая задержка чтения), и
 * таблицу строить рано.
 *
 * ЗУБЦЫ В ТАБЛИЦУ НЕ ПОПАДУТ: на хосте выбрасываются порядки, кратные 11, где
 * живёт настоящий момент. Иначе поправка датчика начала бы компенсировать
 * механику, а это разные вещи с разной судьбой.
 */
#include <SimpleFOC.h>

static const float    U_BASE = 2.0f;
static const float    W_MEAS = 0.3f;
static const uint8_t  REVS   = 6;
static const uint16_t FS_HZ  = 100;

static const uint8_t POLE_PAIRS = 11;
static const float   SUPPLY_V   = 12.0f;
// Нынешние константы. Меряем ИХ, поэтому оставляем как есть.
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

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  blink(5, 80);
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) { }
  Serial.println();
  Serial.println(F("=== измерение полной шкалы AS5048 ==="));
  Serial.print(F("окно в прошивке ")); Serial.print(SENS_MIN_US);
  Serial.print(F("..")); Serial.print(SENS_MAX_US);
  Serial.print(F(" мкс, это ")); Serial.print(SENS_MAX_US - SENS_MIN_US + 1);
  Serial.println(F(" кодов"));

  sensor.init();
  sensor.enableInterrupt(doPWM);
  driver.voltage_power_supply = SUPPLY_V;
  driver.voltage_limit = SUPPLY_V;
  driver.init();
  motor.linkDriver(&driver);
  motor.controller = MotionControlType::velocity_openloop;
  motor.voltage_limit = U_BASE;
  motor.init();

  digitalWrite(LED_BUILTIN, HIGH);
  for (uint8_t blk = 0; blk < 2; blk++) {
  float U = 2.0f;
  float w = (blk == 0) ? +W_MEAS : -W_MEAS;
  motor.voltage_limit = U;
  Serial.print(F("#ПРОГОН U=")); Serial.print(U, 2);
  Serial.print(F(" w=")); Serial.print(w, 3);
  Serial.print(F(" оборотов=")); Serial.print(REVS);
  Serial.print(F(" fs=")); Serial.println(FS_HZ);
  Serial.println(F("t_ms,pulse_us,mech_rad,acc_rad"));
  const uint32_t step_us = 1000000UL / FS_HZ;
  float w_ramp = 0.0f;
  uint32_t prev = micros(), tstart = millis(), next = micros();
  float a0 = 0; bool started = false;

  while (true) {
    uint32_t now = micros();
    float dt = (now - prev) * 1e-6f; prev = now;
    if (dt < 0 || dt > 0.05f) dt = 0;
    if (w_ramp < w) w_ramp = min(w_ramp + 1.0f * dt, w);
    else if (w_ramp > w) w_ramp = max(w_ramp - 1.0f * dt, w);
    motor.loopFOC(); motor.move(w_ramp); sensor.update();
    if (!started && fabsf(w_ramp - w) < 1e-4f) { started = true; a0 = sensor.getAngle(); }
    if ((int32_t)(micros() - next) >= 0) {
      next += step_us;
      Serial.print(millis() - tstart); Serial.print(',');
      Serial.print(sensor.pulse_length_us); Serial.print(',');
      Serial.print(sensor.getMechanicalAngle(), 5); Serial.print(',');
      Serial.println(sensor.getAngle(), 5);
    }
    if (started && fabsf(sensor.getAngle() - a0) >= REVS * TWO_PI_F) break;
  }
  Serial.println(F("#КОНЕЦ"));
  // Между блоками вал не останавливаем полностью: остановка и новый разгон
  // добавили бы в запись два переходных процесса вместо нуля.
  }
  motor.move(0.0f); motor.voltage_limit = 0.0f; motor.disable();
  Serial.println(F("#ВСЁ напряжение снято"));
  while (1) blink(1, 60);
}
void loop() { }
