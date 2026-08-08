/*
 * Мост телефон -> мотор. Бинарный протокол v1.
 * Спецификация: docs/ПРОТОКОЛ_ТЕЛЕФОН_STM32.md — там же контрольные векторы CRC.
 *
 * Железо: Nucleo-F411RE + SimpleFOCShield v2.0.4 + BGM4108 + AS5048 (PWM).
 * Значения железа перенесены из full_control.ino — того скетча, на котором
 * стенд реально работал. Придумывать их заново значило бы проверять железо
 * ещё раз.
 *
 * ВАЖНО: канал БИНАРНЫЙ. Ни одного Serial.print, ни SimpleFOCDebug, ни
 * Commander — любой отладочный текст уедет в тот же поток и приёмник на
 * телефоне будет разбирать мусор. Отладка — только миганием LD2 и полями
 * статуса в ответе.
 *
 * ДВЕ СБОРКИ, по ступеням тикета:
 *   MOTOR_ENABLED 0 — ступень 2: работает только связь. Драйвер не
 *                     инициализируется вовсе, PWM на мотор не идёт.
 *   MOTOR_ENABLED 1 — ступень 3: та же связь плюс разомкнутый контур
 *                     скорости.
 * Ступень 2 обязана пройти на сборке 0: иначе потеря пакета и срыв мотора
 * неразличимы, а тикет разделяет слои намеренно.
 */

#include <SimpleFOC.h>

// ============================ СБОРКА ============================
#define MOTOR_ENABLED 0

// ==================== ПАРАМЕТРЫ (в лог по §3) ====================
// Эти числа обязаны попадать в снимок конфигурации каждого лога: без них
// разбор "почему остановился именно так" задним числом невозможен.
static const uint32_t BAUD          = 115200;   // спецификация протокола
static const uint32_t WATCHDOG_MS   = 300;      // нет кадра дольше -> останов
static const float    VEL_LIMIT     = 20.0f;    // рад/с, как в full_control
static const float    MAX_ACCEL     = 20.0f;    // рад/с^2, ограничение разгона
static const float    VOLTAGE_LIMIT = 1.0f;     // В, как в full_control
static const float    SUPPLY_V      = 12.0f;
static const uint8_t  POLE_PAIRS    = 11;
// Пределы длительности импульса датчика. Библиотека держит их приватными,
// поэтому здесь они ОДНИМ источником: и в конструктор, и в проверку живости.
static const unsigned long SENS_MIN_US = 7;
static const unsigned long SENS_MAX_US = 920;

// Тикет говорит "плавный останов через jerk-limit" и тут же уточняет
// "останов трапецией". Это не одно и то же: трапеция по скорости — это
// ПОСТОЯННОЕ ускорение, то есть ограничение производной скорости, а jerk —
// производная ускорения. Реализована трапеция, как требует уточнение в
// скобках: она снимает и удар по механике, и скачок тока, а ограничение
// рывка поверх неё имеет смысл добавлять только если трапеция окажется
// недостаточной на железе.

// ============================ ЖЕЛЕЗО ============================
MagneticSensorPWM sensor = MagneticSensorPWM(3, SENS_MIN_US, SENS_MAX_US);
void doPWM() { sensor.handlePWM(); }

#if MOTOR_ENABLED
BLDCMotor      motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver3PWM driver = BLDCDriver3PWM(9, 5, 6, 8);
#endif

// ============================ ПРОТОКОЛ ============================
static const uint8_t MAGIC       = 0xA5;
static const uint8_t REQ_LEN     = 7;
static const uint8_t RESP_LEN    = 8;

static const uint8_t ST_WATCHDOG = 1 << 0;
static const uint8_t ST_ENC_OK   = 1 << 1;
static const uint8_t ST_VEL_CLIP = 1 << 2;
static const uint8_t ST_CRC_DROP = 1 << 3;

static uint8_t crc8(const uint8_t *d, uint8_t n) {
  uint8_t c = 0x00;
  while (n--) {
    c ^= *d++;
    for (uint8_t i = 0; i < 8; i++)
      c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x07) : (uint8_t)(c << 1);
  }
  return c;
}

// ============================ СОСТОЯНИЕ ============================
static uint8_t  rxbuf[REQ_LEN];
static uint8_t  rxn = 0;

static float    w_target  = 0.0f;   // что просит телефон
static float    w_applied = 0.0f;   // что отдаётся мотору после ограничений
static uint32_t last_rx_ms = 0;
static bool     had_first_frame = false;
static bool     crc_dropped = false;   // снимается следующим ответом
static bool     vel_clipped = false;
static uint32_t loop_prev_us = 0;

// ============================ ПРИЁМ ============================
//
// Ресинхронизация идёт по МАГИКУ, а не по длине: при потере одного байта
// выравнивание по длине залипает навсегда, и связь больше не восстановится
// сама. При несовпадении CRC выбрасывается один первый байт и разбор
// продолжается с остатка — так кадр находится даже если перед ним пришёл
// лишний байт.
static void rxShift() {
  for (uint8_t i = 1; i < rxn; i++) rxbuf[i - 1] = rxbuf[i];
  rxn--;
  while (rxn > 0 && rxbuf[0] != MAGIC) {
    for (uint8_t i = 1; i < rxn; i++) rxbuf[i - 1] = rxbuf[i];
    rxn--;
  }
}

static void sendReply(uint8_t seq) {
  float theta = sensor.getAngle();
  uint8_t st = 0;
  if (!had_first_frame || (millis() - last_rx_ms) > WATCHDOG_MS) st |= ST_WATCHDOG;
  // Живость энкодера: длительность импульса в рабочем диапазоне. Оборванный
  // сигнал даёт 0 или выход за пределы; неподвижный вал — нет, поэтому
  // критерий не путает "стоит" с "молчит".
  if (sensor.pulse_length_us >= SENS_MIN_US &&
      sensor.pulse_length_us <= SENS_MAX_US) st |= ST_ENC_OK;
  if (vel_clipped) st |= ST_VEL_CLIP;
  if (crc_dropped) st |= ST_CRC_DROP;
  crc_dropped = false;          // бит одноразовый: сообщает о ПРЕДЫДУЩЕМ кадре

  uint8_t out[RESP_LEN];
  out[0] = MAGIC;
  out[1] = seq;
  memcpy(&out[2], &theta, 4);
  out[6] = st;
  out[7] = crc8(out, RESP_LEN - 1);
  Serial.write(out, RESP_LEN);
}

static void pump() {
  while (Serial.available() > 0) {
    uint8_t b = (uint8_t)Serial.read();
    if (rxn == 0 && b != MAGIC) continue;   // мусор до магика
    rxbuf[rxn++] = b;
    if (rxn < REQ_LEN) continue;

    if (crc8(rxbuf, REQ_LEN - 1) != rxbuf[REQ_LEN - 1]) {
      crc_dropped = true;
      rxShift();                            // кадр битый: ответа НЕТ
      continue;
    }
    float w;
    memcpy(&w, &rxbuf[2], 4);
    if (isnan(w) || isinf(w)) {             // мусор, прошедший CRC по случайности
      crc_dropped = true;
      rxn = 0;
      continue;
    }
    w_target = w;
    last_rx_ms = millis();
    had_first_frame = true;
    sendReply(rxbuf[1]);
    rxn = 0;
  }
}

// ============================ ЦИКЛ ============================
void setup() {
  Serial.begin(BAUD);

  pinMode(LED_BUILTIN, OUTPUT);

  sensor.init();
  sensor.enableInterrupt(doPWM);

#if MOTOR_ENABLED
  // Датчик НЕ связывается с мотором и initFOC() НЕ зовётся. Контур
  // разомкнутый: выравнивание ему не нужно, а initFOC() его выполняет —
  // то есть двигает мотор при включении и ставит датчик в критический путь.
  // Тогда сбой датчика, который для нас диагностический, останавливал бы
  // весь контур. Датчик читается отдельно, только для theta_enc в ответе.
  driver.voltage_power_supply = SUPPLY_V;
  driver.init();
  motor.linkDriver(&driver);
  motor.controller = MotionControlType::velocity_openloop;
  motor.voltage_limit  = VOLTAGE_LIMIT;
  motor.velocity_limit = VEL_LIMIT;
  motor.init();
#endif

  loop_prev_us = micros();
}

void loop() {
  uint32_t now_us = micros();
  float dt = (now_us - loop_prev_us) * 1e-6f;
  loop_prev_us = now_us;
  if (dt < 0.0f || dt > 0.05f) dt = 0.0f;   // первый проход и переполнение

  pump();

  // Сторожевой: цель обнуляется, но применяемая скорость СВОДИТСЯ трапецией,
  // а не обрывается. До первого кадра телефона мотор тоже стоит.
  float goal = w_target;
  if (!had_first_frame || (millis() - last_rx_ms) > WATCHDOG_MS) goal = 0.0f;

  vel_clipped = false;
  if (goal >  VEL_LIMIT) { goal =  VEL_LIMIT; vel_clipped = true; }
  if (goal < -VEL_LIMIT) { goal = -VEL_LIMIT; vel_clipped = true; }

  float step = MAX_ACCEL * dt;
  if (w_applied < goal) w_applied = (w_applied + step > goal) ? goal : w_applied + step;
  else if (w_applied > goal) w_applied = (w_applied - step < goal) ? goal : w_applied - step;

#if MOTOR_ENABLED
  // loopFOC() в разомкнутом контуре не нужен: он обслуживает обратную связь,
  // которой здесь нет. move() сам крутит электрический угол по времени.
  motor.move(w_applied);
#endif

  // LD2: связь есть — горит, сторожевой сработал — мигает. Единственная
  // отладка, которая не портит бинарный поток.
  bool alive = had_first_frame && (millis() - last_rx_ms) <= WATCHDOG_MS;
  digitalWrite(LED_BUILTIN, alive ? HIGH : ((millis() >> 8) & 1));
}
