/*
 * B-G431B-ESC1: сколько стоит такт управления. Разовый замер.
 *
 * ЗАЧЕМ. Темп цикла управления на этой плате не измерен НИ РАЗУ — в
 * репозитории ноль чисел. А без него нельзя выбрать ни постоянную фильтра
 * скорости, ни окно вычисления скорости: PIDController считает dt адаптивно и
 * при выходе за границы подставляет догадку 1e-3, а LowPassFilter при dt
 * больше 0.3 с вовсе сбрасывается на сырой вход. Настраивать контур, не зная
 * темпа, значит настраивать вслепую.
 *
 * ЧТО МЕРЯЕТ. Стоимость КАЖДОЙ части такта по отдельности, накопительно.
 * Контур при этом НЕ ЗАМЫКАЕТСЯ: закрывать его без сторожа разгона рано, а
 * стоимость частей от замыкания не зависит — PID и оценка скорости считаются
 * одинаково, слушает их привод или нет.
 *
 * ПОЛЕ ВКЛЮЧАЕТСЯ на 0.5 В — вчетверо ниже разрешённых 2.0. Нужно потому, что
 * loopFOC() при выключенном моторе выходит рано и намерил бы не то. Уставка
 * нулевая, вал не поедет.
 *
 * Сборка: ./stm/build.sh looprate_g431 ; вывод на /dev/ttyACM0, 115200.
 */
#include <SimpleFOC.h>
#include "CaptureSensor.h"

static const uint8_t POLE_PAIRS = 11;
static const float   SUPPLY_V   = 12.0f;
static const float   U_PROBE    = 0.5f;    // много ниже правила 2.0 В
static const unsigned long SENS_MIN_US = 3, SENS_MAX_US = 919;
static const uint32_t WIN_MS = 3000;       // окно замера каждой ступени

MagneticSensorPWM sensor_isr = MagneticSensorPWM(PB8, SENS_MIN_US, SENS_MAX_US);
volatile uint32_t pwm_edges = 0;
void doPWM() { pwm_edges++; sensor_isr.handlePWM(); }

CaptureSensor  sensor_cap;
BLDCMotor      motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver6PWM driver = BLDCDriver6PWM(A_PHASE_UH, A_PHASE_UL,
                                       A_PHASE_VH, A_PHASE_VL,
                                       A_PHASE_WH, A_PHASE_WL);

// Накопительные ступени: каждая добавляет к предыдущей одну работу, поэтому
// цена ЧАСТИ = разность соседних, а не отдельный замер. Отдельные замеры
// врали бы: цикл без полезной работы компилятор волен переставить.
enum Step {
  S_EMPTY = 0, S_ISR, S_CAP, S_BOTH, S_VEL, S_PID, S_FOC, S_MOVE, S_N
};
static const char *NAMES[S_N] = {
  "пустой цикл",
  "+ sensor_isr.update()",
  "+ sensor_cap.update()",
  "+ оба тракта",
  "+ getVelocity() обоих",
  "+ PID_velocity()",
  "+ loopFOC()",
  "+ move(0) разомкнутый",
};

static volatile float sink = 0.0f;   // чтобы работу не выбросил оптимизатор

static uint32_t runStep(int step, uint32_t win_ms) {
  uint32_t n = 0;
  uint32_t t0 = millis();
  while (millis() - t0 < win_ms) {
    if (step >= S_ISR)  sensor_isr.update();
    if (step >= S_CAP)  sensor_cap.update();
    if (step >= S_VEL)  { sink += sensor_isr.getVelocity();
                          sink += sensor_cap.getVelocity(); }
    if (step >= S_PID)  sink += motor.PID_velocity(0.001f);
    if (step >= S_FOC)  motor.loopFOC();
    if (step >= S_MOVE) motor.move(0.0f);
    n++;
  }
  return n;
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 2500) { }
  Serial.println();
  Serial.println(F("=== ТЕМП ЦИКЛА УПРАВЛЕНИЯ, B-G431B-ESC1 ==="));
  Serial.print(F("окно ")); Serial.print(WIN_MS); Serial.println(F(" мс на ступень"));

  sensor_isr.init();
  sensor_isr.enableInterrupt(doPWM);
  sensor_cap.init();

  driver.voltage_power_supply = SUPPLY_V;
  driver.voltage_limit = SUPPLY_V;
  if (!driver.init()) { Serial.println(F("ДРАЙВЕР НЕ ПОДНЯЛСЯ")); return; }
  motor.linkDriver(&driver);
  motor.controller = MotionControlType::velocity_openloop;
  motor.voltage_limit = U_PROBE;
  motor.PID_velocity.P = 0.2f;
  motor.PID_velocity.I = 5.0f;
  motor.PID_velocity.D = 0.0f;
  motor.LPF_velocity.Tf = 0.005f;
  motor.init();
  motor.disable();

  // ПРОГРЕВ СТОИТ ПОСЛЕ motor.init(), И ЭТО НЕ КОСМЕТИКА. В первой редакции
  // он стоял до инициализации привода, и тракт захвата признавал себя
  // негодным при полностью исправном железе. Порядок подобран опытом: ровно
  // так работает stm/elzero_g431. Причина не разобрана до конца, и это
  // записано честно — трогать порядок без замера нельзя.
  for (int i = 0; i < 60; i++) { sensor_isr.update(); sensor_cap.update(); _delay(2); }

  Serial.print(F("тракт ISR фронтов ")); Serial.print(pwm_edges);
  Serial.print(F(", тракт CAP годен ")); Serial.println(sensor_cap.isHealthy() ? 1 : 0);

  // ДИАГНОСТИКА ОТКАЗА ПЕЧАТАЕТСЯ ВСЕГДА, а не только при отказе. Скетч уже
  // дважды отказал с одинаковым «тракт мёртв», и по этому сообщению нельзя
  // отличить оборванный провод от неверного порога внутри класса. Гадать
  // дальше дороже, чем напечатать разбор.
  const CaptureStatus &st = sensor_cap.lastStatus();
  Serial.print(F("  разбор: period=")); Serial.print(st.period);
  Serial.print(F(" high="));            Serial.print(st.high);
  Serial.print(F(" age="));             Serial.println(st.age);
  Serial.print(F("  признаки: paired=")); Serial.print(st.paired);
  Serial.print(F(" period_ok="));         Serial.print(st.period_ok);
  Serial.print(F(" width_ok="));          Serial.print(st.width_ok);
  Serial.print(F(" edges="));             Serial.print(st.edges);
  Serial.print(F(" lost="));              Serial.print(st.lost);
  Serial.print(F(" cc2if="));             Serial.print(st.cc2if);
  Serial.print(F(" ok="));                Serial.println(st.ok);
  Serial.print(F("  негодных подряд: ")); Serial.println(sensor_cap.badStreak());
  Serial.print(F("  сырой угол: "));      Serial.println(sensor_cap.getAngle(), 4);
  Serial.print(F("  для сверки, угол ISR: ")); Serial.println(sensor_isr.getAngle(), 4);

  if (pwm_edges == 0 || !sensor_cap.isHealthy()) {
    Serial.println(F("ОТКАЗ: тракт захвата не признаёт себя годным"));
    return;
  }

  Serial.println();
  Serial.println(F("ступень                       Гц      мкс/такт   цена части, мкс"));
  // float, А НЕ uint32_t: целочисленная prev_us обрезала дробную часть,
  // и колонка разностей врала (5.61-1.48 печаталось как 4.61).
  float prev_us = 0.0f;
  for (int st = 0; st < S_N; st++) {
    // Поле поднимаем только там, где без него loopFOC() выйдет рано.
    if (st == S_FOC) { motor.enable(); }
    uint32_t n = runStep(st, WIN_MS);
    float hz = n * 1000.0f / WIN_MS;
    float us = 1e6f / hz;
    Serial.print(NAMES[st]);
    for (int k = strlen(NAMES[st]); k < 30; k++) Serial.print(' ');
    Serial.print(hz, 0);      Serial.print(F("      "));
    Serial.print(us, 2);      Serial.print(F("      "));
    if (st == 0) Serial.println(F("-"));
    else         Serial.println(us - prev_us, 2);
    prev_us = us;
  }
  motor.move(0.0f);
  motor.disable();

  Serial.println();
  Serial.println(F("ПОСЛЕДНЯЯ строка — полный такт разомкнутого контура."));
  Serial.println(F("Замкнутый добавит к нему только PID и оценку скорости,"));
  Serial.println(F("а они уже посчитаны ступенями выше."));
  Serial.println(F("=== ВСЁ, поле снято ==="));
}

void loop() { digitalWrite(LED_BUILTIN, (millis() >> 9) & 1); }
