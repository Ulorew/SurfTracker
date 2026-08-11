/*
 * B-G431B-ESC1: проверка мотора в одиночку. Ни датчика, ни ESP, ни протокола.
 *
 * Два дела за один прогон:
 *
 *   1. Убедиться, что силовая часть новой платы вообще крутит мотор. После
 *      потери Nucleo это первое, что надо знать, и знать отдельно от всего
 *      остального: если что-то не работает, виноват ровно один слой.
 *
 *   2. Ответить на открытый вопрос §7 регламента — откуда взялся треск на
 *      2.5 В. На старом стенде разделить причины было нельзя: между
 *      контроллером и мотором стояли шилд, клеммник и провода. Здесь их нет
 *      вовсе — силовая часть на той же плате. Поэтому развёртка по напряжению
 *      на ОДНОЙ И ТОЙ ЖЕ скорости различает две версии:
 *
 *        треск повторился  -> дело в моторе и разомкнутом контуре
 *                             (версия про колебания ротора без демпфирования);
 *        треска нет        -> дело было в шилде или проводке.
 *
 *      Скорость держится постоянной, меняется ТОЛЬКО напряжение. Иначе
 *      сравнивать было бы нечего.
 *
 * Отладочный вывод идёт в Serial (USART2 -> встроенный ST-LINK VCP). На этой
 * плате это ОТДЕЛЬНЫЙ канал: бинарный протокол к ESP32 будет жить на USART1
 * (PB6/PB7), и печать ему не мешает. На Nucleo так было нельзя, там канал
 * приходилось держать стерильным и отлаживаться миганием.
 *
 * Сборка:
 *   arduino-cli compile -b STMicroelectronics:stm32:Disco:pnum=B_G431B_ESC1 \
 *       --libraries libraries motor_probe_g431
 */
#include <SimpleFOC.h>

// BGM4108, 11 пар полюсов — как на старом стенде.
static const uint8_t POLE_PAIRS = 11;
static const float   SUPPLY_V   = 12.0f;

// Развёртка. Ниже 1.0 В мотор может не тронуться, выше 3.0 В на верхней части
// диапазона уже возможен ощутимый нагрев — верхняя граница взята со слов Hero
// по прошлому стенду.
static const float    STEPS_V[]  = {1.0f, 1.5f, 2.0f, 2.5f, 3.0f};
static const uint8_t  N_STEPS    = sizeof(STEPS_V) / sizeof(STEPS_V[0]);
static const uint32_t STEP_MS    = 6000;   // на слух этого хватает с запасом
static const float    PROBE_W    = 0.3f;   // рад/с, та же, что мерили вчера

BLDCMotor      motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver6PWM driver = BLDCDriver6PWM(A_PHASE_UH, A_PHASE_UL,
                                       A_PHASE_VH, A_PHASE_VL,
                                       A_PHASE_WH, A_PHASE_WL);

static uint8_t  step_i    = 0;
static uint32_t step_t0   = 0;
static bool     finished  = false;

void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 3000) { }

  Serial.println();
  Serial.println(F("=== B-G431B-ESC1: одиночная проверка мотора ==="));
  Serial.print(F("пар полюсов ")); Serial.print(POLE_PAIRS);
  Serial.print(F(", питание ")); Serial.print(SUPPLY_V); Serial.println(F(" В"));

  driver.voltage_power_supply = SUPPLY_V;
  // Предел ДРАЙВЕРА оставляем на питании, ограничивать будем предел МОТОРА:
  // именно его и крутит развёртка. Если зажать здесь, шаги выше упрутся в
  // потолок молча, и график получится не про то.
  driver.voltage_limit = SUPPLY_V;
  int dok = driver.init();
  Serial.print(F("driver.init() = ")); Serial.println(dok);
  if (!dok) {
    Serial.println(F("ДРАЙВЕР НЕ ПОДНЯЛСЯ — дальше идти незачем"));
    return;
  }
  motor.linkDriver(&driver);

  // Разомкнутый контур по скорости. Датчик не подключён и не нужен: положение
  // в изделии замыкает камера, а не энкодер.
  motor.controller   = MotionControlType::velocity_openloop;
  motor.voltage_limit = STEPS_V[0];

  int mok = motor.init();
  Serial.print(F("motor.init() = ")); Serial.println(mok);
  // initFOC() НЕ вызывается намеренно: он выравнивает датчик, которого здесь
  // нет, и в разомкнутом контуре не нужен.

  Serial.println();
  Serial.println(F("Развёртка: скорость 0.3 рад/с постоянна, меняется только"));
  Serial.println(F("напряжение. Слушайте. При треске — СНИМИТЕ ПИТАНИЕ."));
  Serial.println(F("Шаг 6 с. Всего 5 шагов, ~30 с."));
  Serial.println();

  step_t0 = millis();
  Serial.print(F(">>> шаг 1/5: ")); Serial.print(STEPS_V[0]); Serial.println(F(" В"));
}

void loop() {
  if (finished) return;

  if (millis() - step_t0 >= STEP_MS) {
    step_i++;
    step_t0 = millis();
    if (step_i >= N_STEPS) {
      motor.move(0.0f);
      motor.disable();
      finished = true;
      Serial.println();
      Serial.println(F("=== развёртка закончена, мотор обесточен ==="));
      Serial.println(F("Если треска не было ни на одном шаге — версия про"));
      Serial.println(F("колебания ротора НЕ подтверждается, и вчерашний треск"));
      Serial.println(F("был от шилда или проводки."));
      return;
    }
    motor.voltage_limit = STEPS_V[step_i];
    Serial.print(F(">>> шаг ")); Serial.print(step_i + 1); Serial.print(F("/5: "));
    Serial.print(STEPS_V[step_i]); Serial.println(F(" В"));
  }

  // loopFOC() ОБЯЗАТЕЛЕН и в разомкнутом контуре. В SimpleFOC 2.4.0 именно он
  // выкладывает фазное напряжение; в 2.3.x это делалось внутри
  // velocityOpenloop. Его отсутствие один раз уже стоило вечера отладки:
  // driver.init()=1, motor.init()=1, enabled=1, а на фазах ноль.
  motor.loopFOC();
  motor.move(PROBE_W);
}
