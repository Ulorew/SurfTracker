/*
 * Мост телефон -> мотор, протокол v2.
 * Спецификация: docs/ПРОТОКОЛ_ТЕЛЕФОН_STM32.md (там же контрольные векторы,
 * прогнанные на этом железе прошивкой stm/proto_test).
 *
 * Железо: Nucleo-F411RE + SimpleFOCShield v2.0.4 + BGM4108 + AS5048 (PWM).
 * Значения перенесены из full_control.ino — скетча, на котором стенд реально
 * работал.
 *
 * ЯДРО: собирать ядром STM32 2.10.1. На 3.0.0 HardwareSerial стал абстрактным
 * и конструктор с пинами исчезает; плюс SimpleFOC 2.3.5 с 3.0.0 несовместим
 * (2.4.0 — да). Автообновление ядра уже ломало сборку дважды.
 *
 * ВАЖНО: канал БИНАРНЫЙ. Ни одного Serial.print, ни SimpleFOCDebug, ни
 * Commander — отладочный текст уедет в тот же поток и приёмник будет
 * разбирать мусор. Отладка — миганием LD2 и битами статуса.
 *
 * ПРИНЦИП: пакет — это УСТАВКА, а не команда. Вал ведёт рампа, работающая на
 * частоте цикла; любой скачок уставки превращается в наклон рампы. Канал
 * может дёргаться — вал не может.
 */

#include <SimpleFOC.h>
#include <proto_v2.h>

// ============================ СБОРКА ============================
#define MOTOR_ENABLED 1

// 1 — протокол на ST-LINK VCP (отладка проводом с ноутбука).
// 0 — на USART1, к ESP32. VCP свободен, но говорить по нему нельзя.
#define LINK_ON_VCP 0

// ==================== ПРОФИЛЬ ПЛАТЫ ====================
//
// Один скетч на две платы, а не две копии: копии расходятся, и через неделю
// уже не сказать, в какой из них живёт исправление.
//
// B-G431B-ESC1: силовая часть на самой плате, межплатных проводов нет.
// Наружу выведены ровно три сигнала общего назначения — hall-разъём PB6, PB7,
// PB8, — и они закрывают ровно наши три потребности: две на UART к ESP32,
// одна на вход датчика. Запаса нет; следующий свободный пин был бы уже на
// CAN-разъёме.
#if defined(ARDUINO_B_G431B_ESC1)
  #define LINK_RX_PIN PB7    // A_HALL2, USART1_RX  <- TX ESP32
  #define LINK_TX_PIN PB6    // A_HALL1, USART1_TX  -> RX ESP32
  #define SENSOR_PIN  PB8    // A_HALL3, PWM-выход AS5048
  // Карта пинов USART1 добавляется файлом uart_pinmap_g431.c — в варианте
  // платы её нет, см. пояснение там.
#else
  // Nucleo-F411RE + SimpleFOCShield. НЕ PA9/PA10: PA9 это D8, занят под enable
  // драйвера BLDCDriver3PWM(9,5,6,8). Альтернативная пара того же USART1, оба
  // пина шилд не использует.
  #define LINK_RX_PIN PA10   // D2
  #define LINK_TX_PIN PB6    // D10
  #define SENSOR_PIN  3
#endif

// ==================== ПАРАМЕТРЫ (в лог по §3) ====================
static const uint32_t BAUD           = 115200;
static const uint32_t WATCHDOG_MS    = 300;    // нет кадра дольше -> уставка 0
static const uint32_t EXTRAP_CAP_MS  = 150;    // потолок интегрирования w_dot
static const float    SETPOINT_LIMIT = 2.0f;   // рад/с, ~115 град/с
static const float    HW_LIMIT       = 6.0f;   // рад/с, граница безопасности вала
static const float    MAX_ACCEL      = 1.0f;   // рад/с^2, рампа
// 2.0 В. История ручки такая: на 1.5 В тихо, но рябь скорости в разомкнутом
// контуре 10.5% СКО от команды. На 2.5 В появился высокочастотный шум и
// треск. Версия Hero — плохой контакт фазы, а не сама величина напряжения
// (на замкнутом контуре 3 В шли тихо). 2.0 В взято как нижний край
// диапазона: если виноват контакт, будет тихо; если колебания ротора без
// демпфирования — шум вырастет плавно, и это будет видно.
static const float    VOLTAGE_LIMIT  = 2.0f;   // В
static const float    SUPPLY_V       = 12.0f;
static const uint8_t  POLE_PAIRS     = 11;
// Окно длительности импульса AS5048. ИЗМЕРЕНО на этом экземпляре, а не
// взято из даташита: полный оборот рукой дал 3..919 мкс при периоде 921 мкс.
// Сходится с устройством датчика — кадр 4119 тактов, минимум около 16 тактов.
//
// Прежнее значение 7 приехало со старого стенда и было неверным: по этим
// границам MagneticSensorPWM линейно отображает импульс в угол, так что
// задранная вдвое нижняя граница смещала ВЕСЬ масштаб. Ошибка тихая —
// показания остаются правдоподобными.
static const unsigned long SENS_MIN_US = 3;
static const unsigned long SENS_MAX_US = 919;

// Детектор срыва: расхождение угла с интегралом команды на окне 1 с.
static const float    SLIP_THRESHOLD = 3.0f * PI / 180.0f;   // 3 градуса
static const uint16_t SLIP_WINDOW_MS = 1000;
static const uint16_t SLIP_STEP_MS   = 10;                    // шаг кольца
static const uint8_t  SLIP_N         = SLIP_WINDOW_MS / SLIP_STEP_MS;

// Медиана телеметрии: 5 отсчётов С ИНТЕРВАЛОМ. ТОЛЬКО в телеметрию — в
// управлении угол не участвует вовсе (контур разомкнут по скорости,
// положение замыкает камера).
//
// Интервал обязателен. Первая редакция набирала отсчёты КАЖДОЙ итерацией
// цикла, а он идёт на килогерце: медиана покрывала ~5 мс и душила только
// самое быстрое дрожание. Замер показал, что улучшения нет. При 20 мс окно
// медианы 100 мс — сравнимо с периодом телеметрии, то есть фильтр видит то
// же дрожание, что и потребитель лога.
static const uint8_t  MED_N       = 5;
static const uint16_t MED_STEP_MS = 20;

// Диагностическая сборка: слать в телеметрию СЫРОЙ угол. Нужна ровно для
// одного — сравнить фильтр с отсутствием фильтра на ОДНОМ И ТОМ ЖЕ
// неподвижном вале. Сравнивать два разных прогона нельзя: положение вала и
// распределение по уровням квантования у них разные, и вывод получается
// про выборку, а не про фильтр.
#define TELEMETRY_RAW_THETA 0

// SETPOINT_LIMIT < HW_LIMIT, поэтому аппаратный предел при штатной работе не
// срабатывает никогда. Так и задумано: он страхует от будущего поднятия
// предела уставки и от бага моста, а не участвует в обычном цикле.

// ============================ ЖЕЛЕЗО ============================
MagneticSensorPWM sensor = MagneticSensorPWM(SENSOR_PIN, SENS_MIN_US, SENS_MAX_US);
volatile uint32_t pwm_edges = 0;
void doPWM() { pwm_edges++; sensor.handlePWM(); }

#if MOTOR_ENABLED
BLDCMotor      motor  = BLDCMotor(POLE_PAIRS);
// На ESC1 ключи управляются шестью сигналами (верх и низ каждой стойки
// раздельно), на шилде — тремя. Это разные классы драйвера, а не разная
// раскладка: подставить одни пины в другой конструктор нельзя.
#if defined(ARDUINO_B_G431B_ESC1)
BLDCDriver6PWM driver = BLDCDriver6PWM(A_PHASE_UH, A_PHASE_UL,
                                       A_PHASE_VH, A_PHASE_VL,
                                       A_PHASE_WH, A_PHASE_WL);
#else
BLDCDriver3PWM driver = BLDCDriver3PWM(9, 5, 6, 8);
#endif
#endif

#if LINK_ON_VCP
  #define LINK Serial
#else
  HardwareSerial LinkUart(LINK_RX_PIN, LINK_TX_PIN);
  #define LINK LinkUart
#endif

// ============================ СОСТОЯНИЕ ============================
static uint8_t  rxbuf[proto::REQ_LEN];
static uint8_t  rxn = 0;

static float    w_pkt = 0.0f, wdot_pkt = 0.0f;   // последняя ПРИНЯТАЯ уставка
static uint32_t t_rx_ms = 0;
static uint8_t  last_seq = 0;
static bool     had_first = false;

static float    w_ramp = 0.0f;                   // исполняемая команда
static uint32_t loop_prev_us = 0;

// Watchdog ЗАЩЁЛКИВАЕТСЯ. Сообщать «таймаут прямо сейчас» в ответе
// невозможно по построению: ответ шлётся на пришедший кадр, а он таймаут и
// снимает. Полезен другой смысл — «пока тебя не было, я остановился», и он
// требует защёлки, которая держится до первого доклада.
// В v1 эта же ошибка была найдена эхо-тестом и исправлена; в v2 я повторил
// её заново, и её снова нашёл тест, а не чтение кода.
static bool     st_watchdog = true;   // текущее состояние
static bool     wd_latch = true;      // было ли срабатывание с прошлого доклада
static bool     st_extrap_cap = false;
static bool     st_ramp_sat = false;
static bool     st_clamp = false;
static bool     st_slip = false;
static uint8_t  crc_err_count = 0;    // по модулю 4, биты 6-7

// кольцо детектора срыва: угол и накопленный интеграл команды
static float    slip_theta[SLIP_N];
static float    slip_integ[SLIP_N];
static uint8_t  slip_i = 0;
static bool     slip_full = false;
static uint32_t slip_last_ms = 0;
static float    integ_cmd = 0.0f;     // накопленный интеграл w_ramp

static float    med_buf[MED_N];
static uint8_t  med_i = 0;
static bool     med_full = false;
static uint32_t med_last_ms = 0;

static void slipReset() {
  slip_i = 0; slip_full = false; integ_cmd = 0.0f; st_slip = false;
}

/** Медиана пяти без сортировки массива-источника: копия и три прохода
 *  выбором. На пяти элементах это дешевле любой библиотечной сортировки. */
static float median5(const float *a, uint8_t n) {
  float c[MED_N];
  for (uint8_t i = 0; i < n; i++) c[i] = a[i];
  for (uint8_t i = 0; i <= n / 2; i++) {
    uint8_t m = i;
    for (uint8_t j = i + 1; j < n; j++) if (c[j] < c[m]) m = j;
    float t = c[i]; c[i] = c[m]; c[m] = t;
  }
  return c[n / 2];
}

static float thetaFiltered() {
#if TELEMETRY_RAW_THETA
  return sensor.getAngle();
#else
  return med_full ? median5(med_buf, MED_N)
                   : (med_i ? med_buf[med_i - 1] : sensor.getAngle());
#endif
}

static uint8_t statusByte() {
  uint8_t st = 0;
  if (wd_latch) st |= proto::ST_WATCHDOG;
  wd_latch = false;
  if (st_extrap_cap) st |= proto::ST_EXTRAP_CAP;
  if (st_ramp_sat)   st |= proto::ST_RAMP_SAT;
  // Живость энкодера: свежие фронты И длительность в допуске. Одного
  // диапазона мало — pulse_length_us пишется только на фронте, поэтому
  // оборванный провод оставлял бы последнее валидное значение навсегда.
  static uint32_t edges_seen = 0;
  uint32_t e = pwm_edges;
  bool fresh = (e != edges_seen);
  edges_seen = e;
  if (fresh && sensor.pulse_length_us >= SENS_MIN_US &&
      sensor.pulse_length_us <= SENS_MAX_US) st |= proto::ST_ENC_OK;
  if (st_clamp) st |= proto::ST_CLAMP;
  if (st_slip)  st |= proto::ST_SLIP;
  st |= (uint8_t)((crc_err_count & 0x03) << proto::ST_CRC_SHIFT);
  return st;
}

static void sendTelemetry(uint8_t seq) {
  uint8_t out[proto::TEL_LEN];
  proto::buildTel(out, seq, thetaFiltered(), w_ramp, statusByte());
  LINK.write(out, proto::TEL_LEN);
}

// ============================ ПРИЁМ ============================
//
// Ресинхронизация по МАГИКУ, а не по длине: при потере одного байта
// выравнивание по длине залипает навсегда.
static void rxShift() {
  for (uint8_t i = 1; i < rxn; i++) rxbuf[i - 1] = rxbuf[i];
  rxn--;
  while (rxn > 0 && rxbuf[0] != proto::MAGIC_REQ) {
    for (uint8_t i = 1; i < rxn; i++) rxbuf[i - 1] = rxbuf[i];
    rxn--;
  }
}

static void pump() {
  while (LINK.available() > 0) {
    uint8_t b = (uint8_t)LINK.read();
    if (rxn == 0 && b != proto::MAGIC_REQ) continue;
    rxbuf[rxn++] = b;
    if (rxn < proto::REQ_LEN) continue;

    uint8_t seq = 0, ver = 0;
    float w = 0, wd = 0;
    if (!proto::parseReq(rxbuf, &seq, &w, &wd, &ver)) {
      crc_err_count++;
      rxShift();                      // битый кадр: ответа НЕТ
      continue;
    }
    rxn = 0;
    if (ver != proto::VERSION) continue;       // чужая версия — молчим
    if (isnan(w) || isinf(w) || isnan(wd) || isinf(wd)) {
      crc_err_count++;                // мусор, случайно прошедший CRC
      continue;
    }

    // Свежесть. Кадр вне окна (перезапуск телефона) принимается безусловно:
    // рампа сгладит. Устаревший кадр пачки НЕ применяется, но ответ на него
    // шлётся — иначе телефон не сможет сопоставить его по seq и посчитать.
    bool fresh = !had_first || proto::isFresher(seq, last_seq);
    bool out_of_window = had_first && !fresh &&
                          ((uint8_t)((seq - last_seq) & 0x7F) > 64);
    if (fresh || out_of_window) {
      if (st_watchdog) slipReset();   // возобновление после watchdog
      w_pkt = w; wdot_pkt = wd;
      t_rx_ms = millis();
      last_seq = seq;
      had_first = true;
      st_watchdog = false;
    }
    sendTelemetry(seq);
  }
}

// ============================ ЦИКЛ ============================
void setup() {
  LINK.begin(BAUD);
  pinMode(LED_BUILTIN, OUTPUT);

  sensor.init();
  sensor.enableInterrupt(doPWM);

#if MOTOR_ENABLED
  // Датчик НЕ связывается с мотором и initFOC() НЕ зовётся: контур
  // разомкнутый, выравнивание ему не нужно, а initFOC() двигает мотор при
  // включении и ставит диагностический датчик в критический путь.
  driver.voltage_power_supply = SUPPLY_V;
  driver.init();
  motor.linkDriver(&driver);
  motor.controller = MotionControlType::velocity_openloop;
  motor.voltage_limit  = VOLTAGE_LIMIT;
  motor.velocity_limit = HW_LIMIT;
  motor.init();
#endif

  loop_prev_us = millis() * 1000UL;
  slip_last_ms = millis();
  slipReset();
}

void loop() {
  uint32_t now_us = micros();
  float dt = (now_us - loop_prev_us) * 1e-6f;
  loop_prev_us = now_us;
  if (dt < 0.0f || dt > 0.05f) dt = 0.0f;

  // Датчик обновляется явно: loopFOC(), который делал это раньше, убран
  // вместе с обратной связью.
  sensor.update();
  float theta = sensor.getAngle();
  if (millis() - med_last_ms >= MED_STEP_MS) {
    med_last_ms = millis();
    med_buf[med_i] = theta;
    med_i = (uint8_t)((med_i + 1) % MED_N);
    if (med_i == 0) med_full = true;
  }

  pump();

  uint32_t now = millis();

  // --- уставка: экстраполяция по w_dot с потолком ---
  float goal;
  if (!had_first || (now - t_rx_ms) > WATCHDOG_MS) {
    goal = 0.0f;
    if (!st_watchdog) slipReset();    // вход в watchdog сбрасывает интеграл
    st_watchdog = true;
    wd_latch = true;
    st_extrap_cap = false;
  } else {
    uint32_t age = now - t_rx_ms;
    st_extrap_cap = age > EXTRAP_CAP_MS;
    float t = (st_extrap_cap ? EXTRAP_CAP_MS : age) * 1e-3f;
    goal = w_pkt + wdot_pkt * t;
  }

  // --- пределы: уставка, затем аппаратный ---
  st_clamp = false;
  if (goal >  SETPOINT_LIMIT) { goal =  SETPOINT_LIMIT; st_clamp = true; }
  if (goal < -SETPOINT_LIMIT) { goal = -SETPOINT_LIMIT; st_clamp = true; }
  if (goal >  HW_LIMIT) { goal =  HW_LIMIT; st_clamp = true; }
  if (goal < -HW_LIMIT) { goal = -HW_LIMIT; st_clamp = true; }

  // --- рампа: последний рубеж, активна всегда ---
  float step = MAX_ACCEL * dt;
  st_ramp_sat = fabsf(goal - w_ramp) > step && step > 0.0f;
  if (w_ramp < goal) w_ramp = (w_ramp + step > goal) ? goal : w_ramp + step;
  else if (w_ramp > goal) w_ramp = (w_ramp - step < goal) ? goal : w_ramp - step;

  integ_cmd += w_ramp * dt;

  // --- детектор срыва: угол против интеграла команды на окне 1 с ---
  if (now - slip_last_ms >= SLIP_STEP_MS) {
    slip_last_ms = now;
    slip_theta[slip_i] = theta;
    slip_integ[slip_i] = integ_cmd;
    slip_i = (uint8_t)((slip_i + 1) % SLIP_N);
    if (slip_i == 0) slip_full = true;
    if (slip_full) {
      float d_theta = theta - slip_theta[slip_i];      // slip_i — самый старый
      float d_cmd   = integ_cmd - slip_integ[slip_i];
      st_slip = fabsf(d_theta - d_cmd) > SLIP_THRESHOLD;
    }
  }

#if MOTOR_ENABLED
  // loopFOC() ОБЯЗАТЕЛЕН и в разомкнутом контуре — в SimpleFOC 2.4.0 именно
  // он считает электрический угол для open-loop и подаёт напряжение на фазы,
  // тогда как move() лишь обновляет shaft_angle и вычисляет current_sp.
  // В 2.3.x напряжение подавалось внутри velocityOpenloop, поэтому прежний
  // скетч стенда работал без него — и я убрал его «как ненужный».
  //
  // Симптом ошибки: driver.init()=1, motor.init()=1, enabled=1, shaft_angle
  // растёт ровно на заданной скорости, а на фазах 0 В и вал стоит.
  motor.loopFOC();
  motor.move(w_ramp);
#endif

  bool alive = had_first && !st_watchdog;
  digitalWrite(LED_BUILTIN, alive ? HIGH : ((now >> 8) & 1));
}
