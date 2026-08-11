/*
 * B-G431B-ESC1: матрица «напряжение × скорость» для дрожания вала.
 *
 * Критерий объявлен ДО прогона (tools/stand/jerk_analyze.py): СКО остатка угла
 * в полосе 1–20 Гц за вычетом шумового пола, в градусах. Полоса снизу режет
 * дрейф и артефакты снятия тренда, сверху — широкополосный шум датчика.
 *
 * Пишутся СЫРЫЕ отсчёты угла на 200 Гц, вся обработка на ноутбуке. Так
 * критерий можно пересчитать задним числом, не повторяя прогон, — но объявлен
 * он заранее, чтобы метрика не выбиралась под понравившийся результат.
 *
 * Верхнее напряжение 3.0 В, а не 3.5: Hero предупреждал, что на верхней части
 * диапазона мотор ощутимо греется. Каждая ячейка идёт 11 секунд, на 3.0 В
 * набегает меньше минуты суммарно — но лезть выше без нужды незачем.
 *
 * СИГНАЛЫ: перед ячейкой светодиод мигает номер напряжения, во время записи
 * горит ровно, при выгрузке гаснет.
 */
#include <SimpleFOC.h>

static const float   VOLTS[]  = {1.0f, 1.5f, 2.0f, 2.5f, 3.0f};
static const uint8_t NV       = sizeof(VOLTS) / sizeof(VOLTS[0]);
static const float   SPEEDS[] = {0.1f, 0.3f, 0.6f, 1.0f, 1.5f};
static const uint8_t NS       = sizeof(SPEEDS) / sizeof(SPEEDS[0]);

static const uint16_t FS_HZ    = 200;
static const uint16_t N_SAMP   = 1600;          // 8 с
static const uint32_t SETTLE_MS = 2500;

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

static float buf[N_SAMP];

static void blink(uint8_t n, uint16_t ms) {
  for (uint8_t i = 0; i < n; i++) {
    digitalWrite(LED_BUILTIN, HIGH); delay(ms);
    digitalWrite(LED_BUILTIN, LOW);  delay(ms);
  }
}

static void record(float w) {
  const uint32_t step_us = 1000000UL / FS_HZ;
  static float w_ramp = 0.0f;                 // сохраняется между ячейками:
                                              // между скоростями вал не
                                              // останавливается полностью, но
                                              // рампа всё равно ведёт плавно
  uint32_t prev = micros();
  uint32_t t0 = millis();
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
  Serial.println(F("=== матрица дрожания: напряжение x скорость ==="));
  Serial.print(F("ячеек ")); Serial.print((int)(NV * NS));
  Serial.print(F(", по ")); Serial.print(N_SAMP / (float)FS_HZ, 1);
  Serial.println(F(" с записи на 200 Гц"));

  sensor.init();
  sensor.enableInterrupt(doPWM);

  driver.voltage_power_supply = SUPPLY_V;
  driver.voltage_limit = SUPPLY_V;
  driver.init();
  motor.linkDriver(&driver);
  motor.controller = MotionControlType::velocity_openloop;
  motor.voltage_limit = VOLTS[0];
  motor.init();

  for (uint8_t v = 0; v < NV; v++) {
    motor.voltage_limit = VOLTS[v];
    for (uint8_t sp = 0; sp < NS; sp++) {
      digitalWrite(LED_BUILTIN, LOW); delay(250);
      blink(v + 1, 180); delay(200);
      digitalWrite(LED_BUILTIN, HIGH);
      record(SPEEDS[sp]);
      digitalWrite(LED_BUILTIN, LOW);

      Serial.print(F("#НАЧАЛО w=")); Serial.print(SPEEDS[sp], 4);
      Serial.print(F(" volts=")); Serial.print(VOLTS[v], 2);
      Serial.print(F(" fs=")); Serial.print(FS_HZ);
      Serial.print(F(" n=")); Serial.println(N_SAMP);
      for (uint16_t i = 0; i < N_SAMP; i++) Serial.println(buf[i], 6);
      Serial.println(F("#КОНЕЦ"));
    }
  }
  motor.move(0.0f);
  motor.disable();
  Serial.println(F("#ВСЁ"));
  while (1) blink(1, 60);
}

void loop() { }
