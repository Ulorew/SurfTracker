/*
 * B-G431B-ESC1: сличение двух трактов чтения AS5048A по PWM, БЕЗ МОТОРА.
 *
 * Отвечает ровно на один вопрос: во сколько раз аппаратный захват разрешает
 * лучше, чем micros(). Для этого мотор не нужен вовсе — на неподвижном валу
 * истинный угол постоянен, и весь разброс показаний есть шум тракта целиком.
 *
 * ПОЧЕМУ БЕЗ МОТОРА, ХОТЯ РЕФЕРЕНС ЕГО ВКЛЮЧАЕТ. Мотор добавляет к замеру
 * температуру обмоток, наводку силовой части и риск раскрутки; при этом на
 * главный вопрос он не влияет — разрешение тракта от вращения не зависит.
 * Вращение и контур придут отдельным скетчем, когда будет что ими проверять.
 * Заодно это снимает вопрос напряжения: обмотки обесточены, вал свободен.
 *
 * ДВА ТРАКТА ОДНОВРЕМЕННО, ОДИН СИГНАЛ. Энкодер запаян и на PB8, и на PA15 —
 * один выход на два высокоомных входа. Оба тракта читаются в одном проходе,
 * поэтому отсчёты СТРОГО ПАРНЫЕ: разница между методами не смешана с
 * разницей между прогонами, температурами и положениями магнита.
 *
 *   PB8  — прерывание + micros(), «как сейчас в проде»
 *   PA15 — аппаратный захват TIM2 в режиме PWM input, «как предлагается»
 *
 * ПОЧЕМУ ИМЕННО PA15. Проверено по PeripheralPins ядра 2.10.1: PA15 это
 * TIM2_CH1, а PB8 — TIM4_CH3. Режим PWM input работает только от TI1/TI2,
 * поэтому на PB8 аппаратный захват периода и импульса НЕВОЗМОЖЕН в принципе.
 * TIM2 при этом 32-битный (кадр в 1 мс не переполнит при PSC=0) и свободен:
 * TIMER_TONE=TIM6, TIMER_SERVO=TIM7, а привод для 6-PWM берёт только
 * advanced-таймеры, потому что у TIM2 нет вставки мёртвого времени.
 *
 * КАНАЛ. Serial на этой плате — USART2 на PB3/PB4, это VCP встроенного
 * ST-LINK (variant_B_G431B_ESC1.h: «Connected to ST-Link»). То есть лог идёт
 * по тому же USB, что и прошивка, отдельный переходник не нужен.
 *
 * Скорость 921600, а не 2000000: поток 26 байт на кадр при 1 кГц это 26 кБ/с,
 * то есть 260 кбод с запасом влезает, а 921600 — заведомо поддержанная
 * ST-LINK величина. Гнаться за 2 Мбод значит рисковать связью ради ничего.
 */
#include <Arduino.h>
#include "rec.h"      // формат лога: отдельно, см. пояснение внутри

// ---- пины и константы, ИЗМЕРЕННЫЕ на этом экземпляре -----------------------
// Взяты из боевой прошивки phone_link.ino, а не из даташита: там записано,
// что полный оборот рукой дал 3..919 мкс при периоде 921 мкс, и что прежнее
// значение 7 «приехало со старого стенда и было неверным — задранная вдвое
// нижняя граница смещала ВЕСЬ масштаб».
static const int      PIN_ISR    = PB8;
// Границы нужны РАЗБОРЩИКУ, а не скетчу: угол здесь не считается вовсе, в лог
// идут сырые тики и микросекунды. Держатся тут, чтобы значение и его источник
// лежали рядом с кодом, который снимает данные.
static const uint32_t SENS_MIN_US __attribute__((unused)) = 3;
static const uint32_t SENS_MAX_US __attribute__((unused)) = 919;

// Кадр AS5048A: init 12 + error 4 + data 4095 + exit 8 = 4119 тактов,
// высокий уровень держится (16 + data) тактов.
static const float FRAME_CLK  = 4119.0f;
static const float OFFSET_CLK = 16.0f;
static const float SPAN       = 4096.0f;

static const uint32_t TIM2_HZ = 170000000UL;
static const uint32_t FRAME_US_MIN = 800, FRAME_US_MAX = 1250;

// ---- тракт 1: прерывание + micros() (как в проде) --------------------------
volatile uint32_t isr_t_rise = 0;
volatile uint32_t isr_period = 1000;
volatile uint32_t isr_high   = 0;
volatile uint32_t isr_edges  = 0;

void onEdge() {
  uint32_t now = micros();
  if (digitalRead(PIN_ISR)) { isr_period = now - isr_t_rise; isr_t_rise = now; }
  else                      { isr_high   = now - isr_t_rise; }
  isr_edges++;
}

// ---- тракт 2: аппаратный захват TIM2 на PA15 -------------------------------
void capture_begin() {
  __HAL_RCC_GPIOA_CLK_ENABLE();
  __HAL_RCC_TIM2_CLK_ENABLE();

  // PA15 после сброса отдан под JTDI. Отладке по SWD (PA13/PA14) это не
  // мешает, но пин надо явно перевести в AF1 = TIM2_CH1.
  GPIO_InitTypeDef g = {0};
  g.Pin = GPIO_PIN_15;
  g.Mode = GPIO_MODE_AF_PP;
  g.Pull = GPIO_NOPULL;
  g.Speed = GPIO_SPEED_FREQ_HIGH;
  g.Alternate = GPIO_AF1_TIM2;
  HAL_GPIO_Init(GPIOA, &g);

  TIM2->CR1 = 0;
  TIM2->PSC = 0;                // 170 МГц, шаг 5.9 нс
  TIM2->ARR = 0xFFFFFFFF;       // 32 бита: кадр 1 мс = 170 000 отсчётов

  // CC1S=01 (IC1<-TI1), IC1F=0010 (фильтр N=4), CC2S=10 (IC2<-TI1), IC2F=0010.
  // Фильтр давит помеху силовой части и задерживает оба фронта ОДИНАКОВО,
  // поэтому на отношение high/period не влияет.
  TIM2->CCMR1 = (1u << 0) | (2u << 4) | (2u << 8) | (2u << 12);
  TIM2->CCER  = TIM_CCER_CC1E | TIM_CCER_CC2E | TIM_CCER_CC2P;
  TIM2->SMCR  = (5u << TIM_SMCR_TS_Pos) | (4u << TIM_SMCR_SMS_Pos);
  TIM2->CR1  |= TIM_CR1_CEN;
}

// Возвращает false, если кадр не похож на кадр энкодера.
// fresh говорит, был ли НОВЫЙ захват с прошлого чтения: без этого зависший
// датчик неотличим от неподвижного вала — регистры просто замрут на последнем
// значении, и угол останется правдоподобным.
bool capture_read(uint32_t &period, uint32_t &high, bool &fresh) {
  fresh = (TIM2->SR & TIM_SR_CC1IF) != 0;   // чтение CCR1 ниже сбросит флаг

  // ПЕРЕЧИТЫВАНИЕ В ЦИКЛЕ. Между чтением CCR1 и CCR2 может прилететь новый
  // кадр, и тогда период одного смешается с импульсом другого. Проверка
  // «CCR1 не изменился за время чтения» ловит это; повторяем до четырёх раз,
  // а не однократно, иначе второе попадание на границу даёт ту же смесь.
  uint32_t p = 0, h = 0;
  for (int i = 0; i < 4; i++) {
    p = TIM2->CCR1;
    h = TIM2->CCR2;
    if (p == TIM2->CCR1) break;
  }
  period = p; high = h;

  const uint32_t lo = FRAME_US_MIN * (TIM2_HZ / 1000000UL);
  const uint32_t hi = FRAME_US_MAX * (TIM2_HZ / 1000000UL);
  return (p > lo) && (p < hi) && (h > 0) && (h < p);
}

uint16_t seq = 0;

void setup() {
  Serial.begin(921600);
  pinMode(PIN_ISR, INPUT);
  attachInterrupt(digitalPinToInterrupt(PIN_ISR), onEdge, CHANGE);
  capture_begin();
  delay(200);                    // дать датчику выдать несколько кадров
}

void loop() {
  // Чуть чаще кадра энкодера (921 мкс), чтобы не пропускать и не дублировать
  // без нужды. Точная привязка не нужна: каждая запись самодостаточна.
  static uint32_t last = 0;
  uint32_t now = micros();
  if (now - last < 900) return;
  last = now;

  Rec r;
  r.sync = 0xA55A;
  r.seq  = seq++;
  r.t_us = now;
  r.flags = 0;

  uint32_t p, h; bool fresh;
  if (!capture_read(p, h, fresh)) r.flags |= 1;
  if (!fresh)                     r.flags |= 4;
  r.cap_period = p;
  r.cap_high   = h;

  noInterrupts();
  r.isr_period = isr_period;
  r.isr_high   = isr_high;
  interrupts();
  if (r.isr_period < FRAME_US_MIN || r.isr_period > FRAME_US_MAX) r.flags |= 2;

  r.crc = rec_crc(r);
  Serial.write((uint8_t*)&r, sizeof(r));
}
