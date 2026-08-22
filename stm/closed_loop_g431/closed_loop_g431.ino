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
 *          tools/stand/grab_vcp.sh runs/closed_first.txt
 *          .venv/bin/python tools/stand/loop_compare.py runs/closed_first.txt
 *
 * После прошивки вал СТОИТ и ничего не печатает, пока не подключится захват:
 * см. ожидание хоста в setup(). Это не зависание.
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
// измерения, а не разгон.
static const float    W_ABORT     = 1.0f;      // рад/с, на записи
// ПОРОГ НА УСТАНОВЛЕНИИ ОТДЕЛЬНЫЙ, И ЭТО НЕ ПОБЛАЖКА. При включении драйвера
// поле хватает ротор из произвольного положения и рывком тянет к ближайшему
// электрическому нулю: до половины электрического оборота = 0.03 рад вала за
// единицы миллисекунд. Это настоящее движение, а не артефакт, и на нём первая
// редакция сторожа оборвала прогон. Порог 3 рад/с всё ещё вдвое ниже
// названной границы безопасности вала (6 рад/с), то есть настоящий разгон
// ловится и здесь.
static const float    W_ABORT_SETTLE = 3.0f;
static const uint32_t W_ABORT_MS  = 50;
// ОКНО ИЗМЕРЕНИЯ СКОРОСТИ ДЛЯ СТОРОЖА — СВОЁ, 100 мс.
//
// Первая редакция сторожила motor.shaft_velocity, и в разомкнутом контуре это
// была ошибка: velocityOpenloop() присваивает shaft_velocity УСТАВКУ, то есть
// сторож смотрел на команду и физического разгона не увидел бы вовсе.
//
// Брать sensor.getVelocity() тоже нельзя: она считается за кадр 922 мкс, и
// шум угла 0.037 градуса даёт на нём около 0.7 рад/с — вплотную к порогу
// 1.0. На окне 100 мс тот же шум даёт 0.007 рад/с, то есть запас в сто
// с лишним раз, а разгон до 1 рад/с ловится за 150 мс = 8.6 градуса вала.
static const uint32_t W_WIN_MS = 100;
// ДОПУСТИМЫЙ РАЗБРОС ВЫРАВНИВАНИЯ, радианы электрические. 0.20 рад = 11.5
// градусов электрических, потеря момента 1-cos(5.7 град) = 0.5%. Неустойчивость
// начинается около 90 градусов, так что порог с большим запасом; он ловит не
// потерю момента, а НЕВОСПРОИЗВОДИМОСТЬ замера — признак того, что мерилось
// не выравнивание, а трение и качание груза.
static const float ZEA_SPREAD_MAX = 0.20f;
// УПОР НАПРЯЖЕНИЯ — НЕ ЗАЩИТА, А ДИАГНОСТИКА, и убивать им прогон неверно.
// При пределе 2.0 В упор Uq — это ровно то напряжение, которое штатно подаёт
// разомкнутый контур (velocityOpenloop() возвращает voltage_limit ВСЕГДА),
// опасности в нём нет никакой. Первая редакция обрывала на нём прогон и
// оборвала: разомкнутый отрезок не состоялся из-за нормального состояния.
// Теперь считается доля времени в упоре и печатается в заголовке отрезка.
static const float    UQ_SAT_FRAC = 0.98f;

CaptureSensor  sensor;
BLDCMotor      motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver6PWM driver = BLDCDriver6PWM(A_PHASE_UH, A_PHASE_UL,
                                       A_PHASE_VH, A_PHASE_VL,
                                       A_PHASE_WH, A_PHASE_WL);

static uint16_t n_samp;
static uint16_t fs_hz;
static float buf[N_SAMP];
static float w_meas = 0.0f;      //!< измеренная скорость вала, рад/с (см. guard)

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
  Serial.print(F("  скорость_изм=")); Serial.print(w_meas, 4);
  Serial.print(F(" скорость_SFOC=")); Serial.print(motor.shaft_velocity, 4);
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
static uint32_t w_since = 0, win_t = 0;
static float    win_a = 0.0f;
static bool     win_have = false;
static bool     armed_tight = false;   //!< идёт запись, порог строгий
static float    w_peak = 0.0f;         //!< наибольшая |скорость| за запись
static float    w_peak_settle = 0.0f;  //!< то же за установление, отдельно
static float    zea_mean = 0.0f;       //!< средний электрический ноль по трём замерам
static uint32_t g_calls = 0, g_sat = 0;
// ЗНАК Uq, А НЕ ТОЛЬКО МОДУЛЬ. Счётчик упора сигнатуру «регулятор дёргается
// между упорами» и «регулятор упёрся в одну сторону» не различает вовсе, а
// это два разных отказа с разными причинами.
static float uq_min = 0.0f, uq_max = 0.0f;

/** Сбрасывается ПЕРЕД каждым отрезком. Между отрезками идёт выгрузка длиной в
 *  секунды: не сбросив окно, первую измеренную скорость мы посчитали бы через
 *  всю паузу, а выдержки — от событий прошлого отрезка. */
static void guard_reset() {
  w_since = 0; win_have = false; g_calls = 0; g_sat = 0;
  w_peak = 0.0f; w_peak_settle = 0.0f; armed_tight = false; w_meas = 0.0f;
  uq_min = 1e9f; uq_max = -1e9f;
}

/** Две защиты (разгон, негодный датчик) и один счётчик (упор Uq).
 *  Вызывается из КАЖДОЙ итерации управления — включая холостые, иначе
 *  выдержки мерились бы в чтениях, а не во времени. */
static void guard(bool closed) {
  const uint32_t now = millis();
  g_calls++;
  const float uq_now = motor.voltage.q;
  if (uq_now < uq_min) uq_min = uq_now;
  if (uq_now > uq_max) uq_max = uq_now;
  if (fabsf(uq_now) >= UQ_SAT_FRAC * motor.voltage_limit) g_sat++;

  // ЗДОРОВЬЕ ДАТЧИКА — только в замкнутом. В разомкнутом датчик не участвует
  // в управлении, и его негодность портит замер, но не разгоняет вал.
  if (closed && !sensor.isHealthy()) die("датчик негоден: см. разбор ниже");

  // Скорость меряется по УГЛУ на окне, а не берётся у библиотеки: см. шапку
  // W_WIN_MS. В разомкнутом контуре библиотечная величина — это команда.
  const float a = sensor.getAngle();
  if (!win_have) { win_t = now; win_a = a; win_have = true; }
  else if (now - win_t >= W_WIN_MS) {
    w_meas = (a - win_a) / ((float)(now - win_t) * 1e-3f);
    win_t = now; win_a = a;
  }

  const float aw = fabsf(w_meas);
  if (armed_tight) { if (aw > w_peak) w_peak = aw; }
  else             { if (aw > w_peak_settle) w_peak_settle = aw; }

  // СТОРОЖ РАЗГОНА — ТОЛЬКО В ЗАМКНУТОМ, и это не послабление, а исправление.
  //
  // В РАЗОМКНУТОМ КОНТУРЕ КОМАНДА И ЕСТЬ ПОТОЛОК СКОРОСТИ ПО ПОСТРОЕНИЮ: поле
  // вращается ровно с уставкой, и вал не может уйти быстрее синхронного иначе
  // как рывком при захвате ротора или проскальзыванием — то есть событиями,
  // которые кончаются сами и напряжением не поддерживаются. Разгон, от
  // которого сторож защищает, возможен только там, где регулятор способен
  // держать упор в неверную сторону сколь угодно долго, — в замкнутом.
  //
  // Первая редакция обрывала на этом разомкнутый отрезок дважды подряд,
  // приняв за аварию нормальную физику. Пики теперь МЕРЯЮТСЯ и печатаются
  // отдельно за установление и за запись — это данные, а не повод для отказа.
  if (!closed) return;
  const float lim = armed_tight ? W_ABORT : W_ABORT_SETTLE;
  if (aw > lim) { if (!w_since) w_since = now; }
  else w_since = 0;
  if (w_since && now - w_since > W_ABORT_MS) die("разгон: измеренная скорость выше порога");
}


/**
 * Проба МОМЕНТА: постоянное Uq, регулятор скорости не участвует вовсе.
 *
 * ЗАЧЕМ ОТДЕЛЬНО ОТ КОНТУРА СКОРОСТИ. Восемь сочетаний знака и сдвига нуля
 * дали ноль хода при Uq в упоре. Пока не отделён FOC от регулятора, это
 * можно объяснять и настройкой, и отображением угла, и нагрузкой. В режиме
 * момента объяснений остаётся одно: при верном отображении «датчик ->
 * электрический угол» постоянное Uq держит поле на четверть оборота впереди
 * ротора и обязано раскрутить вал. Если вал запирается — сломано
 * отображение, и настраивать нечего.
 *
 * Возвращается ПРОЙДЕННЫЙ УГОЛ, а не скорость: одиночный скачок к точке
 * запирания средняя скорость за хвост замера не покажет, а он и есть подпись
 * запирания.
 */
static float probe_torque(Direction dir, float uq) {
  motor.sensor_direction    = dir;
  motor.zero_electric_angle = zea_mean;
  motor.controller          = MotionControlType::torque;
  guard_reset();
  const float a0 = sensor.getAngle();
  const uint32_t t0 = millis();
  while (millis() - t0 < 3000) {
    motor.loopFOC(); motor.move(uq); guard(false);
    if (fabsf(w_meas) > 3.0f) break;
  }
  const float trav = sensor.getAngle() - a0;
  motor.move(0.0f); motor.disable(); delay(300); motor.enable();
  return trav;
}

/** Внутренности контура вживую. Печатается ДО перебора сдвигов, потому что
 *  перебор отвечает «какой ноль», а этот отрезок — «шевелится ли вообще то,
 *  из чего ноль вычисляется». Первый замкнутый прогон дал полное напряжение
 *  при нулевом ходе, и по одному этому различить нечего. */
static void diag_closed(float w) {
  motor.zero_electric_angle = zea_mean;
  motor.PID_velocity.reset();
  motor.controller = MotionControlType::velocity;
  guard_reset();
  Serial.println(F("#ДИАГ мс цель w_SFOC w_изм Uq эл_угол мех_угол угол годен"));
  const uint32_t t0 = millis(); uint32_t next = t0;
  while (millis() - t0 < 2000) {
    motor.loopFOC(); motor.move(w); guard(false);
    if ((int32_t)(millis() - next) >= 0) {
      next += 200;
      Serial.print(F("#ДИАГ ")); Serial.print(millis() - t0);
      Serial.print(' '); Serial.print(motor.target, 3);
      Serial.print(' '); Serial.print(motor.shaft_velocity, 4);
      Serial.print(' '); Serial.print(w_meas, 4);
      Serial.print(' '); Serial.print(motor.voltage.q, 3);
      Serial.print(' '); Serial.print(motor.electrical_angle, 4);
      Serial.print(' '); Serial.print(sensor.getMechanicalAngle(), 4);
      Serial.print(' '); Serial.print(sensor.getAngle(), 4);
      Serial.print(' '); Serial.println(sensor.isHealthy());
    }
  }
  motor.move(0.0f); motor.disable(); delay(300); motor.enable();
}

/**
 * Проба электрического нуля: замкнуть контур со сдвигом ZEA и померить,
 * поехал ли вал.
 *
 * ЗАЧЕМ ПЕРЕБОР, А НЕ ДОВЕРИЕ ЗАМЕРУ. Первый замкнутый отрезок 23.08 держал
 * вал НЕПОДВИЖНО (размах 0.25 градуса = шум датчика) при Uq в упоре 83%
 * времени. Это подпись сдвинутого нуля: команда, задуманная как момент
 * (ось q), при сдвиге на 90 градусов ложится на ось d и становится
 * удерживающей. Разомкнутый прогон это не ловит в принципе:
 * velocityOpenloop() строит электрический угол из своего интегратора и ZEA
 * не использует вовсе, поэтому его успех о верности нуля не говорит ничего.
 *
 * Проба НЕ УБИВАЕТ прогон при разгоне: неверный сдвиг обязан быть измерен и
 * напечатан, а не оборвать замер. guard(false) поэтому только считает
 * скорость; выход за порог прекращает ЭТУ пробу, а не всё.
 */
static float probe_zea(Direction dir, float off, float w) {
  motor.sensor_direction    = dir;
  motor.zero_electric_angle = _normalizeAngle(zea_mean + off);
  motor.PID_velocity.reset();
  motor.controller = MotionControlType::velocity;
  guard_reset();
  const uint32_t t_start = millis();
  uint32_t t0 = 0; float a0 = 0.0f; bool have0 = false;
  // ЧЕТЫРЕ СЕКУНДЫ, А НЕ ДВЕ С ПОЛОВИНОЙ. Диагностика показала, что при I=5
  // интегратор набирает упор около двух секунд: замер, начатый раньше, мерил
  // бы не ход, а разгон напряжения.
  while (millis() - t_start < 4000) {
    motor.loopFOC(); motor.move(w);
    guard(false);
    if (!have0 && millis() - t_start > 2000) {
      a0 = sensor.getAngle(); t0 = millis(); have0 = true;
    }
    if (fabsf(w_meas) > 3.0f) break;
  }
  motor.move(0.0f);
  const uint32_t dt = millis() - t0;
  const float v = (have0 && dt > 200) ? (sensor.getAngle() - a0) / (dt * 1e-3f) : 0.0f;
  motor.disable(); delay(300); motor.enable();   // разрядить интегратор и поле
  return v;
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
  guard_reset();
  const uint32_t step_us = 1000000UL / fs_hz;
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

  // Установление кончилось — сторож переходит на строгий порог, а пик
  // скорости обнуляется, чтобы в заголовке отрезка стоял пик ЗАПИСИ, а не
  // рывка при включении поля.
  armed_tight = true; w_since = 0;

  uint32_t next = micros();
  for (uint16_t i = 0; i < n_samp; i++) {
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
  // ФАКТИЧЕСКИЙ предел, а не константа: командой V он меняется, и заголовок,
  // печатавший VOLTS, врал про условия замера.
  Serial.print(F(" volts="));    Serial.print(drive ? motor.voltage_limit : 0.0f, 2);
  Serial.print(F(" fs="));       Serial.print(fs_hz);
  Serial.print(F(" n="));        Serial.print(n_samp);
  Serial.print(F(" упор="));     Serial.print(g_calls ? (100.0f * g_sat / g_calls) : 0.0f, 1);
  Serial.print(F("% w_изм="));   Serial.print(w_meas, 4);
  Serial.print(F(" w_пик_зап=")); Serial.print(w_peak, 4);
  Serial.print(F(" w_пик_уст=")); Serial.print(w_peak_settle, 4);
  Serial.print(F(" Uq=[")); Serial.print(uq_min, 3);
  Serial.print(','); Serial.print(uq_max, 3); Serial.print(']');
  Serial.print(F(" режим="));    Serial.println(tag);
  for (uint16_t i = 0; i < n_samp; i++) Serial.println(buf[i], 6);
  Serial.println(F("#КОНЕЦ"));
  Serial.flush();
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  blink(5, 80);

  Serial.begin(115200);

  // ЖДЁМ ХОСТА, И БЕЗ НЕГО НЕ ДВИГАЕМСЯ ВОВСЕ.
  //
  // Две причины, и обе из практики:
  //
  //  1. Прошивка СБРАСЫВАЕТ плату. Если бы скетч стартовал сам, шапка,
  //     состояние тракта и результат выравнивания напечатались бы в первые
  //     миллисекунды — то есть до того, как захват успел открыть порт, — и
  //     пропали бы ровно те строки, ради которых всё печатается.
  //  2. Вал крутится. Прошивка, которая начинает движение от одного лишь
  //     включения питания, — это то, чем сейчас занята плата (elzero_g431
  //     меряет выравнивание при КАЖДОМ включении и дёргает вал на 5-15
  //     градусов). Здесь движение начинается только по явной команде с
  //     ноутбука.
  //
  // Ожидание БЕЗ ТАЙМАУТА намеренно: истёкший таймаут означал бы движение
  // вала без единого наблюдателя.
  while (!Serial.available()) {
    Serial.println(F("#ЖДУ пришлите любой байт, чтобы начать"));
    for (uint8_t i = 0; i < 10 && !Serial.available(); i++) {
      digitalWrite(LED_BUILTIN, i < 1); delay(100);
    }
  }
  while (Serial.available()) Serial.read();

  // Отладочный вывод библиотеки — В ФАЙЛ ЗАМЕРА. Он печатает «sensor dir»,
  // «PP check: est. pp» и «Zero elec. angle», то есть ровно те промежуточные
  // величины, по которым видно, был ли замер выравнивания уверенным или
  // пограничным. Без них расхождение направлений пришлось бы гадать.
  SimpleFOCDebug::enable(&Serial);

  Serial.println();
  Serial.println(F("=== ESC1: первое замыкание контура на тракт захвата ==="));
  Serial.print(F("#ПРОШИВКА closed_loop_g431 w=")); Serial.print(W_TEST, 3);
  Serial.print(F(" В=")); Serial.print(VOLTS, 2);
  Serial.print(F(" P=")); Serial.print(PID_P, 3);
  Serial.print(F(" I=")); Serial.print(PID_I, 3);
  Serial.print(F(" D=")); Serial.print(PID_D, 3);
  Serial.print(F(" Tf=")); Serial.print(LPF_TF, 4);
  Serial.print(F(" наклон=")); Serial.println(CAPSENS_DEG_PER_DUTY, 3);

  n_samp = N_SAMP; fs_hz = FS_HZ;
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

  Serial.println(F("#ГОТОВ"));
}

// ===========================================================================
// КОМАНДНЫЙ РЕЖИМ
//
// ПОЧЕМУ НЕ ЖЁСТКАЯ ПОСЛЕДОВАТЕЛЬНОСТЬ. Первая редакция гоняла записанный
// наперёд сценарий, и каждый новый вопрос к железу стоил перепрошивки: сборка,
// заливка, сброс, ожидание — около сорока секунд на один ответ. За два часа
// разбора это съело больше времени, чем сами замеры. Здесь прошивка одна, а
// опыт задаётся строкой в порт.
//
// КАЖДАЯ КОМАНДА КОНЧАЕТСЯ #ГОТОВ. Без явной отбивки читающая сторона не
// знает, дочитала ли она ответ, и вынуждена гадать по таймауту — а таймаут,
// подобранный под короткую команду, обрезал бы длинную.
//
//   F              пол прибора: запись без движения
//   O<w>           разомкнутый на скорости w, запись
//   C<w>           замкнутый на скорости w, запись
//   T<uq>          момент Uq на 3 с: печатает пройденный угол и скорость
//   L              лестница момента (0.15..0.9 В)
//   R              лестница разомкнутого по напряжению (0.4..2.0 В)
//   A              выравнивание трижды, направление берётся текущее
//   D<w>           замер направления по разомкнутому ходу на скорости w
//   Z<рад>         сдвинуть электрический ноль
//   V<В>           предел напряжения
//   P<x> I<x> J<x> коэффициенты регулятора (J — дифференциальный)
//   G<с>           постоянная фильтра скорости
//   N<штук>        сколько отсчётов писать (не больше N_SAMP)
//   W<Гц>          частота записи
//   ?              состояние
// ===========================================================================



/** Момент с записью ВНУТРЕННОСТЕЙ: механический угол, электрический угол и
 *  Uq на 200 Гц. Отвечает на единственный оставшийся вопрос — следит ли поле
 *  за ротором. Разомкнутый контур на 2.0 В вал крутит, а режим момента даже
 *  на 2.5 В не сдвигает, хотя поле в нём ставится на четверть оборота впереди
 *  ротора и момент обязан быть максимальным. Одно из двух: либо электрический
 *  угол не следует за механическим, либо Uq не доходит до фаз. Обе гипотезы
 *  различаются прямо в этих трёх колонках. */
static void diag_torque(float uq, uint16_t n) {
  motor.enable();
  motor.controller = MotionControlType::torque;
  guard_reset();
  Serial.print(F("#ТОРК_ДИАГ Uq=")); Serial.print(uq, 3);
  Serial.print(F(" n=")); Serial.print(n);
  Serial.println(F("  колонки: мех эл Uq_факт"));
  const uint32_t step_us = 1000000UL / 200;
  uint32_t next = micros();
  for (uint16_t i = 0; i < n; i++) {
    while ((int32_t)(micros() - next) < 0) { motor.loopFOC(); motor.move(uq); guard(false); }
    next += step_us;
    Serial.print(sensor.getMechanicalAngle(), 4); Serial.print(' ');
    Serial.print(motor.electrical_angle, 4); Serial.print(' ');
    Serial.println(motor.voltage.q, 3);
  }
  motor.move(0.0f); motor.disable(); delay(200); motor.enable();
  Serial.println(F("#ТОРК_КОНЕЦ"));
}

/** Трасса ЗАМКНУТОГО контура: уставка, скорость по библиотеке, Uq, мех. угол.
 *  Отвечает на вопрос, который счётчик упора закрыть не может: упирается ли
 *  регулятор в ОДНУ сторону или дёргается между упорами. */
static void diag_vel(float w, uint16_t n) {
  motor.enable();
  motor.controller = MotionControlType::velocity;
  motor.PID_velocity.reset();
  guard_reset();
  Serial.print(F("#ВЕЛ_ДИАГ w=")); Serial.print(w, 3);
  Serial.print(F(" n=")); Serial.print(n);
  Serial.println(F("  колонки: w_SFOC Uq мех"));
  const uint32_t step_us = 1000000UL / 200;
  uint32_t next = micros();
  for (uint16_t i = 0; i < n; i++) {
    while ((int32_t)(micros() - next) < 0) { motor.loopFOC(); motor.move(w); guard(false); }
    next += step_us;
    Serial.print(motor.shaft_velocity, 4); Serial.print(' ');
    Serial.print(motor.voltage.q, 3); Serial.print(' ');
    Serial.println(sensor.getMechanicalAngle(), 4);
  }
  motor.move(0.0f); Serial.println(F("#ВЕЛ_КОНЕЦ"));
}

static void status() {
  const CaptureStatus &st = sensor.lastStatus();
  Serial.print(F("#СОСТ V=")); Serial.print(motor.voltage_limit, 2);
  Serial.print(F(" P=")); Serial.print(motor.PID_velocity.P, 4);
  Serial.print(F(" I=")); Serial.print(motor.PID_velocity.I, 4);
  Serial.print(F(" D=")); Serial.print(motor.PID_velocity.D, 4);
  Serial.print(F(" Tf=")); Serial.print(motor.LPF_velocity.Tf, 5);
  Serial.print(F(" знак=")); Serial.print(motor.sensor_direction == Direction::CW ? F("CW") : F("CCW"));
  Serial.print(F(" ZEA=")); Serial.print(motor.zero_electric_angle, 5);
  Serial.print(F(" n=")); Serial.print(n_samp);
  Serial.print(F(" fs=")); Serial.print(fs_hz);
  Serial.print(F(" годен=")); Serial.print(sensor.isHealthy());
  Serial.print(F(" угол=")); Serial.print(sensor.getAngle(), 4);
  Serial.print(F(" период=")); Serial.println(st.period);
}

/** Разомкнутый ход и знак по нему. Отдельной командой, потому что это ЗАМЕР,
 *  а не настройка: 12 секунд хода надёжнее двух отсчётов библиотеки. */
static void measure_dir(float w) {
  motor.enable();
  motor.controller = MotionControlType::velocity_openloop;
  guard_reset();
  const float a0 = sensor.getAngle();
  const uint32_t t0 = millis();
  float wr = 0.0f; uint32_t prev = micros();
  while (millis() - t0 < 6000) {
    uint32_t now = micros(); float dt = (now - prev) * 1e-6f; prev = now;
    if (dt < 0 || dt > 0.05f) dt = 0;
    float st = 0.2f * dt;
    if (wr < w) wr = min(wr + st, w); else if (wr > w) wr = max(wr - st, w);
    motor.loopFOC(); motor.move(wr); guard(false);
  }
  const float v = (sensor.getAngle() - a0) / ((millis() - t0) * 1e-3f);
  motor.move(0.0f);
  const float sync = fabsf(v) / fabsf(w);
  motor.sensor_direction = (v * w > 0) ? Direction::CW : Direction::CCW;
  Serial.print(F("#НАПРАВЛЕНИЕ w_изм=")); Serial.print(v, 4);
  Serial.print(F(" команда=")); Serial.print(w, 4);
  Serial.print(F(" синхронизм=")); Serial.print(sync, 3);
  Serial.print(F(" -> "));
  Serial.println(motor.sensor_direction == Direction::CW ? F("CW") : F("CCW"));
}

static void do_align() {
  float zea[3];
  for (uint8_t k = 0; k < 3; k++) {
    motor.zero_electric_angle = NOT_SET;
    if (!motor.initFOC()) { Serial.println(F("#ОТКАЗ выравнивание не удалось")); return; }
    zea[k] = motor.zero_electric_angle;
    Serial.print(F("#ВЫРАВНИВАНИЕ ")); Serial.print(k + 1);
    Serial.print(F(" ZEA=")); Serial.println(zea[k], 5);
    delay(200);
  }
  float cs = 0.0f, sn = 0.0f;
  for (uint8_t k = 0; k < 3; k++) { cs += cosf(zea[k]); sn += sinf(zea[k]); }
  zea_mean = atan2f(sn, cs);
  float sp = 0.0f;
  for (uint8_t k = 0; k < 3; k++) {
    float d = fabsf(atan2f(sinf(zea[k] - zea_mean), cosf(zea[k] - zea_mean)));
    if (d > sp) sp = d;
  }
  sp *= 2.0f;
  motor.zero_electric_angle = zea_mean;
  Serial.print(F("#ВЫРАВНИВАНИЕ_ИТОГ ZEA=")); Serial.print(zea_mean, 5);
  Serial.print(F(" размах=")); Serial.print(sp * 57.2958f, 2);
  Serial.print(F(" град_эл -> ")); Serial.println(sp <= ZEA_SPREAD_MAX ? F("годен") : F("НЕГОДЕН"));
}

void loop() {
  static char cmd[24]; static uint8_t n = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (!n) continue;
      cmd[n] = 0; n = 0;
      const char k = cmd[0];
      const float a = atof(cmd + 1);
      switch (k) {
        case 'F': motor.disable(); record(false, false, 0.0f); dump("пол", false, 0.0f); break;
        case 'O': motor.enable(); motor.controller = MotionControlType::velocity_openloop;
                  record(true, false, a); motor.move(0.0f); dump("разомкнутый", true, a); break;
        case 'C': motor.enable(); motor.controller = MotionControlType::velocity;
                  motor.PID_velocity.reset();
                  record(true, true, a); motor.move(0.0f); dump("замкнутый", true, a); break;
        case 'T': { motor.enable();
                    const float tr = probe_torque(motor.sensor_direction, a);
                    Serial.print(F("#МОМЕНТ Uq=")); Serial.print(a, 3);
                    Serial.print(F(" пройдено=")); Serial.print(tr * 57.2958f, 2);
                    Serial.print(F(" град  w=")); Serial.println(tr / 3.0f, 4); } break;
        case 'A': motor.enable(); do_align(); break;
        case 'D': measure_dir(a); break;
        case 'Z': zea_mean = _normalizeAngle(zea_mean + a); motor.zero_electric_angle = zea_mean;
                  Serial.print(F("#ZEA=")); Serial.println(zea_mean, 5); break;
        case 'V': motor.voltage_limit = a; motor.PID_velocity.limit = a;
                  Serial.print(F("#V=")); Serial.println(a, 2); break;
        case 'P': motor.PID_velocity.P = a; Serial.println(F("#ok")); break;
        case 'I': motor.PID_velocity.I = a; Serial.println(F("#ok")); break;
        case 'J': motor.PID_velocity.D = a; Serial.println(F("#ok")); break;
        case 'G': motor.LPF_velocity.Tf = a; Serial.println(F("#ok")); break;
        case 'N': n_samp = min((uint16_t)a, (uint16_t)N_SAMP); Serial.println(F("#ok")); break;
        case 'W': fs_hz = (uint16_t)a; Serial.println(F("#ok")); break;
        case 'X': motor.move(0.0f); motor.disable(); Serial.println(F("#стоп")); break;
        case 'Y': diag_torque(a, 300); break;
        case 'M': diag_vel(a, 400); break;
        // ЧИСЛО ПАР ПОЛЮСОВ КОМАНДОЙ. Оно входит в перевод «механический угол
        // -> электрический», и ошибка в нём копится С ПРОЙДЕННЫМ ПУТЁМ: поле
        // тянет, пока накопленный сдвиг не переведёт его в удержание. Проверка
        // библиотеки (est. pp) тут бессильна — её допуск 0.5 рад на оборот, а
        // ловить надо ровно такую величину.
        // НАПРЯЖЕНИЕ ВЫРАВНИВАНИЯ ОТДЕЛЬНО ОТ РАБОЧЕГО. Выравнивание держит
        // ротор неподвижно доли секунды, и трение ему мешает: ротор встаёт
        // там, где его держит трение, а не в электрическом нуле. Это прямо
        // портит ZEA, а с ним и весь замкнутый контур.
        case 'S': motor.voltage_sensor_align = a;
                  Serial.print(F("#Uвыр=")); Serial.println(a, 2); break;
        case 'K': motor.pole_pairs = (int)(a + 0.5f);
                  Serial.print(F("#pp=")); Serial.println(motor.pole_pairs); break;
        case '?': status(); break;
        default: Serial.println(F("#? неизвестная команда"));
      }
      Serial.println(F("#ГОТОВ"));
    } else if (n < sizeof(cmd) - 1) cmd[n++] = c;
  }
}
