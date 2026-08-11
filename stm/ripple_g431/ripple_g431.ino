/*
 * B-G431B-ESC1: рябь скорости в разомкнутом контуре против напряжения.
 *
 * Вопрос, который закрывает этот замер. На старом стенде при 1.5 В рябь была
 * 10.5% СКО от команды, и поднять напряжение мешал треск. Треск оказался
 * плохим контактом на клемме шилда, шилда больше нет — значит ручка снова
 * доступна, и надо узнать, лечит ли она рябь.
 *
 * От ответа зависит план: если рябь падает с напряжением, разомкнутого
 * контура достаточно и камера замкнёт положение сама. Если не падает —
 * причина в зубцовом моменте, а он вольтами не лечится, и придётся заводить
 * замкнутый контур по скорости со всеми его настройками.
 *
 * Меряется ОДНА величина при трёх напряжениях, скорость постоянна. Иначе
 * сравнивать было бы нечего.
 *
 * Мгновенная скорость считается на окне 0.5 с. Короче — доминирует шум
 * датчика (размах покоя 1.57°, это 0.027 рад, на окне 0.1 с дало бы 0.27
 * рад/с фантомной ряби, больше самой команды). Длиннее — рябь усредняется, и
 * мы потеряем ровно то, что ищем.
 *
 * СИГНАЛЫ: перед каждым этапом светодиод мигает номер этапа (1, 2, 3 раза),
 * во время вращения горит ровно, между этапами погашен.
 */
#include <SimpleFOC.h>

static const float    VOLTS[]  = {1.5f, 2.0f, 2.5f};
static const uint8_t  NV       = sizeof(VOLTS) / sizeof(VOLTS[0]);
static const float    PROBE_W  = 0.3f;
static const uint32_t SPIN_MS  = 12000;
static const uint32_t REST_MS  = 2000;

static const uint8_t  POLE_PAIRS = 11;
static const float    SUPPLY_V   = 12.0f;
static const unsigned long SENS_MIN_US = 3;
static const unsigned long SENS_MAX_US = 919;

// Окно скорости: 0.5 с при шаге 10 мс.
static const uint8_t  RING = 51;
static const uint16_t STEP_MS = 10;

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

// Результат отдаётся через указатели, а не структурой: сборщик Arduino
// вставляет прототипы функций ВЫШЕ объявлений типов, и возврат структуры
// перестал бы компилироваться с невнятной ошибкой 'Res does not name a type'.
static void measure(float volts, float w_target, float *mean, float *sd,
                     float *lo_out, float *hi_out, uint16_t *n_out) {
  motor.voltage_limit = volts;

  float ring[RING];
  uint8_t ri = 0; bool full = false;
  uint32_t t_step = millis();

  double sum = 0, sum2 = 0;
  uint16_t n = 0;
  float lo = 1e9f, hi = -1e9f;

  float w_ramp = 0.0f;
  uint32_t prev = micros();
  uint32_t t0 = millis();
  bool settled = false;
  uint32_t t_settled = 0;

  while (millis() - t0 < SPIN_MS) {
    uint32_t now = micros();
    float dt = (now - prev) * 1e-6f; prev = now;
    if (dt < 0 || dt > 0.05f) dt = 0;
    float st = 1.0f * dt;
    if (w_ramp < w_target) w_ramp = min(w_ramp + st, w_target);

    motor.loopFOC();
    motor.move(w_ramp);
    sensor.update();

    if (!settled && fabsf(w_ramp - w_target) < 1e-4f) {
      settled = true; t_settled = millis();
    }

    if (millis() - t_step >= STEP_MS) {
      t_step += STEP_MS;
      ring[ri] = sensor.getAngle();
      uint8_t oldest = (uint8_t)((ri + 1) % RING);
      ri = (uint8_t)((ri + 1) % RING);
      if (ri == 0) full = true;
      // Копим статистику только на полке рампы и не раньше, чем через
      // полсекунды после выхода на неё: иначе первое окно захватывало бы
      // хвост разгона и завышало разброс.
      if (full && settled && millis() - t_settled > 600) {
        float v = (ring[(uint8_t)((ri + RING - 1) % RING)] - ring[oldest])
                  / ((RING - 1) * STEP_MS * 1e-3f);
        sum += v; sum2 += (double)v * v; n++;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
  }

  *n_out = n;
  *mean = n ? (float)(sum / n) : 0.0f;
  *sd   = n > 1 ? (float)sqrt(sum2 / n - (sum / n) * (sum / n)) : 0.0f;
  *lo_out = lo; *hi_out = hi;
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  blink(5, 80);

  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) { }
  Serial.println();
  Serial.println(F("=== ESC1: рябь скорости против напряжения ==="));
  Serial.print(F("команда ")); Serial.print(PROBE_W, 2);
  Serial.print(F(" рад/с, окно скорости 0.5 с, этап "));
  Serial.print(SPIN_MS / 1000); Serial.println(F(" с"));

  sensor.init();
  sensor.enableInterrupt(doPWM);

  driver.voltage_power_supply = SUPPLY_V;
  driver.voltage_limit = SUPPLY_V;
  driver.init();
  motor.linkDriver(&driver);
  motor.controller = MotionControlType::velocity_openloop;
  motor.voltage_limit = VOLTS[0];
  motor.init();

  // КОНТРОЛЬ: та же статистика на СТОЯЩЕМ вале. Настоящей ряби там нет по
  // определению, значит всё, что намеряется, — шум измерения. Без этого числа
  // нельзя сказать, меряем мы механику или датчик.
  float c_mean, c_sd, c_lo, c_hi; uint16_t c_n;
  digitalWrite(LED_BUILTIN, LOW); delay(400);
  blink(1, 700);
  Serial.println(F("КОНТРОЛЬ: вал стоит, команда 0 — меряю шум измерения"));
  digitalWrite(LED_BUILTIN, HIGH);
  measure(VOLTS[0], 0.0f, &c_mean, &c_sd, &c_lo, &c_hi, &c_n);
  digitalWrite(LED_BUILTIN, LOW);

  float r_mean[NV], r_sd[NV], r_lo[NV], r_hi[NV];
  uint16_t r_n[NV];
  for (uint8_t i = 0; i < NV; i++) {
    digitalWrite(LED_BUILTIN, LOW);
    delay(400);
    blink(i + 1, 250);            // номер этапа
    delay(300);
    Serial.print(F("этап ")); Serial.print(i + 1);
    Serial.print(F("/")); Serial.print(NV);
    Serial.print(F(": ")); Serial.print(VOLTS[i], 1); Serial.println(F(" В"));
    digitalWrite(LED_BUILTIN, HIGH);
    measure(VOLTS[i], PROBE_W, &r_mean[i], &r_sd[i], &r_lo[i], &r_hi[i], &r_n[i]);
    motor.move(0.0f);
    digitalWrite(LED_BUILTIN, LOW);
    uint32_t tr = millis();
    while (millis() - tr < REST_MS) { motor.loopFOC(); motor.move(0.0f); sensor.update(); }
  }
  motor.disable();

  Serial.println();
  Serial.println(F("=== ИТОГ ==="));
  Serial.print(F("ШУМ ИЗМЕРЕНИЯ (вал стоит): СКО ")); Serial.print(c_sd, 4);
  Serial.print(F(" рад/с = ")); Serial.print(100.0f * c_sd / PROBE_W, 1);
  Serial.println(F("% от команды"));
  Serial.println();
  Serial.println(F("  В     средняя   СКО      %команды   размах"));
  for (uint8_t i = 0; i < NV; i++) {
    Serial.print(F("  ")); Serial.print(VOLTS[i], 1);
    Serial.print(F("   ")); Serial.print(r_mean[i], 4);
    Serial.print(F("   ")); Serial.print(r_sd[i], 4);
    Serial.print(F("   ")); Serial.print(100.0f * r_sd[i] / PROBE_W, 1);
    Serial.print(F("%   ")); Serial.print(r_lo[i], 3);
    Serial.print(F("..")); Serial.print(r_hi[i], 3);
    Serial.print(F("  (окон ")); Serial.print(r_n[i]); Serial.println(F(")"));
  }
  Serial.println();
  // Вычитание в квадратуре: шум измерения и настоящая рябь независимы, и
  // складываются их дисперсии, а не СКО.
  Serial.println(F("после вычитания шума измерения (в квадратуре):"));
  for (uint8_t i = 0; i < NV; i++) {
    float v = r_sd[i] * r_sd[i] - c_sd * c_sd;
    Serial.print(F("  ")); Serial.print(VOLTS[i], 1); Serial.print(F(" В: "));
    if (v <= 0) {
      Serial.println(F("НИЧЕГО НЕ ОСТАЁТСЯ — рябь неотличима от шума датчика"));
    } else {
      float t = sqrt(v);
      Serial.print(t, 4); Serial.print(F(" рад/с = "));
      Serial.print(100.0f * t / PROBE_W, 1); Serial.println(F("% от команды"));
    }
  }

  while (1) blink(1, 60);
}

void loop() { }
