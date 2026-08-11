/*
 * B-G431B-ESC1: честное измерение полной шкалы AS5048.
 *
 * Зачем. Константы SENS_MIN_US=3 и SENS_MAX_US=919 снимались как наблюдённые
 * крайние значения при вращении вала РУКОЙ. Это не полная шкала, а выборочные
 * экстремумы одного оборота: настоящие края могли просто не попасться.
 * Проверка сетки печатаемых углов показала 2pi/917, тогда как окно 3..919
 * задаёт 916 кодов, — то есть константы неверны хотя бы на код.
 *
 * Метод. При РАВНОМЕРНОМ вращении истинный угол растёт линейно, значит и
 * длительность импульса обязана расти линейно и сбрасываться раз в оборот.
 * Пишем СЫРУЮ длительность (не производный угол!), а на хосте:
 *   - подгоняем прямую к каждому участку между сбросами -> наклон в мкс/с;
 *   - положение сбросов даёт истинные края шкалы БЕЗ опоры на текущие
 *     константы;
 *   - отклонение от прямой внутри участка — это нелинейность датчика, то самое
 *     обвинение, которое я не смог проверить прошлой попыткой.
 *
 * Пишется и производный угол тоже — чтобы видеть, что именно нынешние
 * константы делают с сырыми данными.
 *
 * РАЗВИЛКА «ДАТЧИК ИЛИ МЕХАНИКА». Шкала оказалась верна (916.1 против 916),
 * но нелинейность внутри оборота настоящая: СКО 0.483 град. Кто её создаёт —
 * различается одним признаком:
 *
 *   настоящий момент (разбаланс груза, эксцентриситет ротора) даёт отклонение
 *     угла, ОБРАТНО ПРОПОРЦИОНАЛЬНОЕ жёсткости привода, а жёсткость растёт с
 *     напряжением. При 3.0 В против 2.0 амплитуда обязана упасть в 1.5 раза;
 *   ошибка ДАТЧИКА — это отображение «длительность импульса -> угол», и от
 *     напряжения она не зависит вовсе.
 *
 * Поэтому прогон идёт двумя блоками, 2.0 В и 3.0 В, по два оборота. Два
 * оборота на 0.3 рад/с это 42 секунды — блок на 3.0 В укладывается в минуту,
 * разрешённую владельцем для этого напряжения.
 */
#include <SimpleFOC.h>

static const float    U_BASE = 2.0f;
static const float    W_MEAS = 0.3f;
static const uint8_t  REVS   = 2;
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
  float U = (blk == 0) ? 2.0f : 3.0f;
  motor.voltage_limit = U;
  Serial.print(F("#ПРОГОН U=")); Serial.print(U, 2);
  Serial.print(F(" w=")); Serial.print(W_MEAS, 3);
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
    if (w_ramp < W_MEAS) w_ramp = min(w_ramp + 1.0f * dt, W_MEAS);
    motor.loopFOC(); motor.move(w_ramp); sensor.update();
    if (!started && fabsf(w_ramp - W_MEAS) < 1e-4f) { started = true; a0 = sensor.getAngle(); }
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
