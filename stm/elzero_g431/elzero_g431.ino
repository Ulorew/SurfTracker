/*
 * B-G431B-ESC1: разовый замер выравнивания для замкнутого контура.
 *
 * ЗАЧЕМ. initFOC() крутит вал при КАЖДОМ включении: ищет направление датчика
 * (электрический оборот вперёд и назад, около двух секунд) и электрический
 * ноль (удержание поля на 3π/2). На стенде это терпимо, на мачте с камерой —
 * нет. SimpleFOC 2.4.0 пропускает оба шага, если оба поля заданы заранее:
 *   FOCMotor.cpp:861  if (sensor_direction == Direction::UNKNOWN) { ...крутит... }
 *   FOCMotor.cpp:908  if (!_isset(zero_electric_angle))          { ...держит... }
 * Значит надо задать ОБА: одного мало, второй шаг всё равно подаст напряжение.
 *
 * ПОЧЕМУ ОБА ТРАКТА ЗА ОДНУ УСТАНОВКУ РОТОРА. zero_electric_angle считается
 * от getMechanicalAngle() конкретного датчика, а тракты отображают импульс в
 * угол ПО-РАЗНОМУ: мост между шкалами измерен и он не тождественный (наклон
 * 362.7 град на единицу скважности, мёртвый участок кадра около процента).
 * Значит константа у каждого тракта СВОЯ, и сравнивать их можно только сняв
 * в одном и том же физическом положении вала.
 *
 * НАПРЯЖЕНИЕ 2.0 В — правило владельца для долгой работы. Умолчание
 * библиотеки voltage_sensor_align = 3.0 В (defaults.h) здесь не годится.
 *
 * Печатает и НИЧЕГО НЕ ЗАШИВАЕТ: числа переносит человек.
 * Сборка: ./stm/build.sh elzero_g431
 */
#include <SimpleFOC.h>
#include "CaptureSensor.h"

static const uint8_t POLE_PAIRS = 11;
static const float   SUPPLY_V   = 12.0f;
static const float   U_ALIGN    = 2.0f;
static const uint8_t N_REPEAT   = 5;

// Границы боевого тракта — ИЗМЕРЕННЫЕ на этом экземпляре, а не номинал
// даташита. Задранная нижняя граница смещает ВЕСЬ масштаб, и ошибка тихая.
static const unsigned long SENS_MIN_US = 3, SENS_MAX_US = 919;

static const float TWO_PI_F   = 6.28318530718f;
static const float THREE_PI_2 = 4.71238898038f;

MagneticSensorPWM sensor_isr = MagneticSensorPWM(PB8, SENS_MIN_US, SENS_MAX_US);
volatile uint32_t pwm_edges = 0;
void doPWM() { pwm_edges++; sensor_isr.handlePWM(); }

CaptureSensor sensor_cap;

BLDCMotor      motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver6PWM driver = BLDCDriver6PWM(A_PHASE_UH, A_PHASE_UL,
                                       A_PHASE_VH, A_PHASE_VL,
                                       A_PHASE_WH, A_PHASE_WL);

static float zeaFrom(float mech, int dir) {
  return _normalizeAngle((float)(dir * POLE_PAIRS) * mech);
}

// Среднее по КРУГУ, а не арифметическое. Углы живут на окружности: две
// величины по разные стороны от нуля (0.05 и 6.23) дают арифметическое
// среднее 3.14 — ровно противоположную точку. Ошибка тихая и правдоподобная.
static float meanCircular(const float *a, uint8_t n) {
  float sx = 0, sy = 0;
  for (uint8_t i = 0; i < n; i++) { sx += cosf(a[i]); sy += sinf(a[i]); }
  return _normalizeAngle(atan2f(sy, sx));
}
// Разброс по кругу: длина среднего вектора. 1.0 — все совпали, 0 — размазаны.
static float spreadCircular(const float *a, uint8_t n) {
  float sx = 0, sy = 0;
  for (uint8_t i = 0; i < n; i++) { sx += cosf(a[i]); sy += sinf(a[i]); }
  return sqrtf(sx * sx + sy * sy) / n;
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 2500) { }
  Serial.println();
  Serial.println(F("=== ВЫРАВНИВАНИЕ: направление и электрический ноль ==="));
  Serial.print(F("U_align=")); Serial.print(U_ALIGN, 2);
  Serial.print(F(" В, пар полюсов ")); Serial.println(POLE_PAIRS);

  sensor_isr.init();
  sensor_isr.enableInterrupt(doPWM);
  sensor_cap.init();
  delay(200);

  driver.voltage_power_supply = SUPPLY_V;
  driver.voltage_limit = SUPPLY_V;
  if (!driver.init()) { Serial.println(F("ДРАЙВЕР НЕ ПОДНЯЛСЯ")); return; }
  motor.linkDriver(&driver);
  // Датчик НЕ линкуем и initFOC() НЕ зовём: всё меряем руками, повторяя то,
  // что делает alignSensor(), — иначе он сам всё прокрутит и напечатает
  // только для одного тракта.
  motor.controller = MotionControlType::velocity_openloop;
  motor.voltage_limit = U_ALIGN;
  motor.init();
  motor.disable();          // init() включает поле сам (BLDCMotor.cpp)

  // ПРОГРЕВ ПЕРЕД ОПРОСОМ ГОДНОСТИ. isHealthy() у тракта захвата встаёт не
  // с первого чтения: годность возвращается только после нескольких подряд
  // годных, и это сделано намеренно — около процента чтений законно попадает
  // в мёртвый участок кадра, и признак, падающий от каждого, был бы бесполезен.
  // Первая редакция этого скетча спрашивала годность после ОДНОГО update() и
  // получала честный отказ при полностью исправном тракте.
  for (int i = 0; i < 60; i++) { sensor_isr.update(); sensor_cap.update(); _delay(2); }
  Serial.print(F("тракт ISR: фронтов ")); Serial.print(pwm_edges);
  Serial.print(F(", угол ")); Serial.println(sensor_isr.getAngle(), 4);
  Serial.print(F("тракт CAP: годен ")); Serial.print(sensor_cap.isHealthy() ? 1 : 0);
  Serial.print(F(", угол ")); Serial.print(sensor_cap.getAngle(), 4);
  Serial.print(F(", негодных подряд ")); Serial.println(sensor_cap.badStreak());
  if (pwm_edges == 0 || !sensor_cap.isHealthy()) {
    Serial.println(F("ОТКАЗ: тракт мёртв, замер недействителен"));
    return;
  }

  // ---- 1. НАПРАВЛЕНИЕ ------------------------------------------------------
  // Электрический оборот вперёд и назад, как FOCMotor::alignSensor(). Оба
  // тракта читают ОДИН магнит, значит направление обязано совпасть; если нет
  // — один из них отображает угол зеркально, и это надо чинить, а не зашивать.
  motor.enable();
  for (int i = 0; i <= 500; i++) {
    motor.setPhaseVoltage(U_ALIGN, 0, THREE_PI_2 + TWO_PI_F * i / 500.0f);
    sensor_isr.update(); sensor_cap.update(); _delay(2);
  }
  sensor_isr.update(); sensor_cap.update();
  float mid_isr = sensor_isr.getAngle(), mid_cap = sensor_cap.getAngle();
  for (int i = 500; i >= 0; i--) {
    motor.setPhaseVoltage(U_ALIGN, 0, THREE_PI_2 + TWO_PI_F * i / 500.0f);
    sensor_isr.update(); sensor_cap.update(); _delay(2);
  }
  sensor_isr.update(); sensor_cap.update();
  float end_isr = sensor_isr.getAngle(), end_cap = sensor_cap.getAngle();
  motor.setPhaseVoltage(0, 0, 0);
  _delay(300);

  float moved = fabsf(mid_isr - end_isr);
  Serial.print(F("\nсдвиг за электрический оборот, рад: ")); Serial.println(moved, 5);
  if (moved < TWO_PI_F / 101.0f) {
    Serial.println(F("ДВИЖЕНИЯ НЕ ЗАМЕЧЕНО — замер недействителен"));
    motor.disable(); return;
  }
  int dir_isr = (mid_isr < end_isr) ? -1 : 1;
  int dir_cap = (mid_cap < end_cap) ? -1 : 1;
  Serial.print(F("направление ISR = ")); Serial.println(dir_isr > 0 ? F("CW") : F("CCW"));
  Serial.print(F("направление CAP = ")); Serial.println(dir_cap > 0 ? F("CW") : F("CCW"));
  if (dir_isr != dir_cap)
    Serial.println(F("!!! ТРАКТЫ РАСХОДЯТСЯ ПО НАПРАВЛЕНИЮ — разбираться, не зашивать"));

  // Оценка числа пар полюсов. Задаром, а стоит дорого: зашив константы, мы
  // теряем эту самопроверку библиотеки навсегда.
  Serial.print(F("оценка пар полюсов по сдвигу: ")); Serial.println(TWO_PI_F / moved, 3);

  // ---- 2. ЭЛЕКТРИЧЕСКИЙ НОЛЬ ----------------------------------------------
  // Держим поле на 3π/2, ротор притягивается, читаем механический угол обоих
  // трактов. N_REPEAT раз со СБРОСОМ поля между попытками: трение оставляет
  // ротор недоехавшим, и разброс по попыткам — единственный способ увидеть,
  // насколько. Одна попытка выглядела бы точной при любой ошибке.
  float z_isr[N_REPEAT], z_cap[N_REPEAT];
  Serial.println(F("\n#  mech_isr  mech_cap   zea_isr   zea_cap   (рад)"));
  for (uint8_t k = 0; k < N_REPEAT; k++) {
    motor.setPhaseVoltage(0, 0, 0); _delay(400);
    // Подход с одной и той же стороны: иначе трение сместит по-разному.
    motor.setPhaseVoltage(U_ALIGN, 0, THREE_PI_2 + TWO_PI_F * 0.25f); _delay(400);
    motor.setPhaseVoltage(U_ALIGN, 0, THREE_PI_2); _delay(800);
    sensor_isr.update(); sensor_cap.update();
    float m_isr = sensor_isr.getMechanicalAngle();
    float m_cap = sensor_cap.getMechanicalAngle();
    z_isr[k] = zeaFrom(m_isr, dir_isr);
    z_cap[k] = zeaFrom(m_cap, dir_cap);
    Serial.print(k);          Serial.print('\t');
    Serial.print(m_isr, 5);   Serial.print('\t');
    Serial.print(m_cap, 5);   Serial.print('\t');
    Serial.print(z_isr[k], 5); Serial.print('\t');
    Serial.println(z_cap[k], 5);
  }
  motor.setPhaseVoltage(0, 0, 0);
  motor.disable();

  float zi = meanCircular(z_isr, N_REPEAT), si = spreadCircular(z_isr, N_REPEAT);
  float zc = meanCircular(z_cap, N_REPEAT), sc = spreadCircular(z_cap, N_REPEAT);
  Serial.println(F("\n=== ЗАШИВАТЬ ЭТО ==="));
  Serial.print(F("// тракт ISR (MagneticSensorPWM, PB8), кучность "));
  Serial.println(si, 4);
  Serial.print(F("static const Direction DIR_ISR = Direction::"));
  Serial.println(dir_isr > 0 ? F("CW;") : F("CCW;"));
  Serial.print(F("static const float     ZEA_ISR = ")); Serial.print(zi, 5);
  Serial.println(F("f;"));
  Serial.print(F("// тракт CAPTURE (TIM2, PA15), кучность ")); Serial.println(sc, 4);
  Serial.print(F("static const Direction DIR_CAP = Direction::"));
  Serial.println(dir_cap > 0 ? F("CW;") : F("CCW;"));
  Serial.print(F("static const float     ZEA_CAP = ")); Serial.print(zc, 5);
  Serial.println(F("f;"));
  Serial.println(F("# кучность: 1.0 = все попытки совпали, ниже 0.99 —"));
  Serial.println(F("# ротор не доезжает, зашивать рано"));
  Serial.println(F("=== ВСЁ, поле снято ==="));
}

void loop() { digitalWrite(LED_BUILTIN, (millis() >> 9) & 1); }
