/*
 * ESP32 + AS5048A по SPI: пробник «датчик вообще отвечает и что он видит».
 *
 * ЗАЧЕМ ОТДЕЛЬНЫМ СКЕТЧЕМ. На стенде уже есть AS5048 — но читается он с
 * ДРУГОЙ платы (STM32 B-G431B-ESC1) и ДРУГИМ способом: по ширине импульса,
 * MagneticSensorPWM на PB8. Здесь проверяется новый путь целиком — новый
 * интерфейс (SPI), новый хозяин шины (ESP32), новые провода. Мешать это с
 * прошивкой моста bt_link нельзя: тогда отказ нельзя будет отнести ни к
 * датчику, ни к мосту.
 *
 * РАСПИНОВКА (провода на стенде, цвета — как приехали):
 *   GPIO18  CLK   жёлтый
 *   GPIO19  MISO  синий
 *   GPIO21  MOSI  зелёный
 *   GPIO5   CS    белый
 *   GND     GND   чёрный
 *   3V3     VCC   ПИТАНИЕ — шестой провод, без него шина читается как мусор.
 *
 * ПОЧЕМУ MOSI НА 21, А НЕ НА 23. У VSPI по умолчанию MOSI=23, но пины ESP32
 * разведены через GPIO-матрицу, и любой из них назначается программно —
 * SPI.begin(SCK, MISO, MOSI, SS). Молчаливой платы за это нет: единственное
 * условие — не пользоваться SPI.begin() без аргументов, иначе драйвер уйдёт
 * на 23-й и датчик не ответит, хотя провода в порядке.
 *
 * ПОЧЕМУ CS ИМЕННО 5. Это strapping-пин: в момент загрузки он обязан быть
 * высоким. Для CS это и есть покой (подтяжка вверх + CS_IDLE=HIGH), так что
 * выбор не конфликтует с загрузкой. Обратное — вешать на 5 что-то, что
 * тянет вниз, — стоило бы незагружающейся платы.
 *
 * SPI-РЕЖИМ 1 (CPOL=0, CPHA=1) — по даташиту AS5048A. Режим 0 даёт сдвиг на
 * бит и «почти правдоподобные» углы: не мусор, который заметен сразу, а
 * тихо неверные числа. Поэтому режим здесь константа, а не подбор.
 *
 * ЧАСТОТА 1 МГц, хотя датчик держит 10. Провода на стенде — дюпоны без витой
 * пары и без земли рядом с каждым сигналом; на 10 МГц такая шина ловит сбои
 * чётности, которые выглядят как «датчик врёт», а не «провод длинный».
 * Разгонять есть смысл только после того, как на 1 МГц всё сойдётся.
 *
 * ФОРМАТ ВЫВОДА — «label:value», как у Arduino Serial Plotter. Одна строка на
 * отсчёт, 50 Гц. Тот же поток читает и рисует tools/stand/enc_plot.py, так
 * что график есть и в IDE, и в терминале, и никакого второго формата
 * поддерживать не надо. Всё, что НЕ отсчёт (шапка, диагностика, отказы),
 * начинается с '#': плоттеры такие строки игнорируют, человек читает.
 *
 * Счётчики ошибок чётности и EF идут В КАЖДОЙ строке, а не только при сбое:
 * одиночный сбой на дюпонах глазами по бегущим строкам не поймать, он виден
 * только как приращение счётчика.
 *
 * СИГНАЛЫ СВЕТОДИОДОМ (GPIO2). На стенде не видно ни момента заливки, ни
 * того, что происходит, — поэтому состояние выведено наружу:
 *   5 быстрых миганий  — прошивка стартовала
 *   горит ровно        — датчик отвечает, диагностика чистая
 *   мигает 2 Гц        — датчик отвечает, но жалуется (магнит/AGC/ошибка)
 *   мигает 1 Гц        — датчика нет на шине (0x0000 или 0xFFFF в ответах)
 *
 * Сборка:  esp/build.sh as5048a_probe
 * Заливка: esp/build.sh as5048a_probe --прошить   (перезапишет мост bt_link!)
 */

#include <SPI.h>
#include <driver/gpio.h>   // подтяжки ставятся мимо pinMode: см. тест линии MISO

// ---------------------- распиновка и параметры ----------------------
static const int PIN_SCK  = 18;
static const int PIN_MISO = 19;
static const int PIN_MOSI = 21;
static const int PIN_CS   = 5;
static const int PIN_LED  = 2;

static const uint32_t OUT_HZ   = 50;        // строк в секунду; см. шапку про формат

// ВРЕМЯ ШЛЁТ ПЛАТА, А НЕ СТАВИТ ХОСТ. Это не мелочь: строки приходят на хост
// пачками по мере опустошения буфера USB, и метка времени приёма даёт
// слипшиеся отсчёты. Первый же разбор такой записи выдал скорость вала
// 11 546 320 °/с — деление на почти нулевой интервал. По времени платы такого
// не бывает: интервал задан циклом, а не транспортом.
//
// Цена: Arduino Serial Plotter нарисует t отдельной растущей линией и сожмёт
// шкалу остальных. Кому мешает — WITH_TIME 0, тогда формат прежний.
#define WITH_TIME 1
static const uint32_t SPI_HZ   = 1000000;   // см. шапку: не 10 МГц
static const uint8_t  SPI_MODE = SPI_MODE1; // CPOL=0, CPHA=1 — по даташиту

// ---------------------- регистры AS5048A ----------------------
static const uint16_t REG_NOP       = 0x0000;
static const uint16_t REG_ERRFL     = 0x0001;  // флаги ошибок, читается со сбросом
static const uint16_t REG_DIAAGC    = 0x3FFD;  // диагностика + AGC
static const uint16_t REG_MAGNITUDE = 0x3FFE;  // амплитуда поля
static const uint16_t REG_ANGLE     = 0x3FFF;  // угол, 14 бит

SPIClass* spi = nullptr;

static uint32_t parity_errors = 0;
static uint32_t ef_errors     = 0;
static uint16_t last_raw_a = 0, last_raw_m = 0, last_raw_d = 0;

// Чётность ЧЁТНАЯ по битам 0..14 — в бит 15 кладётся так, чтобы число
// единиц во всём слове стало чётным. Считать «сколько единиц в 16 битах»
// вместо 15 — классическая ошибка: команда уедет с неверной чётностью, и
// датчик ответит флагом ошибки, а не данными.
static bool parity_even(uint16_t v) {
  v ^= v >> 8; v ^= v >> 4; v ^= v >> 2; v ^= v >> 1;
  return v & 1;
}

static uint16_t frame_read(uint16_t addr) {
  uint16_t cmd = 0x4000 | (addr & 0x3FFF);        // бит14 = 1: чтение
  if (parity_even(cmd)) cmd |= 0x8000;
  return cmd;
}

// Одна транзакция. ВАЖНО: AS5048A отдаёт результат ПРЕДЫДУЩЕЙ команды,
// поэтому один обмен ничего не значит — читать надо парой (команда, затем
// NOP или следующая команда). Функция ниже это и делает.
static uint16_t xfer(uint16_t frame) {
  spi->beginTransaction(SPISettings(SPI_HZ, MSBFIRST, SPI_MODE));
  digitalWrite(PIN_CS, LOW);
  delayMicroseconds(1);                            // t_L: CS до первого фронта
  uint16_t r = spi->transfer16(frame);
  digitalWrite(PIN_CS, HIGH);
  spi->endTransaction();
  delayMicroseconds(1);                            // t_CSn: пауза между кадрами
  return r;
}

static uint16_t last_raw = 0;

// Возвращает 14 бит данных; ok=false, если ответ не прошёл проверку.
static uint16_t read_reg(uint16_t addr, bool* ok) {
  xfer(frame_read(addr));                          // команда
  uint16_t raw = xfer(frame_read(REG_NOP));        // ответ приходит на следующем кадре
  last_raw = raw;                                  // сырое слово нужно для отказов: 0x0000 и 0xFFFF значат разное
  bool par_ok = !parity_even(raw);                 // во всём слове должно быть чётное число единиц
  bool ef     = raw & 0x4000;                      // бит14 — флаг ошибки транзакции
  if (!par_ok) parity_errors++;
  if (ef)      ef_errors++;
  if (ok) *ok = par_ok && !ef;
  return raw & 0x3FFF;
}

// ---------------------- тест линии MOSI ----------------------
//
// ЗАЧЕМ. Ответы датчика могут выглядеть правдоподобно и при ОБОРВАННОМ MOSI:
// если команда не доходит, датчик всё равно тактируется и что-то отдаёт, а
// «случайный угол при отсутствующем магните» и «мусор из-за необорванной
// команды» на глаз неотличимы. Отличить их можно только ответом на команду,
// которую датчик обязан ЗАБРАКОВАТЬ.
//
// Приём: посылается кадр с НАМЕРЕННО неверной чётностью. Живой MOSI -> датчик
// поднимает в ERRFL бит parity (0x4) и флаг EF в следующем ответе. Оборванный
// MOSI -> ERRFL остаётся нулевым, потому что команды датчик не видел вовсе.
//
// ERRFL самоочищается при чтении, поэтому тест не оставляет следов.
static void mosi_line_test() {
  uint16_t bad = frame_read(REG_ANGLE) ^ 0x8000;   // чётность перевёрнута
  xfer(bad);
  xfer(frame_read(REG_NOP));
  xfer(frame_read(REG_ERRFL));
  uint16_t errfl = xfer(frame_read(REG_NOP)) & 0x3FFF;

  if (errfl & 0x4) {
    Serial.println("#   тест MOSI: команда ДОХОДИТ (датчик забраковал кадр по чётности)");
  } else {
    Serial.printf("#   тест MOSI: команду НЕ ВИДНО (ERRFL=0x%04X после заведомо битого кадра) "
                  "-> проверить зелёный провод на GPIO21\n", errfl);
  }
  xfer(frame_read(REG_ERRFL));                     // дочитать, чтобы регистр ушёл чистым
  xfer(frame_read(REG_NOP));
}

// ---------------------- тест линии MISO ----------------------
//
// РАЗЛИЧАЕТ ДВА ОТКАЗА, которые по данным выглядят одинаково: «провод MISO ни
// к чему не подключён» и «подключён, но датчик не отвечает». Приём прямой: на
// линию по очереди ставится внутренняя подтяжка вниз и вверх. Если её никто
// не держит, показания пойдут ЗА подтяжкой (0x0000, потом 0xFFFF). Если на
// том конце живой выход датчика, он перебьёт подтяжку и результат в обоих
// случаях будет одинаковым.
//
// Подтяжка ставится через gpio_set_pull_mode, а НЕ через pinMode: pinMode на
// ESP32 переназначает функцию пина и сорвал бы маршрут SPI, после чего тест
// показывал бы состояние отвязанного пина, а не шины.
static void miso_line_test() {
  gpio_set_pull_mode((gpio_num_t)PIN_MISO, GPIO_PULLDOWN_ONLY);
  delay(2);
  xfer(frame_read(REG_ANGLE));
  uint16_t down = xfer(frame_read(REG_NOP));

  gpio_set_pull_mode((gpio_num_t)PIN_MISO, GPIO_PULLUP_ONLY);
  delay(2);
  xfer(frame_read(REG_ANGLE));
  uint16_t up = xfer(frame_read(REG_NOP));

  gpio_set_pull_mode((gpio_num_t)PIN_MISO, GPIO_FLOATING);

  if (down == 0x0000 && up == 0xFFFF) {
    Serial.println("#   тест MISO: линию НИКТО не держит -> нет питания у датчика, "
                   "либо не подключён MISO/GND, либо CS не доходит");
  } else if (down == up) {
    Serial.printf("#   тест MISO: линию кто-то держит (0x%04X при обеих подтяжках) -> "
                  "провод есть, датчик отвечает не по протоколу: проверить SPI-режим, "
                  "частоту и что это AS5048A (а не 5048B/I2C)\n", down);
  } else {
    Serial.printf("#   тест MISO: неустойчиво (вниз 0x%04X, вверх 0x%04X) -> "
                  "плохой контакт или наводка\n", down, up);
  }
}

// ---------------------- светодиод ----------------------
static void blink(int n, int on_ms, int off_ms) {
  for (int i = 0; i < n; i++) {
    digitalWrite(PIN_LED, HIGH); delay(on_ms);
    digitalWrite(PIN_LED, LOW);  delay(off_ms);
  }
}

void setup() {
  pinMode(PIN_LED, OUTPUT);
  pinMode(PIN_CS, OUTPUT);
  digitalWrite(PIN_CS, HIGH);                      // CS в покое — высокий, ещё до SPI.begin
  Serial.begin(115200);
  delay(300);
  blink(5, 60, 60);                                // «прошивка стартовала»

  spi = new SPIClass(VSPI);
  spi->begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CS); // ЯВНО: иначе MOSI уйдёт на 23
  // CS дёргается вручную, а не аппаратно: между кадрами AS5048A обязательна
  // пауза, а аппаратный CS её не даёт.

  Serial.println();
  Serial.println("# == AS5048A probe ==");
  Serial.printf("# SCK=%d MISO=%d MOSI=%d CS=%d, %lu Гц, SPI_MODE1, вывод %lu Гц\n",
                PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CS,
                (unsigned long)SPI_HZ, (unsigned long)OUT_HZ);

  // ERRFL читается первым и намеренно: регистр самоочищается при чтении, и в
  // нём лежат ошибки, накопленные с подачи питания — в том числе от мусорных
  // кадров в момент загрузки, когда strapping-пин 5 ещё дёргался. Не сбросив
  // его здесь, мы бы всю сессию смотрели на ошибку, которой уже нет.
  bool ok;
  uint16_t errfl = read_reg(REG_ERRFL, &ok);
  Serial.printf("# ERRFL при старте: 0x%04X%s%s%s\n", errfl,
                (errfl & 0x1) ? " [framing]" : "",
                (errfl & 0x2) ? " [invalid command]" : "",
                (errfl & 0x4) ? " [parity]" : "");
  mosi_line_test();                                // до первых данных: см. шапку теста
  parity_errors = 0; ef_errors = 0;                // стартовый мусор в счёт не идёт
  Serial.println("# t:мс_платы angle:град mag:амплитуда agc:усиление perr:чётность ef:флаг_ошибки");
}

// Прошлое состояние диагностики. Печатать расшифровку в каждой строке нельзя
// (она сломала бы формат плоттера), а печатать только раз — значит потерять
// событие «магнит уехал в середине прогона». Поэтому '#'-строка выдаётся на
// ИЗМЕНЕНИЕ состояния: тихо, пока всё стабильно, и заметно в момент перемены.
static uint16_t last_flags = 0xFFFF;

void loop() {
  static uint32_t next_us = 0;
  const uint32_t period_us = 1000000UL / OUT_HZ;
  if ((int32_t)(micros() - next_us) < 0) return;
  // Шаг СЧИТАЕТСЯ ОТ ПРЕДЫДУЩЕЙ УСТАВКИ, а не от «сейчас»: иначе к периоду
  // каждый раз прибавляется длительность тела цикла и частота уползает вниз.
  next_us += period_us;
  if ((int32_t)(micros() - next_us) > (int32_t)period_us) next_us = micros() + period_us;  // отстали — не догоняем пачкой

  // Метка времени ставится ЗДЕСЬ, до чтений, а не в printf. В printf она
  // отставала на длительность трёх обменов по SPI и на ожидание буфера UART,
  // и интервал между отсчётами гулял 9..20 мс при заданных ровно 20.
  const uint32_t t_ms = millis();

  bool ok_a, ok_m, ok_d;
  uint16_t angle = read_reg(REG_ANGLE,     &ok_a); last_raw_a = last_raw;
  uint16_t mag   = read_reg(REG_MAGNITUDE, &ok_m); last_raw_m = last_raw;
  uint16_t diag  = read_reg(REG_DIAAGC,    &ok_d); last_raw_d = last_raw;

  // «Датчика нет» опознаётся по вырожденным ответам: висящий MISO даёт
  // сплошные нули (подтяжка вниз/земля) или единицы (подтяжка вверх). Так
  // отличается «не подключен» от «подключен и жалуется» — иначе оба случая
  // выглядели бы одинаково плохими числами.
  bool dead = (angle == 0x0000 && mag == 0x0000 && diag == 0x0000) ||
              (angle == 0x3FFF && mag == 0x3FFF && diag == 0x3FFF);
  if (dead) {
    // Печатается РАЗ В СЕКУНДУ и с сырыми словами. Общее «нет ответа» ничего
    // не сужает: 0x0000 и 0xFFFF на шине означают разные неисправности, и
    // различить их можно только по сырому слову вместе с тестом линии ниже.
    static uint32_t last_msg = 0;
    if (millis() - last_msg > 1000) {
      last_msg = millis();
      Serial.printf("# НЕТ ОТВЕТА: сырое angle=0x%04X mag=0x%04X diag=0x%04X — %s\n",
                    last_raw_a, last_raw_m, last_raw_d,
                    (last_raw_a == 0xFFFF) ? "линия висит вверх" :
                    (last_raw_a == 0x0000) ? "линия висит вниз" : "смешанный мусор");
      miso_line_test();
    }
    digitalWrite(PIN_LED, (millis() / 500) & 1);
    return;
  }

  uint8_t agc      = diag & 0xFF;
  bool    ocf      = diag & 0x0100;   // внутренняя калибровка завершена
  bool    cof      = diag & 0x0200;   // переполнение CORDIC — угол недостоверен
  bool    comp_low = diag & 0x0400;   // поле слабое: магнит далеко
  bool    comp_hi  = diag & 0x0800;   // поле сильное: магнит близко

  uint16_t flags = (diag >> 8) & 0x0F;
  if (flags != last_flags) {
    Serial.printf("# диагностика: DIAAGC=0x%04X %s%s%s%s AGC=%u маг=%u\n", diag,
                  ocf ? "OCF " : "!OCF(калибровка не завершена) ",
                  cof ? "COF(угол недостоверен) " : "",
                  comp_low ? "МАГНИТ_ДАЛЕКО " : "",
                  comp_hi ? "МАГНИТ_БЛИЗКО " : "",
                  agc, mag);
    last_flags = flags;
  }

  // Строка отсчёта. Угол в градусах, а не в тиках: плоттеру нужна величина в
  // осмысленных единицах, а сырые тики всё равно восстанавливаются делением.
#if WITH_TIME
  Serial.printf("t:%lu,angle:%.3f,mag:%u,agc:%u,perr:%lu,ef:%lu\n",
                (unsigned long)t_ms,
                angle * 360.0f / 16384.0f, mag, agc,
#else
  Serial.printf("angle:%.3f,mag:%u,agc:%u,perr:%lu,ef:%lu\n",
                angle * 360.0f / 16384.0f, mag, agc,
#endif
                (unsigned long)parity_errors, (unsigned long)ef_errors);

  bool clean = ok_a && ok_m && ok_d && ocf && !cof && !comp_low && !comp_hi;
  // Светодиод переключается по времени, а не delay-ами: цикл теперь держит
  // частоту вывода, и любой delay здесь съел бы её.
  if (clean) digitalWrite(PIN_LED, HIGH);
  else       digitalWrite(PIN_LED, (millis() / 250) & 1);
}
