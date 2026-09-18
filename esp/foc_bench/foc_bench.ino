/*
 * Стенд контура на ESP32: параметры и прогоны задаются ПО SERIAL, а не
 * прошиваются.
 *
 * ЗАЧЕМ. Матрица 5 скоростей x 9 наборов коэффициентов — это 45 ячеек. С
 * зашитыми константами это 45 заливок, то есть 45 перезапусков initFOC, и
 * каждая ячейка мерялась бы после СВОЕГО выравнивания. Разброс выравнивания
 * (zero_electric_angle гулял 3.778..3.826 между запусками) попал бы в
 * результат как разброс коэффициентов, которого нет.
 *
 * ФОРМАТ ЗАПИСИ — ТОТ ЖЕ, что у стенда STM32, намеренно:
 *
 *     #НАЧАЛО w=0.600 volts=2.00 fs=200
 *     0.001234            <- угол в РАДИАНАХ, по одному числу в строке
 *     ...
 *     #КОНЕЦ n=1600 uq_avg=0.34 uq_max=0.80 v_est=0.601
 *
 * Так записи читаются существующими tools/stand/jerk_metric.py и
 * jerk_analyze.py без единой правки. Заводить свой формат значило бы завести
 * и вторую реализацию критерия дрожания — того самого, в котором в августе
 * нашли три ошибки сразу. Лишние поля в #КОНЕЦ старый разборщик игнорирует.
 *
 * Угол пишется ОТ НАЧАЛА ЗАПИСИ, а не абсолютный: за час матрицы вал
 * накрутил бы сотни радиан, и у float осталось бы 3e-5 рад разрешения —
 * грубее собственного шага датчика.
 *
 * КОМАНДЫ (по строке, регистр важен):
 *     SET P=0.5 I=10 D=0 Tf=0.05 PA=6 RAMP=200
 *     RUN v=0.6 t=8000 s=2000      прогон: разгон s мс, потом запись t мс
 *     OFF                          снять поле
 *     STATE                        что сейчас установлено
 *     DEMO                         профиль с непрерывно меняющейся скоростью
 *     FAST v=0.1 n=4000            пакетная запись 1 кГц В ПАМЯТЬ, потом выгрузка
 *
 * СТОРОЖ. Если поле включено и команд нет 60 с — драйвер гаснет сам. Забытый
 * под напряжением вал держит постоянный ток в одной паре обмоток; нагрев
 * локальный, и это хуже вращения.
 */

#include <SimpleFOC.h>

static const int PIN_IN1 = 17, PIN_IN2 = 16, PIN_IN3 = 4, PIN_EN = 22;
// РАСПИНОВКА СМЕНЕНА 18 сентября: SimpleFOCMini разведён на плату напрямую и
// закрывает правый ряд. SPI переехал на левый: CLK 14, MISO 27, MOSI 26, CS 25.
// Все четыре — обычные пины, без strapping и без флеша. GPIO 12 в этом ряду
// НЕ ЗАНИМАТЬ: он обязан быть НИЗКИМ при загрузке, иначе плата выставит
// питание флеша 1.8 В и может не подняться.
//
// Прежний CS сидел на 5 — strapping-пине, и годился лишь потому, что CS в
// покое высокий. На обычном пине этой тонкости больше нет.
//
// На плате датчика шелкограф обслуживает оба варианта чипа: SCL это CLK,
// а SDA это CSn. Питание с вывода «5V» заведено на 3V3 — чип это принимает
// через свой стабилизатор, а главное, MISO тогда качается 0..3.3 В:
// GPIO ESP32 к пяти вольтам НЕ толерантны.
static const int PIN_SCK = 14, PIN_MISO = 27, PIN_MOSI = 26, PIN_CS = 25;
static const int PIN_LED = 2;

static const float POLE_PAIRS = 11;
static const float V_SUPPLY   = 12.0f;
static const float V_LIMIT    = 2.0f;     // правило владельца для долгой работы
static const float V_ALIGN    = 1.0f;
static const uint32_t FS      = 200;      // частота записи, Гц
static const uint32_t WD_MS   = 60000;    // сторож

BLDCMotor         motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver3PWM    driver = BLDCDriver3PWM(PIN_IN1, PIN_IN2, PIN_IN3, PIN_EN);
MagneticSensorSPI sensor = MagneticSensorSPI(AS5048_SPI, PIN_CS);
SPIClass spi(VSPI);

static float    target = 0.0f;
static bool     field_on = false;
static uint32_t last_cmd_ms = 0;

// состояние записи
static bool     rec = false, settling = false;
static uint32_t rec_next_us = 0, rec_period_us = 1000000UL / FS;
static uint32_t rec_n = 0, rec_need = 0, settle_until = 0;
static float    rec_a0 = 0.0f, rec_v = 0.0f;
static float    uq_sum = 0.0f, uq_max = 0.0f, v_sum = 0.0f;

// ---- ПРЕДПОДАЧА ПО НАПРЯЖЕНИЮ -------------------------------------------
// Uq_пред = знак(уставки) * (FFA + FFB * |уставка|)
//
// Смысл: часть напряжения нужна валу ВСЕГДА и известна заранее — трение
// (постоянная FFA) и противо-ЭДС (пропорциональная скорости, FFB). Отдав их
// напрямую, мы снимаем эту работу с ПИ, и шумная оценка скорости перестаёт
// задавать выход. Это и есть «опираться на экстраполяцию, а не на энкодер»,
// но БЕЗ платы запаздыванием — в отличие от увеличения окна оценки, которое
// проверено и разрушает ход (MET=0.02 дал 2 градуса вместо 0.04).
//
// ВНИМАНИЕ: feed_forward_voltage.q прибавляется в библиотеке ПОСЛЕ
// _constrain (FOCMotor.cpp:606), то есть ОБХОДИТ voltage_limit. Поэтому
// бюджет делим сами: ПИ получает остаток, и сумма не вылезает за потолок.
static float ff_a = 0.0f, ff_b = 0.0f;

// ---- КОМПЕНСАЦИЯ ЗУБЦОВ ДВУМЯ ГАРМОНИКАМИ --------------------------------
// Снято 18 сентября на 0.628 рад/с, четыре прогона по две минуты
// (runs/cog_2026-09-18_2032 и _2045). Таблица ЦЕЛИКОМ не воспроизводится
// (корреляция 0.83-0.87), а эти две гармоники — да:
//     44-й: 0.0498 / 0.0495 / 0.0494 / 0.0510 В при 18.3 / 18.4 / 19.4 / 20.6°
//     22-й: 0.0179 / 0.0204 / 0.0175 / 0.0214 В при 71.2 / 69.7 / 66.4 / 66.1°
// Нулевой порядок 70.7 (физически невозможный) даёт 0.0073 В — это фон
// проекции. 44-й выше него в 6.8 раза, 22-й в 2.6. Остальное в корзинах шум,
// и подавать его обратно в мотор нельзя.
//
// СНЯТО НА 0.628, ПРИМЕНЯЕТСЯ НА 0.02-0.11. Зубцовый момент — функция
// ПОЛОЖЕНИЯ и от скорости не зависит, поэтому перенос законен. Но проверить
// это надо замером: на медленном ходу связь «Uq -> момент» может отличаться.
//
// ЗНАК ОПРЕДЕЛЯЕТСЯ ЗАМЕРОМ, А НЕ РАССУЖДЕНИЕМ. Ошибка знака не ослабляет
// помеху, а УДВАИВАЕТ её. Поэтому множитель cog_k: 0 выкл, +1 прямая
// подача, -1 обратная. Правильный знак тот, при котором дрожание падает.
static const float COG_A44 = 0.0499f, COG_F44 = 19.7f * PI / 180.0f;
static const float COG_A22 = 0.0193f, COG_F22 = 68.4f * PI / 180.0f;
static const float COG_MAX = COG_A44 + COG_A22;   // потолок вклада, для бюджета
static float cog_k = 0.0f;
static float ff_база = 0.0f;



// ---- ТАБЛИЦА ЗУБЦОВ ------------------------------------------------------
// Копится по АБСОЛЮТНОМУ механическому углу, а не по фазе от начала записи:
// таблица индексируется валом, и две записи обязаны складываться.
// 360 корзин по градусу: у 22-го порядка период 16.36 град — шестнадцать
// корзин на период, с запасом.
static const int COG_N = 360;
static float    cog_sum[COG_N];
static uint16_t cog_cnt[COG_N];
static bool     cog_on = false;
static uint32_t cog_until_ms = 0;

// ---------------------- профиль показа ----------------------
//
// ЗАЧЕМ ОТДЕЛЬНАЯ КОМАНДА, а не серия RUN с разными скоростями. Ступенька
// уставки — это разрыв: контур отрабатывает её рывком, и увиденное будет
// говорить о переходном процессе, а не о ровности хода. Здесь уставка
// НЕПРЕРЫВНА и непрерывна её производная: синус и половинки косинуса вместо
// прямых. Тогда всё, что видно на валу, — свойство контура и механики, а не
// след от скачка команды.
//
// Амплитуда 1.0 рад/с — потолок проверенного: на нём калибровка мерила Uq
// 1.19 В из 2.0. Выше начинается неизведанное, и показывать на нём нечего.
//
// РАЗВОРОТ ЧЕРЕЗ НОЛЬ включён намеренно: смена знака момента — то место, где
// люфт и трение видны глазом лучше всего.
struct Seg { const char* name; uint8_t kind; float a, b; uint32_t ms; };
//   kind 0 = синус от нуля до нуля, амплитуда a, полных периодов b
//   kind 1 = плавный переход a -> b половинкой косинуса
//   kind 2 = постоянная a
static const Seg DEMO_PLAN[] = {
  {"синус +-1.0 рад/с",      0,  1.0f, 1.0f, 20000},
  {"плавный разгон до 1.0",  1,  0.0f, 1.0f,  6000},
  {"плавное торможение",     1,  1.0f, 0.0f,  6000},
  {"ползком 0.15 рад/с",     2,  0.15f, 0.0f, 8000},
  {"стоп",                   2,  0.0f, 0.0f,  1500},
};
static const int DEMO_N = sizeof(DEMO_PLAN) / sizeof(DEMO_PLAN[0]);
static bool     demo = false;
static int      demo_i = 0;
static uint32_t demo_t0 = 0, demo_next_us = 0;
static float    demo_a0 = 0.0f;

static float demo_target(const Seg& g, float u) {   // u — доля этапа, 0..1
  switch (g.kind) {
    case 0: return g.a * sinf(2.0f * PI * g.b * u);
    case 1: return g.a + (g.b - g.a) * 0.5f * (1.0f - cosf(PI * u));
    default: return g.a;
  }
}

// ---------------------- пакетная запись ----------------------
//
// ЗАЧЕМ ОТДЕЛЬНЫЙ РЕЖИМ. Обычная запись идёт 200 Гц и видит только до 100 Гц.
// Наблюдаемое глазом высокочастотное подрагивание может лежать и выше, а
// печатать быстрее 200 Гц через UART нельзя: строка угла это девять байт, и
// на 1 кГц это 78% всей пропускной способности 115200 — печать начнёт
// блокировать цикл управления и сама станет источником дрожания.
//
// Поэтому отсчёты копятся В ПАМЯТИ (4000 x 4 байта = 16 КБ, при свободных
// 300 КБ) и выгружаются ПОСЛЕ. Тогда запись не влияет на то, что меряет.
static const uint16_t FAST_MAX = 4000;
static float    fast_buf[FAST_MAX];
static bool     fast = false, fast_dump = false;
static uint16_t fast_n = 0, fast_need = 0;
static uint32_t fast_next_us = 0;
static float    fast_a0 = 0.0f;

// Частота цикла управления. Меряется, а не берётся из головы: от неё зависит,
// какие колебания контур вообще способен подавить, и упираемся ли мы в неё.
static uint32_t loop_cnt = 0, loop_hz = 0, loop_t0 = 0;

static char line[128];
static uint8_t line_n = 0;

static float arg(const char* s, const char* key, float def) {
  const char* p = strstr(s, key);
  if (!p) return def;
  p += strlen(key);
  if (*p != '=') return def;
  return atof(p + 1);
}

static void say_state() {
  Serial.printf("# STATE P=%.3f I=%.3f D=%.3f Tf=%.4f PA=%.2f RAMP=%.0f "
                "MET=%.4f vlim=%.2f FFA=%.3f FFB=%.3f COGK=%.1f field=%d loop_hz=%lu\n",
  motor.PID_velocity.P, motor.PID_velocity.I, motor.PID_velocity.D,
                motor.LPF_velocity.Tf, motor.P_angle.P,
                motor.PID_velocity.output_ramp, sensor.min_elapsed_time,
                motor.voltage_limit, ff_a, ff_b, cog_k, (int)field_on, (unsigned long)loop_hz);
}

static void field(bool on) {
  if (on == field_on) return;
  if (on) motor.enable(); else motor.disable();
  field_on = on;
  digitalWrite(PIN_LED, on);
}

static void применить_предподачу(float цель) {
  float ff = ff_a + ff_b * fabsf(цель);
  if (цель < 0) ff = -ff; else if (цель == 0) ff = 0;
  // Бюджет учитывает и зубцовую добавку: она тоже идёт мимо ограничителя.
  float остаток = V_LIMIT - fabsf(ff) - (cog_k != 0.0f ? COG_MAX : 0.0f);
  if (остаток < 0.3f) {            // ПИ нельзя оставлять совсем без бюджета
    остаток = 0.3f;
    ff = (V_LIMIT - 0.3f) * (ff < 0 ? -1.0f : 1.0f);
  }
  ff_база = ff;
  motor.feed_forward_voltage.q = ff;
  motor.updateVoltageLimit(остаток);   // именно так: прямая запись не трогает PID.limit
}
static void handle(const char* s) {
  last_cmd_ms = millis();
  if (!strncmp(s, "SET", 3)) {
    motor.PID_velocity.P = arg(s, "P", motor.PID_velocity.P);
    motor.PID_velocity.I = arg(s, "I", motor.PID_velocity.I);
    motor.PID_velocity.D = arg(s, "D", motor.PID_velocity.D);
    motor.LPF_velocity.Tf = arg(s, "Tf", motor.LPF_velocity.Tf);
    motor.P_angle.P = arg(s, "PA", motor.P_angle.P);
    motor.PID_velocity.output_ramp = arg(s, "RAMP", motor.PID_velocity.output_ramp);
    // MET — база оценки скорости. Стала перебираемой не из любопытства: цена
    // одного тика датчика в рад/с равна 0.000384/MET, и на медленном ходу она
    // сравнима с самой скоростью. При MET=5 мс и уставке 0.1 рад/с шум оценки
    // от квантования составляет ±0.077 рад/с — почти всю величину. Регулятор
    // в таких условиях не отличает зубцы от собственного шума.
    sensor.min_elapsed_time = arg(s, "MET", sensor.min_elapsed_time);
    ff_a = arg(s, "FFA", ff_a);
    ff_b = arg(s, "FFB", ff_b);
    cog_k = arg(s, "COGK", cog_k);
    // Интегратор сбрасывается при смене коэффициентов. Иначе накопленное
    // прошлым набором доехало бы в следующую ячейку и меряли бы мы историю.
    motor.PID_velocity.reset();
    say_state();
  } else if (!strncmp(s, "RUN", 3)) {
    rec_v    = arg(s, "v", 0.0f);
    rec_need = (uint32_t)(arg(s, "t", 8000.0f) / 1000.0f * FS);
    uint32_t settle = (uint32_t)arg(s, "s", 2000.0f);
    target = rec_v;
    применить_предподачу(target);
    field(true);
    settling = true;
    settle_until = millis() + settle;
    uq_sum = 0; uq_max = 0; v_sum = 0; rec_n = 0;
  } else if (!strncmp(s, "COG", 3)) {
    // COG v=0.628 sec=60 — снять таблицу зубцов.
    //
    // Компенсировать надо МОМЕНТ, и его прямо показывает выход регулятора Uq:
    // в установившемся вращении всё, что регулятор подкручивает в функции
    // ПОЛОЖЕНИЯ, и есть зубцовая помеха. Остаток угла для этого не годится —
    // он показывает то, что контур НЕ ДОДАВИЛ, то есть помеху, уже прошедшую
    // через чувствительность контура, и потому зависит от коэффициентов.
    float v = arg(s, "v", 0.628f);
    float sec = arg(s, "sec", 60.0f);
    for (int i = 0; i < COG_N; i++) { cog_sum[i] = 0.0f; cog_cnt[i] = 0; }
    target = v; field(true);
    cog_on = true; cog_until_ms = millis() + (uint32_t)(sec * 1000.0f);
    Serial.printf("# ЗУБЦЫ старт v=%.3f sec=%.0f корзин=%d\n", v, sec, COG_N);
  } else if (!strncmp(s, "OFF", 3)) {
    target = 0.0f; применить_предподачу(0.0f); field(false); rec = false; settling = false;
    Serial.println("# OFF");
  } else if (!strncmp(s, "DEMO", 4)) {
    demo = true; demo_i = 0; demo_t0 = millis();
    demo_next_us = micros(); demo_a0 = motor.shaft_angle;
    field(true);
    Serial.println("#ДЕМО fs=100");
    Serial.printf("#ЭТАП %s\n", DEMO_PLAN[0].name);
  } else if (!strncmp(s, "FAST", 4)) {
    float v = arg(s, "v", 0.1f);
    fast_need = (uint16_t)arg(s, "n", 4000);
    if (fast_need > FAST_MAX) fast_need = FAST_MAX;
    target = v; field(true);
    // Разгон перед записью тот же, что и в обычном прогоне: мерить переходный
    // процесс и звать его колебаниями — обычная ошибка.
    delay(2500);
    fast_a0 = motor.shaft_angle;
    fast_n = 0; fast_next_us = micros(); fast = true;
  } else if (!strncmp(s, "STATE", 5)) {
    say_state();
  } else {
    Serial.printf("# ? %s\n", s);
  }
}

void setup() {
  pinMode(PIN_LED, OUTPUT);
  Serial.begin(115200);
  delay(300);
  for (int i = 0; i < 5; i++) { digitalWrite(PIN_LED, HIGH); delay(60);
                                digitalWrite(PIN_LED, LOW); delay(60); }
  spi.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CS);
  sensor.init(&spi);
  // 5 мс, а не 0.1: на короткой базе оценка скорости завышалась на 16.8%
  // из-за задержки обмена по SPI внутри интервала Ts. Подробности в шапке
  // esp/foc_closed_probe.
  // ОКНО ОЦЕНКИ СКОРОСТИ. Замерено 5 сентября развёрткой по обеим осям.
  //
  // При 0.005 контур НЕ ДЕРЖИТ рабочие скорости владельца: отставание 0.69 на
  // 0.628 рад/с и 0.82 на 1.571 (воспроизведено в двух независимых прогонах).
  // При 0.002 держит везде: 0.985..1.000 на двенадцати скоростях от 0.1 до
  // 1.571, дрожание 0.165..0.333 град.
  //
  // Меньше — хуже: 0.001 даёт 0.262 против 0.254, а 0.0005 теряет ячейку по
  // отставанию (0.943). Больше — обрыв: 0.003 разваливается на 1.571 (3.74).
  //
  // Тонкость: правило из документации SimpleFOC про LPF_velocity.Tf лечит
  // СЛЕДСТВИЕ (шум оценки уже возник), а MET — ПРИЧИНУ. Убрав причину, можно
  // оставить лёгкий фильтр Tf=0.05 и получить втрое лучше, чем тяжёлым
  // фильтром по правилу: 0.254 против 0.402 при Tf=0.13.
  sensor.min_elapsed_time = 0.002f;
  motor.linkSensor(&sensor);

  driver.voltage_power_supply = V_SUPPLY;
  driver.voltage_limit = V_LIMIT * 2;
  if (!driver.init()) { Serial.println("# ОТКАЗ: драйвер"); while (1) delay(500); }
  motor.linkDriver(&driver);

  motor.voltage_limit        = V_LIMIT;
  motor.voltage_sensor_align = V_ALIGN;
  motor.controller           = MotionControlType::velocity;
  motor.torque_controller    = TorqueControlType::voltage;
  motor.velocity_limit       = 3.0f;
  // УМОЛЧАНИЯ — ПОБЕДИТЕЛЬ КАЛИБРОВКИ 4 сентября 2026 (docs/foc_tuning).
  // P=2.0 I=80 Tf=0.05: отставание 0.999, дрожание 0.18°, Uq макс 1.19 из 2.0.
  // Границы рядом с обеих сторон: I=120 срывается на 1 рад/с, P=2.5 с I=80
  // срывается везде, Tf ниже 0.05 уводит контур в насыщение при стоящем вале.
  // УМОЛЧАНИЯ ПОД МОТОР iFlight 3506, замерено 18 сентября.
  //
  // Прежние P=2.0 I=80 — от мотора 4108, и на 3506 они РАЗРУШАЮТ ход выше
  // 0.3 рад/с: СКО остатка 14.5 град, вал срывается и наверстывает. Оставлять
  // их умолчанием опасно: любой сброс платы возвращает чужие коэффициенты, и
  // инструмент, забывший выставить P и I, молча снимает мусор. Так и вышло со
  // съёмом таблицы зубцов 18 сентября.
  //
  // Выбрано началом плато, а не нижней точкой: от P=4 до P=6 выигрыш 5%, а
  // запас по фазе тает. Tf=0.01 — настоящий минимум, ниже и выше хуже.
  motor.PID_velocity.P = 4.0f; motor.PID_velocity.I = 5.0f;
  motor.PID_velocity.D = 0.0f; motor.PID_velocity.output_ramp = 200.0f;
  motor.LPF_velocity.Tf = 0.01f; motor.P_angle.P = 6.0f;

  Serial.println();
  Serial.println("# == стенд контура, команды по serial ==");
  motor.init();
  if (!motor.initFOC()) { Serial.println("# ОТКАЗ: initFOC"); while (1) delay(500); }
  Serial.printf("# initFOC: dir=%s zero=%.4f\n",
                motor.sensor_direction == Direction::CW ? "CW" : "CCW",
                motor.zero_electric_angle);
  motor.disable();
  field_on = false;
  Serial.printf("# ГОТОВ fs=%lu vlim=%.2f\n", (unsigned long)FS, V_LIMIT);
  last_cmd_ms = millis();
}

void loop() {
  // ЗУБЦОВАЯ ДОБАВКА СЧИТАЕТСЯ КАЖДЫЙ ТАКТ по АБСОЛЮТНОМУ углу вала:
  // помеха привязана к положению, а не ко времени и не к фазе от старта.
  if (cog_k != 0.0f && field_on) {
    float th = sensor.getMechanicalAngle();
    float доб = COG_A44 * cosf(44.0f * th - COG_F44)
              + COG_A22 * cosf(22.0f * th - COG_F22);
    motor.feed_forward_voltage.q = ff_база + cog_k * доб;
  }
  motor.loopFOC();
  motor.move(target);

  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (line_n) { line[line_n] = 0; handle(line); line_n = 0; }
    } else if (line_n < sizeof(line) - 1) {
      line[line_n++] = c;
    }
  }

  loop_cnt++;
  if (millis() - loop_t0 >= 1000) { loop_hz = loop_cnt; loop_cnt = 0; loop_t0 = millis(); }

  if (fast) {
    if ((int32_t)(micros() - fast_next_us) >= 0) {
      fast_next_us += 1000;                    // 1 кГц
      fast_buf[fast_n++] = motor.shaft_angle - fast_a0;
      if (fast_n >= fast_need) { fast = false; fast_dump = true; }
    }
    return;                                    // ничего не печатаем, пока меряем
  }
  if (fast_dump) {
    fast_dump = false;
    target = 0.0f; field(false);
    Serial.printf("#БЫСТРО n=%u fs=1000 loop_hz=%lu\n",
                  fast_n, (unsigned long)loop_hz);
    for (uint16_t i = 0; i < fast_n; i++) Serial.println(fast_buf[i], 6);
    Serial.println("#КОНЕЦ-БЫСТРО");
    return;
  }

  if (demo) {
    const Seg& g = DEMO_PLAN[demo_i];
    uint32_t прошло = millis() - demo_t0;
    float u = (float)прошло / g.ms;
    if (u > 1.0f) u = 1.0f;
    target = demo_target(g, u);
    // Светодиод показывает ЗНАК уставки, а не этап: со стороны стенда это
    // единственный способ отличить «вал остановился по команде» от «вал встал
    // сам», и он же подсвечивает момент разворота.
    digitalWrite(PIN_LED, target > 0.02f ? ((millis() / 150) & 1)
                        : (target < -0.02f ? ((millis() / 600) & 1) : 1));
    if ((int32_t)(micros() - demo_next_us) >= 0) {
      demo_next_us += 10000;                     // 100 Гц: показ, не спектр
      Serial.printf("d:%lu,%.4f,%.4f,%.3f\n", (unsigned long)прошло,
                    target, motor.shaft_velocity,
                    (motor.shaft_angle - demo_a0) * 180.0f / PI);
    }
    if (прошло >= g.ms) {
      demo_i++; demo_t0 = millis();
      if (demo_i >= DEMO_N) {
        demo = false; target = 0.0f; field(false);
        Serial.println("#ДЕМО-КОНЕЦ");
      } else {
        Serial.printf("#ЭТАП %s\n", DEMO_PLAN[demo_i].name);
      }
    }
    return;                                      // запись матрицы и демо не смешиваются
  }

  if (settling && millis() >= settle_until) {
    // Запись начинается ПОСЛЕ разгона: включив её сразу, мы бы мерили
    // переходный процесс и звали его дрожанием.
    settling = false;
    rec = true;
    rec_a0 = motor.shaft_angle;
    rec_next_us = micros();
    // АБСОЛЮТНЫЙ МЕХАНИЧЕСКИЙ УГОЛ В ЗАГОЛОВКЕ. Без него запись нельзя
    // привязать к валу: угол пишется ОТ НАЧАЛА ЗАПИСИ, и абсолютная фаза
    // между прогонами теряется. Для таблицы компенсации зубцов это
    // смертельно — таблица индексируется механическим углом, и без опорной
    // точки два прогона сложить нельзя.
    // Поле добавлено в конец строки: разборщики читают #НАЧАЛО по токенам
    // (ищут w= и fs=), лишнее игнорируют.
    Serial.printf("#НАЧАЛО w=%.3f volts=%.2f fs=%lu a0_mech=%.6f\n",
                  rec_v, V_LIMIT, (unsigned long)FS, (double)sensor.getMechanicalAngle());
  }

  if (cog_on) {
    // Копим КАЖДЫЙ такт: чем больше отсчётов на корзину, тем ниже шум оценки.
    float th = sensor.getMechanicalAngle();          // 0..2pi, абсолютный
    int bin = (int)(th * (COG_N / (2.0f * PI)));
    if (bin >= 0 && bin < COG_N && cog_cnt[bin] < 65000) {
      cog_sum[bin] += motor.voltage.q;               // СО ЗНАКОМ: направление важно
      cog_cnt[bin]++;
    }
    if ((int32_t)(millis() - cog_until_ms) >= 0) {
      cog_on = false; target = 0.0f; field(false);
      Serial.println("#ЗУБЦЫ_НАЧАЛО угол_град uq_среднее отсчётов");
      for (int i = 0; i < COG_N; i++) {
        float m = cog_cnt[i] ? cog_sum[i] / cog_cnt[i] : 0.0f;
        Serial.printf("%.2f %.5f %u\n", (i + 0.5f) * 360.0f / COG_N, m, cog_cnt[i]);
      }
      Serial.println("#ЗУБЦЫ_КОНЕЦ");
    }
  }

  if (rec && (int32_t)(micros() - rec_next_us) >= 0) {
    rec_next_us += rec_period_us;          // от уставки, а не от «сейчас»: иначе частота уползёт
    Serial.println(motor.shaft_angle - rec_a0, 6);
    float u = fabsf(motor.voltage.q);
    uq_sum += u; if (u > uq_max) uq_max = u;
    v_sum += motor.shaft_velocity;
    if (++rec_n >= rec_need) {
      rec = false;
      target = 0.0f;
      Serial.printf("#КОНЕЦ n=%lu uq_avg=%.3f uq_max=%.3f v_est=%.4f\n",
                    (unsigned long)rec_n, uq_sum / rec_n, uq_max, v_sum / rec_n);
    }
  }

  if (field_on && !rec && !settling && millis() - last_cmd_ms > WD_MS) {
    target = 0.0f; field(false);
    Serial.println("# СТОРОЖ: команд нет минуту, поле снято");
  }
}
