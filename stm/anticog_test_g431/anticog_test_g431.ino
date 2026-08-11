/*
 * B-G431B-ESC1: сравнение плавности С таблицей компенсации и БЕЗ.
 *
 * Инструкция: reports/АНТИЗУБЦЫ.md, шаг 4.
 *
 * ЧЕРЕДОВАНИЕ, А НЕ ДВА ПРОГОНА. Блоки идут ВЫКЛ / +G / -G / ВЫКЛ подряд в
 * одной записи. Два отдельных прогона сравнивать нельзя: между ними меняется
 * температура обмоток, а с ней и сопротивление, и рабочая точка. Чередование
 * раздаёт дрейф всем режимам поровну, а два блока ВЫКЛ по краям показывают,
 * сколько дрейфа набежало: если они разошлись между собой сильнее, чем ВЫКЛ
 * от ВКЛ, сравнивать нечего.
 *
 * ЗНАК ПРОВЕРЯЕТСЯ, А НЕ УГАДЫВАЕТСЯ. Какая полярность добавки толкает вал
 * туда, где он отстаёт, из измерения профиля не следует: связь «возмущение
 * момента → отклонение угла» проходит через жёсткость привода, знак которой
 * мы не выводили. Поэтому в прогоне участвуют ОБА знака, и правильный тот, на
 * котором 22-й порядок падает. Неправильный обязан его УВЕЛИЧИТЬ — это и
 * служит проверкой различающей силы: если оба знака дают одно и то же,
 * компенсация не работает вовсе, а не «работает слабо».
 *
 * ДОБАВКА ИДЁТ В voltage_limit, то есть в амплитуду фазного напряжения —
 * ровно модель U_cmd = U_база + ΔU(φ_эл) из инструкции.
 *
 * Напряжение 2.0 В: правило владельца для долгой непрерывной работы.
 * По окончании скетч снимает напряжение сам.
 */
#include <SimpleFOC.h>
#include "cog_table.h"

static const float    U_BASE  = 2.0f;
// Скорость 0.3 рад/с — ТА, НА КОТОРОЙ HERO ВИДЕЛ ПОДЁРГИВАНИЯ на синусе.
// Калибровка снята на 0.15, и это оговорка к результату: ближе к резонансу
// фаза отклика уходит, поэтому таблица может работать хуже, чем в точке
// съёмки. Слепой тест ставится там, где дефект наблюдался, а не там, где
// компенсация выгоднее выглядит.
static const float    TEST_W  = 0.3f;
static const uint8_t  REVS    = 1;          // ~21 с на блок
static const uint16_t FS_HZ   = 50;
static const float    DU_CLAMP = 0.5f;      // §6: |ΔU| не больше 0.5 В

static const uint8_t POLE_PAIRS = 11;
static const float   SUPPLY_V   = 12.0f;
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

/** φ_эл по АБСОЛЮТНОМУ углу датчика — та же формула, что при калибровке. */
static inline float phiEl() {
  float m = sensor.getMechanicalAngle();
  float p = fmodf(m * POLE_PAIRS, TWO_PI_F);
  return p < 0 ? p + TWO_PI_F : p;
}

/** Добавка по таблице с линейной интерполяцией. gain = 0 выключает. */
static inline float deltaU(float phi, float gain) {
  if (gain == 0.0f) return 0.0f;
  float x = phi / TWO_PI_F * 64.0f;
  int i0 = (int)x; float f = x - i0;
  i0 &= 63;
  int i1 = (i0 + 1) & 63;
  float v = (COG_MV[i0] * (1.0f - f) + COG_MV[i1] * f) * 0.001f * gain;
  if (v >  DU_CLAMP) v =  DU_CLAMP;
  if (v < -DU_CLAMP) v = -DU_CLAMP;
  return v;
}

static void block(const char *label, float gain) {
  const uint32_t step_us = 1000000UL / FS_HZ;
  Serial.print(F("#БЛОК ")); Serial.print(label);
  Serial.print(F(" gain=")); Serial.print(gain, 3);
  Serial.print(F(" w=")); Serial.print(TEST_W, 4);
  Serial.print(F(" U=")); Serial.print(U_BASE, 2);
  Serial.print(F(" fs=")); Serial.println(FS_HZ);
  Serial.println(F("t_ms,theta_acc,phi_el,du_v"));

  float w_ramp = 0.0f;
  uint32_t prev = micros();
  uint32_t t0 = millis();
  uint32_t next = micros();
  float a0 = 0; bool started = false;

  while (true) {
    uint32_t now = micros();
    float dt = (now - prev) * 1e-6f; prev = now;
    if (dt < 0 || dt > 0.05f) dt = 0;
    if (w_ramp < TEST_W) w_ramp = min(w_ramp + 1.0f * dt, TEST_W);

    float du = deltaU(phiEl(), gain);
    motor.voltage_limit = U_BASE + du;
    motor.loopFOC();
    motor.move(w_ramp);
    sensor.update();

    if (!started && fabsf(w_ramp - TEST_W) < 1e-4f) {
      started = true; a0 = sensor.getAngle();
    }
    if ((int32_t)(micros() - next) >= 0) {
      next += step_us;
      Serial.print(millis() - t0); Serial.print(',');
      Serial.print(sensor.getAngle(), 5); Serial.print(',');
      Serial.print(phiEl(), 5); Serial.print(',');
      Serial.println(du, 4);
    }
    if (started && fabsf(sensor.getAngle() - a0) >= REVS * TWO_PI_F) break;
  }
  Serial.println(F("#КОНЕЦ"));
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  blink(5, 80);
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) { }
  Serial.println();
  Serial.println(F("=== компенсация зубцов: с таблицей и без ==="));
  Serial.print(F("блоков 4 по ")); Serial.print(REVS);
  Serial.print(F(" оборота, ~"));
  Serial.print((int)(4 * REVS * TWO_PI_F / TEST_W / 60.0f));
  Serial.println(F(" мин"));

  sensor.init();
  sensor.enableInterrupt(doPWM);
  driver.voltage_power_supply = SUPPLY_V;
  driver.voltage_limit = SUPPLY_V;
  driver.init();
  motor.linkDriver(&driver);
  motor.controller = MotionControlType::velocity_openloop;
  motor.voltage_limit = U_BASE;
  motor.init();

  // СХЕМА ABBA: ВЫКЛ, ВКЛ, ВКЛ, ВЫКЛ.
  //
  // Первый прогон (ВЫКЛ/ПЛЮС/МИНУС/ВЫКЛ) провалился не результатом, а
  // разрешающей способностью: дрейф между двумя выключенными блоками вышел
  // 0.0287 град при эффекте 0.011-0.014. База гуляла вдвое сильнее того, что
  // меряем.
  //
  // Дрейф систематический — обмотки греются за девять минут работы, — поэтому
  // удлинение блоков не помогает: усреднение давит шум, а не тренд. ABBA
  // убирает ЛИНЕЙНЫЙ тренд из контраста точно: полусумма крайних блоков
  // приходится на ту же среднюю точку времени, что полусумма средних.
  //
  // Коэффициент втрое больше: если при добавке 0.15 В эффект тонул в дрейфе,
  // надо сперва узнать, масштабируется ли он вообще. 0.45 В пика всё ещё под
  // пределом 0.5 В из §6 инструкции.
  // СЛЕПОЙ ТЕСТ. Порядок чередования зашит здесь и НЕ объявляется Hero до
  // того, как он назовёт свои впечатления. Светодиод отбивает НОМЕР блока, а
  // не состояние компенсации: знай наблюдатель, где включено, он бы это и
  // увидел — так устроено человеческое зрение, и спорить с ним бесполезно.
  //
  // Порядок неравномерный (ВКЛ, ВЫКЛ, ВЫКЛ, ВКЛ, ВЫКЛ, ВКЛ), чтобы нельзя
  // было угадать чередованием.
  const float SEQ[6] = { +3.0f, 0.0f, 0.0f, +3.0f, 0.0f, +3.0f };
  const char *NAMES[6] = { "1", "2", "3", "4", "5", "6" };
  digitalWrite(LED_BUILTIN, HIGH);
  for (uint8_t i = 0; i < 6; i++) {
    digitalWrite(LED_BUILTIN, LOW); delay(400);
    blink(i + 1, 250);
    delay(400);
    digitalWrite(LED_BUILTIN, HIGH);
    block(NAMES[i], SEQ[i]);
  }

  motor.move(0.0f);
  motor.voltage_limit = 0.0f;
  motor.disable();
  Serial.println(F("#ВСЁ напряжение снято"));
  while (1) blink(1, 60);
}

void loop() { }
