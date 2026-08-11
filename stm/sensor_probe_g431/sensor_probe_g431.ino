/*
 * B-G431B-ESC1: проверка датчика AS5048 (PWM) на PB8, в одиночку.
 *
 * Мотор не трогается вовсе — драйвер даже не инициализируется. Если датчик
 * не читается, я хочу знать это про датчик, а не гадать, чей это отказ.
 *
 * Что проверяется и почему именно это:
 *
 *   1. ЧАСТОТА ФРОНТОВ. У AS5048 период ШИМ около 1.2 мс, то есть ~840 Гц.
 *      Ноль фронтов — нет сигнала совсем (питание, земля, не тот пин).
 *      Частота сильно ниже — сигнал есть, но рвётся.
 *   2. ДЛИТЕЛЬНОСТЬ ИМПУЛЬСА. Должна лежать внутри окна 7..920 мкс, которым
 *      настроен драйвер датчика. Выход за окно означает, что окно подобрано не
 *      для этого экземпляра, и угол будет считаться неверно — молча.
 *   3. ШУМ НА НЕПОДВИЖНОМ ВАЛУ. Это тот самый разброс, который на прошлом
 *      стенде душила медиана. Число нужно, чтобы решить, нужна ли она здесь.
 *   4. ОХВАТ ПРИ ВРАЩЕНИИ РУКОЙ. Датчик может отдавать правдоподобные, но
 *      залипшие значения. Отличить залипание от работы можно только увидев,
 *      что угол ходит.
 *
 * Отдельно про уровни. Если плата датчика питается 5 В и не имеет своего
 * стабилизатора, на PB8 придёт логика 5 В. Здесь это видно косвенно: если
 * фронты считаются и длительности осмысленны — уровень принят. Если фронтов
 * нет, а питание есть, первый подозреваемый — именно уровень.
 *
 *   arduino-cli compile -b STMicroelectronics:stm32:Disco:pnum=B_G431B_ESC1 \
 *       --libraries libraries sensor_probe_g431
 */
#include <SimpleFOC.h>

#define SENSOR_PIN PB8

// Окно длительности — то же, что в боевой прошивке. Меряем ровно те
// настройки, с которыми поедем.
static const unsigned long SENS_MIN_US = 7;
static const unsigned long SENS_MAX_US = 920;

MagneticSensorPWM sensor = MagneticSensorPWM(SENSOR_PIN, SENS_MIN_US, SENS_MAX_US);
volatile uint32_t edges = 0;
void doPWM() { edges++; sensor.handlePWM(); }

static const uint32_t QUIET_MS = 5000;    // неподвижный вал: шум
static const uint32_t TURN_MS  = 15000;   // вращение рукой: охват

static float a_min = 1e9f, a_max = -1e9f;
static float q_min = 1e9f, q_max = -1e9f;      // то же, но только на покое
static unsigned long p_min = 0xFFFFFFFF, p_max = 0;

int main_stage = 0;

void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 2000) { }

  Serial.println();
  Serial.println(F("=== ESC1: проверка датчика AS5048 на PB8 ==="));
  Serial.print(F("окно длительности ")); Serial.print(SENS_MIN_US);
  Serial.print(F("..")); Serial.print(SENS_MAX_US); Serial.println(F(" мкс"));

  sensor.init();
  sensor.enableInterrupt(doPWM);

  Serial.println();
  Serial.println(F("ЭТАП 1 (5 с): НЕ ТРОГАЙТЕ вал — меряю шум покоя"));
}

void loop() {
  static uint32_t t_start = 0;
  static uint32_t last_report = 0;
  static uint32_t e_prev = 0;
  static uint32_t t_prev = 0;
  if (t_start == 0) { t_start = millis(); t_prev = t_start; last_report = t_start; }

  sensor.update();
  float a = sensor.getAngle();
  unsigned long p = sensor.pulse_length_us;

  uint32_t el = millis() - t_start;

  if (a < a_min) a_min = a;
  if (a > a_max) a_max = a;
  if (p < p_min) p_min = p;
  if (p > p_max) p_max = p;
  if (el < QUIET_MS) {
    if (a < q_min) q_min = a;
    if (a > q_max) q_max = a;
  }

  // Раз в секунду — частота фронтов за прошедшую секунду.
  if (millis() - last_report >= 1000) {
    uint32_t e = edges;
    uint32_t dt = millis() - t_prev;
    float hz = dt ? (e - e_prev) * 1000.0f / dt : 0.0f;
    e_prev = e; t_prev = millis(); last_report = millis();
    Serial.print(F("  t=")); Serial.print(el / 1000);
    Serial.print(F("с  фронтов/с ")); Serial.print(hz, 0);
    Serial.print(F("  импульс ")); Serial.print(p);
    Serial.print(F(" мкс  угол ")); Serial.print(a * 57.2958f, 2);
    Serial.println(F("°"));
  }

  if (main_stage == 0 && el >= QUIET_MS) {
    main_stage = 1;
    Serial.println();
    Serial.print(F("ШУМ ПОКОЯ: размах "));
    Serial.print((q_max - q_min) * 57.2958f, 4);
    Serial.println(F("°"));
    Serial.println();
    Serial.println(F("ЭТАП 2 (15 с): ПОВЕРНИТЕ вал рукой, хотя бы на пол-оборота"));
    a_min = 1e9f; a_max = -1e9f;   // охват считаем только за этап 2
  }

  if (main_stage == 1 && el >= QUIET_MS + TURN_MS) {
    main_stage = 2;
    float span = (a_max - a_min) * 57.2958f;
    Serial.println();
    Serial.println(F("=== ИТОГ ==="));
    Serial.print(F("длительность импульса: ")); Serial.print(p_min);
    Serial.print(F("..")); Serial.print(p_max); Serial.println(F(" мкс"));
    if (p_min < SENS_MIN_US || p_max > SENS_MAX_US)
      Serial.println(F("  ВНИМАНИЕ: вышли за окно — угол считается неверно"));
    else
      Serial.println(F("  внутри окна — ok"));
    Serial.print(F("шум покоя: размах "));
    Serial.print((q_max - q_min) * 57.2958f, 4); Serial.println(F("°"));
    Serial.print(F("охват при вращении: ")); Serial.print(span, 1); Serial.println(F("°"));
    if (span < 5.0f)
      Serial.println(F("  ЗАЛИПАНИЕ: угол не ходит, хотя вал крутили"));
    else
      Serial.println(F("  угол ходит — датчик живой"));
    Serial.print(F("всего фронтов: ")); Serial.println(edges);
    if (edges == 0)
      Serial.println(F("  СИГНАЛА НЕТ ВОВСЕ: питание, земля, пин или уровень"));
  }
}
