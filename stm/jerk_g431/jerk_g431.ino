/*
 * B-G431B-ESC1: поиск БЫСТРЫХ подёргиваний вала.
 *
 * Зачем ещё один замер про рябь. Прошлый (ripple_g431) считал скорость на окне
 * 0.5 с. Окно выбрано, чтобы перебить шум датчика, — и оно же съедает всё, что
 * быстрее полусекунды. Вывод «рябь ниже шумового пола» верен ровно для
 * медленных колебаний. Hero же видит глазами подёргивания на быстрой части
 * синуса, то есть события короче окна, в которое я смотрел.
 *
 * Почему спектр, а не разброс. Шум датчика широкополосный и некогерентный:
 * в спектре он размазан по всем частотам. Зубцовый момент даёт ПЕРИОДИЧЕСКУЮ
 * помеху на частоте, пропорциональной скорости вращения. Периодическая помеха
 * собирается в спектре в узкий пик и вылезает над шумом даже тогда, когда в
 * среднеквадратичном её не видно. Поэтому здесь пишутся сырые отсчёты, а вся
 * обработка — на ноутбуке.
 *
 * Почему не по сети. Телеметрия идёт 10 Гц, то есть видит частоты до 5 Гц.
 * Зубцовая помеха при 11 парах полюсов и 1 рад/с лежит на десятках герц —
 * по сети её не увидеть в принципе. Отсюда запись в память и выгрузка через
 * VCP: канал отладки на этой плате отдельный, и это как раз тот случай, ради
 * которого он пригодился.
 *
 * СИГНАЛЫ: перед каждой скоростью светодиод мигает её номер, во время записи
 * горит ровно, во время выгрузки погашен.
 */
#include <SimpleFOC.h>

static const float    SPEEDS[] = {0.1f, 0.3f, 0.6f, 1.0f};
static const uint8_t  NS       = sizeof(SPEEDS) / sizeof(SPEEDS[0]);
static const uint16_t FS_HZ    = 200;
static const uint16_t N_SAMP   = 1600;          // 8 с при 200 Гц
static const uint32_t SETTLE_MS = 2500;

static const uint8_t  POLE_PAIRS = 11;
static const float    SUPPLY_V   = 12.0f;
static const unsigned long SENS_MIN_US = 3;
static const unsigned long SENS_MAX_US = 919;

// Напряжение задаётся здесь. Прогон повторяется на двух значениях, чтобы
// ответить и на вторую догадку Hero — помогает ли ручка напряжения.
#ifndef PROBE_VOLTS
#define PROBE_VOLTS 1.5f
#endif

MagneticSensorPWM sensor = MagneticSensorPWM(PB8, SENS_MIN_US, SENS_MAX_US);
void doPWM() { sensor.handlePWM(); }

BLDCMotor      motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver6PWM driver = BLDCDriver6PWM(A_PHASE_UH, A_PHASE_UL,
                                       A_PHASE_VH, A_PHASE_VL,
                                       A_PHASE_WH, A_PHASE_WL);

static float buf[N_SAMP];

static void blink(uint8_t n, uint16_t ms) {
  for (uint8_t i = 0; i < n; i++) {
    digitalWrite(LED_BUILTIN, HIGH); delay(ms);
    digitalWrite(LED_BUILTIN, LOW);  delay(ms);
  }
}

/** Крутит на w, ждёт установления, пишет N_SAMP отсчётов угла на FS_HZ. */
static void record(float w) {
  const uint32_t step_us = 1000000UL / FS_HZ;
  float w_ramp = 0.0f;
  uint32_t prev = micros();
  uint32_t t0 = millis();

  // разгон рампой: скачок уставки сорвал бы синхронизм, и мы записали бы срыв
  while (millis() - t0 < SETTLE_MS || fabsf(w_ramp - w) > 1e-4f) {
    uint32_t now = micros();
    float dt = (now - prev) * 1e-6f; prev = now;
    if (dt < 0 || dt > 0.05f) dt = 0;
    float st = 1.0f * dt;
    if (w_ramp < w) w_ramp = min(w_ramp + st, w);
    else if (w_ramp > w) w_ramp = max(w_ramp - st, w);
    motor.loopFOC(); motor.move(w_ramp);
    sensor.update();
  }

  uint32_t next = micros();
  for (uint16_t i = 0; i < N_SAMP; i++) {
    // Крутим и обновляем датчик, пока не подошёл момент отсчёта. Спать нельзя:
    // без loopFOC() поле встанет, и мы запишем не подёргивания, а остановку.
    while ((int32_t)(micros() - next) < 0) {
      motor.loopFOC(); motor.move(w_ramp); sensor.update();
    }
    next += step_us;
    buf[i] = sensor.getAngle();
  }
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  blink(5, 80);

  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) { }
  Serial.println();
  Serial.println(F("=== ESC1: запись угла на 200 Гц ==="));
  Serial.print(F("напряжение ")); Serial.print(PROBE_VOLTS, 2);
  Serial.print(F(" В, отсчётов ")); Serial.print(N_SAMP);
  Serial.print(F(" на скорость, всего скоростей ")); Serial.println(NS);

  sensor.init();
  sensor.enableInterrupt(doPWM);

  driver.voltage_power_supply = SUPPLY_V;
  driver.voltage_limit = SUPPLY_V;
  driver.init();
  motor.linkDriver(&driver);
  motor.controller = MotionControlType::velocity_openloop;
  motor.voltage_limit = PROBE_VOLTS;
  motor.init();

  for (uint8_t k = 0; k < NS; k++) {
    digitalWrite(LED_BUILTIN, LOW); delay(300);
    blink(k + 1, 200); delay(200);
    digitalWrite(LED_BUILTIN, HIGH);
    record(SPEEDS[k]);
    motor.move(0.0f);
    digitalWrite(LED_BUILTIN, LOW);

    // Выгрузка. Заголовок содержит всё, что нужно для разбора, — потребитель
    // не должен догадываться о частоте или скорости по имени файла.
    Serial.print(F("#НАЧАЛО w=")); Serial.print(SPEEDS[k], 4);
    Serial.print(F(" volts=")); Serial.print(PROBE_VOLTS, 2);
    Serial.print(F(" fs=")); Serial.print(FS_HZ);
    Serial.print(F(" n=")); Serial.println(N_SAMP);
    for (uint16_t i = 0; i < N_SAMP; i++) Serial.println(buf[i], 6);
    Serial.println(F("#КОНЕЦ"));

    uint32_t tr = millis();
    while (millis() - tr < 1500) { motor.loopFOC(); motor.move(0.0f); sensor.update(); }
  }
  motor.disable();
  Serial.println(F("#ВСЁ"));
  while (1) blink(1, 60);
}

void loop() { }
