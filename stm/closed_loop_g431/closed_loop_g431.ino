/*
 * B-G431B-ESC1: ПЕРВОЕ ЗАМЫКАНИЕ КОНТУРА СКОРОСТИ на тракт захвата (TIM2/PA15).
 *
 * Отвечает на один вопрос: даёт ли замыкание выигрыш в ПЛАВНОСТИ. Не в
 * точности скорости — в разомкнутом контуре она уже 0.987..1.003 от команды
 * во всех 25 ячейках матрицы «напряжение x скорость», улучшать нечего.
 * Мишень названа заранее: резонанс подвеса 5.5..6.5 Гц, добротность ~5,
 * подтверждён отношением спектров «с грузом / без груза» x5.5..8.4 при
 * контроле base_load.csv (груз висит, вал стоит) ровно x1.00.
 *
 * ============ ПОЧЕМУ ВСЕ ТРИ ЗАМЕРА В ОДНОЙ ПРОШИВКЕ, ЗА ОДНО ВКЛЮЧЕНИЕ ====
 *
 * Прошлый раз базовая линия снималась одним конвейером (телефон, 103.6 Гц,
 * полоса 1..20 Гц), а сравнивать её собирались с замером через VCP. Так
 * сравнивать нельзя, и это уже стоило нам таблицы, помеченной «непригодна».
 * Здесь пол прибора, разомкнутый и замкнутый пишутся ОДНИМ прибором (тракт
 * захвата), НА ОДНОМ ТЕМПЕ, В ОДНОМ ФАЙЛЕ, за одно включение питания. Тогда
 * из сравнения выпадают: разница трактов, разница темпа опроса, дрейф
 * выравнивания между включениями и смена температуры.
 *
 * ПОЛ ПРИБОРА ПИШЕТСЯ ПЕРВЫМ И ВСЕГДА. Без него мы уже однажды померили
 * собственный шум датчика и приняли за дрожание вала. Нижняя половина
 * лестницы разомкнутого контура (0.038 град при поле 0.032) лежала внутри
 * пола на 72% мощности.
 *
 * ============ ЧТО ЗДЕСЬ ЗАЩИЩЕНО И ПОЧЕМУ ИМЕННО ЭТО ======================
 *
 * Разобрано по исходникам SimpleFOC 2.4.0, не по документации.
 *
 * 1. velocity_limit В ЗАМКНУТОМ КОНТУРЕ СКОРОСТИ НЕ РАБОТАЕТ ВОВСЕ.
 *    FOCMotor.cpp:737 — ветка MotionControlType::velocity делает
 *    shaft_velocity_sp = target без единого _constrain, в отличие от ветки
 *    angle, где ограничение стоит явно. Поэтому сторож разгона здесь СВОЙ
 *    (см. guard()), а не библиотечный.
 *
 * 2. ЗАМЕРШИЙ ДАТЧИК ВЫГЛЯДИТ ИСПРАВНЫМ. Sensor::getVelocity() обновляет
 *    скорость только при изменении угла, а возвращает всегда — то есть вечно
 *    рапортует последнюю ненулевую. Регулятор увидит неподвижный вал при
 *    ненулевой уставке и упрёт напряжение в потолок. Поэтому потребляется
 *    sensor.isHealthy(), в котором с 23.08 есть признак живости таймера:
 *    замерший TIM2 по регистрам неотличим от исправного тракта на
 *    неподвижном валу, различает только время.
 *
 * 3. ВЫРАВНИВАНИЕ НЕ ЗАШИТО, А МЕРИТСЯ ЗДЕСЬ ЖЕ. Зашить было бы можно
 *    (разброс 0.88 град механических = 9.7 электрических = потеря момента
 *    1.4%), но прежние значения ZEA_CAP сняты, когда наклон моста в классе
 *    был 362.7, а теперь 362.07. Наклон меняет отображение «скважность ->
 *    угол», то есть и ZEA. Мерить заново дешевле, чем гадать, насколько
 *    старое число ещё верно.
 *
 *    Если zero_electric_angle И sensor_direction заданы, initFOC пропускает
 *    ОБА этапа выравнивания и напряжения на фазы не подаёт (FOCMotor.cpp:846
 *    и :908). Здесь они НЕ заданы намеренно — выравнивание нужно.
 *
 * 4. ТОКОВЫЙ ДАТЧИК НЕ ЛИНКУЕТСЯ. Если его прилинковать, initFOC() подаст
 *    напряжение на фазы даже при зашитых DIR и ZEA: alignCurrentSense()
 *    пропускается только флагом skip_align.
 *
 * 5. D-ЧЛЕН = 0 И ПОКА НЕ ТРОГАТЬ. pid.cpp считает D*(error-error_prev)/dt,
 *    а dt — это такт цикла (~30 мкс), тогда как кадр датчика 922 мкс: на 31
 *    такте из 32 приращение нулевое, а на 32-м делится на 30 мкс вместо 922
 *    и завышается в 31 раз.
 *
 * 6. NaN ПРОХОДИТ _constrain КАК МАКСИМУМ (макрос на двух сравнениях, с NaN
 *    оба ложны). Тракт захвата делит на период кадра; нулевой период дал бы
 *    полное напряжение на фазы. Гейт по периоду держит класс, снимать его
 *    нельзя.
 *
 * ============ НАПРЯЖЕНИЕ ==================================================
 *
 * 2.0 В — указание владельца для длительной непрерывной работы (2.5 В до
 * десяти минут, 3 В до минуты). Суммарное время движения здесь около 40 с.
 *
 * ============ ПОРЯДОК НАСТРОЙКИ, КОТОРЫЙ НЕ МЕНЯТЬ =======================
 *
 * Запаздывания окна вычисления скорости и фильтра СКЛАДЫВАЮТСЯ. Сперва
 * фильтр вниз, потом окно вверх. Обратный порядок уже стоил автоколебаний на
 * прежнем стенде.
 *
 * СИГНАЛЫ СВЕТОДИОДА: перед каждым отрезком мигает его номер, во время
 * записи горит ровно, во время выгрузки погашен. Частое мигание без конца —
 * сработал сторож, причина напечатана.
 *
 * ЗАПУСК:  stm/build.sh closed_loop_g431 --прошить
 *          затем  tools/stand/grab_vcp.py runs/closed_first.txt
 *          разбор tools/stand/jerk_metric.py runs/closed_first.txt
 */
#include <SimpleFOC.h>
#include <CaptureSensor.h>

// ---- что меряем ----------------------------------------------------------
//
// ОДНА СКОРОСТЬ, И ИМЕННО 0.20 РАД/С. Выбор не вкусовой: квант скорости
// тракта захвата за кадр 0.0438 рад/с, то есть на 0.05 рад/с он составляет
// 88% уставки — нижняя половина лестницы лежит ЗА ПРЕДЕЛОМ РАЗРЕШЕНИЯ
// обратной связи, и замыкать там нечего. На 0.20 разрешение в 4-5 раз лучше,
// и там же измерен вклад резонанса подвеса.
static const float    W_TEST    = 0.20f;
static const float    VOLTS     = 2.0f;
static const float    SUPPLY_V  = 12.0f;
static const uint8_t  POLE_PAIRS = 11;

static const uint16_t FS_HZ   = 200;
static const uint16_t N_SAMP  = 2400;          // 12 с при 200 Гц, бин 0.083 Гц
static const uint32_t SETTLE_MS = 3000;

// Коэффициенты — стартовая точка из full_control, где они подбирались на
// Nucleo при voltage_limit 1.0 В и без мёртвого времени. У 6PWM на G431
// мёртвая зона около 0.24 В, так что здесь они заведомо робкие. Это
// осознанно: первый пуск отвечает «есть ли эффект», а не «каков он лучший».
static const float    PID_P = 0.2f;
static const float    PID_I = 5.0f;
static const float    PID_D = 0.0f;
static const float    LPF_TF = 0.005f;

// ---- сторож разгона ------------------------------------------------------
//
// Порог 5x от уставки, а не «сколько-нибудь больше»: контур скорости на
// кванте 0.0438 рад/с шумит, и порог вплотную к уставке ловил бы шум
// измерения, а не разгон. Выдержка нужна по той же причине.
static const float    W_ABORT     = 1.0f;      // рад/с
static const uint32_t W_ABORT_MS  = 50;
// Упор напряжения сам по себе не авария (на трогании он нормален), аварией
// его делает ДЛИТЕЛЬНОСТЬ.
static const float    UQ_ABORT_FRAC = 0.98f;
static const uint32_t UQ_ABORT_MS   = 2000;

CaptureSensor  sensor;
BLDCMotor      motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver6PWM driver = BLDCDriver6PWM(A_PHASE_UH, A_PHASE_UL,
                                       A_PHASE_VH, A_PHASE_VL,
                                       A_PHASE_WH, A_PHASE_WL);

static float buf[N_SAMP];
static const char *abort_why = 0;

static void blink(uint8_t n, uint16_t ms) {
  for (uint8_t i = 0; i < n; i++) {
    digitalWrite(LED_BUILTIN, HIGH); delay(ms);
    digitalWrite(LED_BUILTIN, LOW);  delay(ms);
  }
}

/** Смертельно: гасит фазы, печатает причину, мигает без конца.
 *  ОСТАНОВ, А НЕ ПРОДОЛЖЕНИЕ С ОГОВОРКОЙ: любая из этих причин означает, что
 *  регулятор работает по недостоверному измерению, и данные после неё
 *  сравнивать не с чем. */
static void die(const char *why) {
  motor.move(0.0f);
  motor.disable();
  Serial.println();
  Serial.print(F("#ОТКАЗ ")); Serial.println(why);
  Serial.print(F("  скорость=")); Serial.print(motor.shaft_velocity, 4);
  Serial.print(F(" Uq="));        Serial.print(motor.voltage.q, 3);
  Serial.print(F(" годен="));     Serial.print(sensor.isHealthy());
  Serial.print(F(" подряд_негодных=")); Serial.println(sensor.badStreak());
  const CaptureStatus &st = sensor.lastStatus();
  Serial.print(F("  пара=")); Serial.print(st.paired);
  Serial.print(F(" период_ок=")); Serial.print(st.period_ok);
  Serial.print(F(" ширина_ок=")); Serial.print(st.width_ok);
  Serial.print(F(" фронты="));    Serial.print(st.edges);
  Serial.print(F(" жив="));       Serial.print(st.alive);
  Serial.print(F(" возраст="));   Serial.println(st.age);
  Serial.println(F("#ВСЁ"));
  Serial.flush();
  while (1) blink(1, 60);
}

/** Три сторожа, по одному на отказ, который в разомкнутом контуре не страшен.
 *  Вызывается из КАЖДОЙ итерации управления — включая холостые, иначе
 *  выдержки мерились бы в чтениях, а не во времени. */
static void guard(bool closed) {
  static uint32_t w_since = 0, uq_since = 0;
  const uint32_t now = millis();

  // ЗДОРОВЬЕ ДАТЧИКА — только в замкнутом. В разомкнутом датчик не участвует
  // в управлении, и его негодность портит замер, но не разгоняет вал.
  if (closed && !sensor.isHealthy()) die("датчик негоден: см. разбор ниже");

  const float w = fabsf(motor.shaft_velocity);
  if (w > W_ABORT) { if (!w_since) w_since = now; }
  else w_since = 0;
  if (w_since && now - w_since > W_ABORT_MS) die("разгон: скорость выше порога");

  const float uq = fabsf(motor.voltage.q);
  if (uq >= UQ_ABORT_FRAC * VOLTS) { if (!uq_since) uq_since = now; }
  else uq_since = 0;
  if (uq_since && now - uq_since > UQ_ABORT_MS) die("напряжение в упоре дольше выдержки");
}

/**
 * Один отрезок: установиться, потом записать N_SAMP отсчётов угла на FS_HZ.
 *
 * РАМПА, А НЕ СКАЧОК УСТАВКИ. В разомкнутом контуре скачок сорвал бы
 * синхронизм, и мы записали бы срыв вместо плавности. В замкнутом он дал бы
 * бросок Uq в упор на старте и разряд интегратора потом — то есть переходный
 * процесс внутри окна записи.
 *
 * drive=false — мотор выключен целиком: это ПОЛ ПРИБОРА. loopFOC() при этом
 * всё равно опрашивает датчик (FOCMotor.cpp:589 стоит ДО проверки enabled),
 * так что пол меряется тем же трактом и тем же кодом, что и остальное.
 */
static void record(bool drive, bool closed, float w) {
  const uint32_t step_us = 1000000UL / FS_HZ;
  float w_ramp = 0.0f;
  uint32_t prev = micros();
  uint32_t t0 = millis();

  while (millis() - t0 < SETTLE_MS || (drive && fabsf(w_ramp - w) > 1e-4f)) {
    uint32_t now = micros();
    float dt = (now - prev) * 1e-6f; prev = now;
    if (dt < 0 || dt > 0.05f) dt = 0;
    if (drive) {
      float st = 0.2f * dt;                  // рампа 0.2 рад/с^2
      if (w_ramp < w) w_ramp = min(w_ramp + st, w);
      else if (w_ramp > w) w_ramp = max(w_ramp - st, w);
    }
    motor.loopFOC();
    if (drive) motor.move(w_ramp);
    guard(closed);
  }

  uint32_t next = micros();
  for (uint16_t i = 0; i < N_SAMP; i++) {
    // Крутим и опрашиваем, пока не подошёл момент отсчёта. Спать нельзя:
    // без loopFOC() поле встанет, и мы запишем не плавность, а остановку.
    while ((int32_t)(micros() - next) < 0) {
      motor.loopFOC();
      if (drive) motor.move(w_ramp);
      guard(closed);
    }
    next += step_us;
    buf[i] = sensor.getAngle();
  }
}

/** Выгрузка сразу после отрезка, а не всех в конце: если следующий отрезок
 *  упрётся в сторож, уже снятое не пропадёт. */
static void dump(const char *tag, bool drive, float w) {
  Serial.print(F("#НАЧАЛО w=")); Serial.print(drive ? w : 0.0f, 4);
  Serial.print(F(" volts="));    Serial.print(drive ? VOLTS : 0.0f, 2);
  Serial.print(F(" fs="));       Serial.print(FS_HZ);
  Serial.print(F(" n="));        Serial.print(N_SAMP);
  Serial.print(F(" режим="));    Serial.println(tag);
  for (uint16_t i = 0; i < N_SAMP; i++) Serial.println(buf[i], 6);
  Serial.println(F("#КОНЕЦ"));
  Serial.flush();
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  blink(5, 80);

  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) { }
  Serial.println();
  Serial.println(F("=== ESC1: первое замыкание контура на тракт захвата ==="));
  Serial.print(F("#ПРОШИВКА closed_loop_g431 w=")); Serial.print(W_TEST, 3);
  Serial.print(F(" В=")); Serial.print(VOLTS, 2);
  Serial.print(F(" P=")); Serial.print(PID_P, 3);
  Serial.print(F(" I=")); Serial.print(PID_I, 3);
  Serial.print(F(" D=")); Serial.print(PID_D, 3);
  Serial.print(F(" Tf=")); Serial.print(LPF_TF, 4);
  Serial.print(F(" наклон=")); Serial.println(CAPSENS_DEG_PER_DUTY, 3);

  sensor.init();

  // ПРОВЕРКА ТРАКТА ДО ПОДАЧИ НАПРЯЖЕНИЯ. Класс впервые работает на железе;
  // если захват не поднялся, узнать это надо на неподвижном вале, а не по
  // поведению регулятора.
  for (uint8_t i = 0; i < 50; i++) { sensor.update(); delay(2); }
  const CaptureStatus &st = sensor.lastStatus();
  Serial.print(F("#ТРАКТ годен=")); Serial.print(sensor.isHealthy());
  Serial.print(F(" период=")); Serial.print(st.period);
  Serial.print(F(" ширина=")); Serial.print(st.high);
  Serial.print(F(" возраст=")); Serial.print(st.age);
  Serial.print(F(" жив=")); Serial.print(st.alive);
  Serial.print(F(" угол=")); Serial.println(sensor.getAngle(), 4);
  if (!sensor.isHealthy()) {
    Serial.println(F("#ОТКАЗ захват не поднялся — напряжение НЕ подавалось"));
    Serial.println(F("#ВСЁ")); Serial.flush();
    while (1) blink(2, 200);
  }

  driver.voltage_power_supply = SUPPLY_V;
  driver.voltage_limit = SUPPLY_V;
  driver.init();
  motor.linkDriver(&driver);
  motor.linkSensor(&sensor);

  motor.voltage_limit = VOLTS;
  motor.voltage_sensor_align = VOLTS;   // библиотечные 3.0 В здесь не годятся
  motor.velocity_limit = 6.0f;          // декоративен в этой ветке, см. шапку
  motor.PID_velocity.P = PID_P;
  motor.PID_velocity.I = PID_I;
  motor.PID_velocity.D = PID_D;
  motor.PID_velocity.limit = VOLTS;     // отдельно от voltage_limit: прямая
                                        // запись в voltage_limit его не трогает
  motor.LPF_velocity.Tf = LPF_TF;
  motor.controller = MotionControlType::velocity;
  motor.init();

  // ---- отрезок 0: выравнивание. Вал дёрнется. --------------------------
  Serial.println(F("#ВЫРАВНИВАНИЕ начато (вал дёрнется)"));
  if (!motor.initFOC()) {
    Serial.println(F("#ОТКАЗ выравнивание не удалось"));
    motor.disable(); Serial.println(F("#ВСЁ")); Serial.flush();
    while (1) blink(3, 200);
  }
  Serial.print(F("#ВЫРАВНИВАНИЕ ZEA=")); Serial.print(motor.zero_electric_angle, 5);
  Serial.print(F(" направление="));
  Serial.println(motor.sensor_direction == Direction::CW ? F("CW") : F("CCW"));

  // ---- отрезок 1: ПОЛ ПРИБОРА -------------------------------------------
  motor.disable();
  blink(1, 200); digitalWrite(LED_BUILTIN, HIGH);
  record(false, false, 0.0f);
  digitalWrite(LED_BUILTIN, LOW);
  dump("пол", false, 0.0f);

  // ---- отрезок 2: РАЗОМКНУТЫЙ -------------------------------------------
  //
  // Базовая линия снимается ПЕРЕД замкнутым намеренно: если замкнутый упрётся
  // в сторож, у нас всё равно останется линия, снятая этим же прибором.
  motor.enable();
  motor.controller = MotionControlType::velocity_openloop;
  blink(2, 200); digitalWrite(LED_BUILTIN, HIGH);
  record(true, false, W_TEST);
  digitalWrite(LED_BUILTIN, LOW);
  motor.move(0.0f);
  dump("разомкнутый", true, W_TEST);

  // ---- отрезок 3: ЗАМКНУТЫЙ ---------------------------------------------
  motor.controller = MotionControlType::velocity;
  motor.PID_velocity.reset();           // интегратор от прошлого отрезка
  blink(3, 200); digitalWrite(LED_BUILTIN, HIGH);
  record(true, true, W_TEST);
  digitalWrite(LED_BUILTIN, LOW);
  motor.move(0.0f);
  dump("замкнутый", true, W_TEST);

  motor.disable();
  Serial.println(F("#ВСЁ"));
  Serial.flush();
  while (1) blink(1, 600);
}

void loop() { }
