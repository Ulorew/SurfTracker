/*
 * B-G431B-ESC1: проверка датчика МОТОРОМ, а не рукой.
 *
 * Почему так. Прошлая проверка размечала этапы временем и просила Hero
 * покрутить вал в нужную секунду. Со стороны стенда не видно ни момента
 * заливки, ни границ этапов — приходится гадать. Здесь человек из замера
 * убран совсем:
 *
 *   - вал крутит мотор, и это же служит сигналом «идёт этап»;
 *   - границы этапов отбиваются светодиодом (PC6);
 *   - мотор — ЭТАЛОН скорости, поэтому проверяется не только «датчик живой»,
 *     но и калибровка: измеренная скорость обязана совпасть с командой.
 *     Ошибка окна длительности проявится как отличие отношения от единицы.
 *
 * Пин датчика ОПРЕДЕЛЯЕТСЯ САМ. На этом стенде сигнал уже находили не там,
 * где ожидали (паяли по распиновке в PB8, пришёл на PB6 — разъём считается с
 * другого конца). Поэтому сначала опрос всех трёх пинов разъёма, и только
 * потом замер — иначе каждая перепайка стоила бы отдельного круга.
 *
 * СИГНАЛЫ СВЕТОДИОДОМ:
 *   5 быстрых миганий  — прошивка стартовала
 *   2 мигания          — поиск пина закончен
 *   горит ровно        — этап «покой», вал стоять ДОЛЖЕН
 *   мигает 2 Гц        — этап «вперёд», вал крутится
 *   мигает 0.5 Гц      — этап «назад», вал крутится в другую сторону
 *   частое мигание     — всё сошлось
 *   длинные вспышки    — что-то не сошлось, смотреть вывод
 */
#include <SimpleFOC.h>

static const uint8_t  PINS[]  = {PB6, PB7, PB8};
static const char*    NAMES[] = {"PB6 (A_HALL1)", "PB7 (A_HALL2)", "PB8 (A_HALL3)"};
static const uint8_t  NPIN    = 3;

// Окно длительности импульса. ИЗМЕРЕНО на этом экземпляре: полный оборот дал
// 3..919 мкс при периоде 921 мкс. Сходится с устройством AS5048 — кадр 4119
// тактов. Прежнее значение 7 приехало со старого стенда; ошибка небольшая
// (0.33% по масштабу и постоянные -1.6° смещения), но бесплатная в починке.
static const unsigned long SENS_MIN_US = 3;
static const unsigned long SENS_MAX_US = 919;

static const uint8_t POLE_PAIRS   = 11;
static const float   SUPPLY_V     = 12.0f;
static const float   VOLTAGE_LIMIT = 2.0f;
static const float   PROBE_W      = 0.3f;      // рад/с
static const uint32_t REST_MS     = 3000;
static const uint32_t SPIN_MS     = 12000;

MagneticSensorPWM *sensor = 0;
BLDCMotor      motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver6PWM driver = BLDCDriver6PWM(A_PHASE_UH, A_PHASE_UL,
                                       A_PHASE_VH, A_PHASE_VL,
                                       A_PHASE_WH, A_PHASE_WL);

volatile uint32_t edges = 0;
void doPWM() { edges++; if (sensor) sensor->handlePWM(); }

// --- счётчики поиска пина ---
volatile uint32_t scan[NPIN] = {0, 0, 0};
void s0() { scan[0]++; }
void s1() { scan[1]++; }
void s2() { scan[2]++; }
static void (*SISR[NPIN])() = {s0, s1, s2};

static void blink(uint8_t n, uint16_t on_ms, uint16_t off_ms) {
  for (uint8_t i = 0; i < n; i++) {
    digitalWrite(LED_BUILTIN, HIGH); delay(on_ms);
    digitalWrite(LED_BUILTIN, LOW);  delay(off_ms);
  }
}

/** Крутит заданное время, возвращает измеренную скорость по датчику. */
static float spin(float w, uint32_t ms, uint16_t led_period_ms,
                   unsigned long *p_lo, unsigned long *p_hi) {
  uint32_t t0 = millis();
  // Разгон рампой руками: скачок уставки на разомкнутом контуре срывает
  // синхронизм, и мы мерили бы срыв вместо датчика.
  float w_ramp = 0.0f;
  uint32_t prev = micros();
  float a0 = 0.0f; bool a0_set = false; uint32_t t_a0 = 0;

  while (millis() - t0 < ms) {
    uint32_t now = micros();
    float dt = (now - prev) * 1e-6f; prev = now;
    if (dt < 0 || dt > 0.05f) dt = 0;
    float step = 1.0f * dt;                    // 1 рад/с^2
    if (w_ramp < w) w_ramp = min(w_ramp + step, w);
    else if (w_ramp > w) w_ramp = max(w_ramp - step, w);

    motor.loopFOC();
    motor.move(w_ramp);
    sensor->update();

    unsigned long p = sensor->pulse_length_us;
    if (p < *p_lo) *p_lo = p;
    if (p > *p_hi) *p_hi = p;

    // Отсчёт скорости берём ПОСЛЕ выхода рампы на полку: на разгоне
    // мгновенная скорость по определению не равна команде, и включать его в
    // среднее значило бы занижать отношение на величину, зависящую от рампы.
    if (!a0_set && fabsf(w_ramp - w) < 1e-4f) {
      a0 = sensor->getAngle(); t_a0 = millis(); a0_set = true;
    }
    digitalWrite(LED_BUILTIN,
                  (millis() / (led_period_ms / 2)) & 1 ? HIGH : LOW);
  }
  float a1 = sensor->getAngle();
  uint32_t t1 = millis();
  if (!a0_set || t1 == t_a0) return 0.0f;
  return (a1 - a0) / ((t1 - t_a0) * 1e-3f);
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  blink(5, 80, 80);                 // «прошивка стартовала» — видно сразу

  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) { }
  Serial.println();
  Serial.println(F("=== ESC1: проверка датчика мотором ==="));

  // --- 1. Поиск пина ---
  for (uint8_t i = 0; i < NPIN; i++) {
    pinMode(PINS[i], INPUT);
    attachInterrupt(digitalPinToInterrupt(PINS[i]), SISR[i], CHANGE);
  }
  delay(600);
  uint8_t best = 0xFF; uint32_t bestn = 0;
  for (uint8_t i = 0; i < NPIN; i++) {
    detachInterrupt(digitalPinToInterrupt(PINS[i]));
    Serial.print(F("  ")); Serial.print(NAMES[i]);
    Serial.print(F(": фронтов за 0.6 с = ")); Serial.println(scan[i]);
    if (scan[i] > bestn) { bestn = scan[i]; best = i; }
  }
  blink(2, 150, 150);
  if (best == 0xFF || bestn < 100) {
    Serial.println(F("СИГНАЛА НЕТ НИ НА ОДНОМ ПИНЕ — проверять питание и пайку"));
    while (1) blink(1, 800, 800);
  }
  Serial.print(F("датчик найден на ")); Serial.println(NAMES[best]);
  Serial.print(F("период ШИМ ~"));
  Serial.print(600.0f / (bestn / 2.0f), 3); Serial.println(F(" мс"));

  sensor = new MagneticSensorPWM(PINS[best], SENS_MIN_US, SENS_MAX_US);
  sensor->init();
  sensor->enableInterrupt(doPWM);

  // --- 2. Мотор ---
  driver.voltage_power_supply = SUPPLY_V;
  driver.voltage_limit = SUPPLY_V;
  if (!driver.init()) {
    Serial.println(F("ДРАЙВЕР НЕ ПОДНЯЛСЯ"));
    while (1) blink(1, 800, 800);
  }
  motor.linkDriver(&driver);
  motor.controller = MotionControlType::velocity_openloop;
  motor.voltage_limit = VOLTAGE_LIMIT;
  motor.init();

  unsigned long p_lo = 0xFFFFFFFF, p_hi = 0;

  // --- 3. Покой: шум ---
  Serial.println();
  Serial.println(F("этап ПОКОЙ (3 с): светодиод горит ровно, вал стоит"));
  digitalWrite(LED_BUILTIN, HIGH);
  float q_lo = 1e9f, q_hi = -1e9f;
  uint32_t tq = millis();
  while (millis() - tq < REST_MS) {
    sensor->update();
    float a = sensor->getAngle();
    if (a < q_lo) q_lo = a;
    if (a > q_hi) q_hi = a;
  }
  float noise = (q_hi - q_lo) * 57.2958f;
  Serial.print(F("  шум покоя: размах ")); Serial.print(noise, 4);
  Serial.println(F("°"));

  // --- 4. Вперёд ---
  Serial.println();
  Serial.print(F("этап ВПЕРЁД (12 с): светодиод мигает часто, команда +"));
  Serial.println(PROBE_W, 2);
  float w_fwd = spin(PROBE_W, SPIN_MS, 500, &p_lo, &p_hi);

  Serial.println(F("этап ПОКОЙ"));
  digitalWrite(LED_BUILTIN, HIGH);
  motor.move(0.0f);
  tq = millis();
  while (millis() - tq < REST_MS) { motor.loopFOC(); motor.move(0.0f); sensor->update(); }

  // --- 5. Назад ---
  Serial.print(F("этап НАЗАД (12 с): светодиод мигает редко, команда -"));
  Serial.println(PROBE_W, 2);
  float w_rev = spin(-PROBE_W, SPIN_MS, 2000, &p_lo, &p_hi);

  motor.move(0.0f);
  motor.disable();
  digitalWrite(LED_BUILTIN, LOW);

  // --- 6. Итог ---
  Serial.println();
  Serial.println(F("=== ИТОГ ==="));
  Serial.print(F("длительность импульса: ")); Serial.print(p_lo);
  Serial.print(F("..")); Serial.print(p_hi); Serial.println(F(" мкс"));
  Serial.print(F("шум покоя: ")); Serial.print(noise, 4); Serial.println(F("°"));
  Serial.print(F("скорость вперёд: ")); Serial.print(w_fwd, 4);
  Serial.print(F(" при команде ")); Serial.print(PROBE_W, 3);
  Serial.print(F("  отношение ")); Serial.println(w_fwd / PROBE_W, 4);
  Serial.print(F("скорость назад:  ")); Serial.print(w_rev, 4);
  Serial.print(F(" при команде ")); Serial.print(-PROBE_W, 3);
  Serial.print(F("  отношение ")); Serial.println(w_rev / -PROBE_W, 4);

  bool ok = fabsf(w_fwd / PROBE_W - 1.0f) < 0.08f
          && fabsf(w_rev / -PROBE_W - 1.0f) < 0.08f;
  Serial.println();
  if (ok) Serial.println(F("СОШЛОСЬ: датчик мерит то же, что командуем"));
  else    Serial.println(F("НЕ СОШЛОСЬ: датчик и команда расходятся больше 8%"));

  while (1) blink(1, ok ? 60 : 600, ok ? 60 : 600);
}

void loop() { }
