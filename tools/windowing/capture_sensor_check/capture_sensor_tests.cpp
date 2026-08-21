/*
 * Стенд для CaptureSensor: проверка без железа.
 *
 * ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ И ЧТО НЕТ. Проверяется ЛОГИКА: перевод скважности в
 * угол, поведение на шве кадра, распознавание негодных чтений и признак
 * годности. Не проверяется и не может быть проверено: что PA15 действительно
 * заведён на TIM2_CH1 и что датчик выдаёт то, что мы думаем, — это факты о
 * железе, снятые замером 21.08.2026 (reports/ЭНКОДЕР_PWM.md).
 *
 * ПОДЛОЖКА ЖИВЁТ НА СВОИХ КОНСТАНТАХ, А НЕ НА КОНСТАНТАХ КЛАССА. Если бы
 * генератор кадров брал наклон из CaptureSensor.h, порча наклона поехала бы
 * одновременно в подложку и в проверяемый код, и тест прошёл бы на сломанном
 * классе — то есть не проверял бы ничего. Поэтому числа ниже выписаны из
 * отчёта заново.
 *
 * ЗАПУСК: tools/windowing/capture_sensor_check/check.sh
 */
#include <cstdio>
#include <cmath>
#include <cstdlib>
#include "capture_sensor_host.h"
#include "CaptureSensor.h"

unsigned long HostReg::reads = 0;
unsigned long HostReg::writes = 0;

// ---- числа замера 21.08.2026, выписанные из reports/ЭНКОДЕР_PWM.md --------
static const double MEAS_SLOPE_LO = 362.6;   // два независимых прогона
static const double MEAS_SLOPE_HI = 362.8;
static const double MEAS_SPAN_LO  = 0.989;   // размах скважности
static const double MEAS_SPAN_HI  = 0.993;
static const double SIM_SLOPE     = 362.7;   // середина полосы — им и генерим
static const double SIM_SPAN      = 360.0 / SIM_SLOPE;
static const double SIM_DUTY_MIN  = 3.0 / 921.0;   // SENS_MIN_US / кадр
static const uint32_t SIM_PERIOD  = 156700;        // 922 мкс при 170 МГц

static const double TWO_PI = 6.283185307179586;
static const double DEG = 360.0 / TWO_PI;          // радианы -> градусы

static int failed = 0;
static const char *cur = "";
static void ok(bool cond, const char *what) {
  if (!cond) { failed++; printf("  СБОЙ  %s: %s\n", cur, what); }
}

// Доступ к getSensorAngle(): в бою его дёргает SimpleFOC через update(),
// стенду он нужен напрямую.
struct Probe : public CaptureSensor {
  explicit Probe(TIM_TypeDef *t) : CaptureSensor(t) {}
  using CaptureSensor::getSensorAngle;
};

// ---------------------------------------------------------------- подложка
//
// Модель кадра. Нарастающие фронты R0, R1...; кадр k = [Rk, Rk+1). В CCR1
// период кадра k, в CNT возраст текущего кадра k+1, в CCR2 — ширина импульса
// кадра k, пока не пришёл спад кадра k+1, и кадра k+1 после него.
struct Sim {
  TIM_TypeDef t;
  uint32_t period = SIM_PERIOD;

  static double duty_of(double turn) { return SIM_DUTY_MIN + turn * SIM_SPAN; }
  uint32_t high_of(double turn) const {
    return (uint32_t)llround(duty_of(turn) * (double)period);
  }
  void clear_sr() { t.SR.set(0); }
  /** Опрос в ВЫСОКОЙ фазе кадра: пара из одного кадра. */
  void poll_paired(double turn, bool cc2if_set = true) {
    uint32_t h = high_of(turn);
    t.CCR1.set(period); t.CCR2.set(h); t.CNT.set(h / 2);
    t.SR.set(cc2if_set ? TIM_SR_CC2IF : 0);
  }
  /** Опрос в НИЗКОЙ фазе: импульс уже от СЛЕДУЮЩЕГО кадра, период от старого. */
  void poll_mixed(double turn_next, bool cc2if_set = true) {
    uint32_t h = high_of(turn_next);
    t.CCR1.set(period); t.CCR2.set(h); t.CNT.set(h + (period - h) / 2);
    t.SR.set(cc2if_set ? TIM_SR_CC2IF : 0);
  }
  /** Датчик молчит: регистры замерли, возраст растёт. */
  void poll_dead(uint32_t age) {
    uint32_t h = high_of(0.3);
    t.CCR1.set(period); t.CCR2.set(h); t.CNT.set(age);
    t.SR.set(TIM_SR_CC2IF);
  }
};

static double wrap_deg(double d) {
  while (d < -180.0) d += 360.0;
  while (d >= 180.0) d -= 360.0;
  return d;
}

// =================================================== 1. перевод скважности
//
// Известные значения: доля оборота -> угол. Допуск 0.05 град — на порядок
// больше кванта захвата (0.026 шага энкодера) и на порядок меньше любой
// ошибки масштаба, которую стоит ловить.
static void test_scale() {
  cur = "перевод скважности";
  Sim s; Probe p(&s.t);
  const double turns[] = {0.0, 0.125, 0.25, 0.5, 0.75, 0.999};
  for (double u : turns) {
    s.poll_paired(u);
    double a = p.getSensorAngle() * DEG;      // градусы
    double want = u * 360.0;
    if (fabs(wrap_deg(a - want)) > 0.05) {
      printf("        доля %.3f: получено %.4f°, ожидалось %.4f°\n", u, a, want);
      ok(false, "угол не отвечает известной доле оборота");
    }
    ok(a >= 0.0 && a < 360.0, "угол обязан лежать в [0, 360)");
  }
}

// ============================================== 2. сверка наклона и размаха
//
// Та же независимая сверка, что в diag_metrics.py, только в обратную сторону:
// наклон обязан лежать в измеренной полосе, выведенный из него размах — в
// своей, а произведение обязано дать ровно 360. Это ловит подстановку
// наивного наклона и любую «round number» правку констант.
static void test_bridge_constants() {
  cur = "сверка наклона и размаха";
  ok(CAPSENS_DEG_PER_DUTY >= MEAS_SLOPE_LO && CAPSENS_DEG_PER_DUTY <= MEAS_SLOPE_HI,
     "наклон вне измеренной полосы 362.6..362.8 град на единицу скважности");
  ok(CAPSENS_DUTY_SPAN >= MEAS_SPAN_LO && CAPSENS_DUTY_SPAN <= MEAS_SPAN_HI,
     "выведенный размах вне измеренной полосы 0.989..0.993");
  double prod = (double)CAPSENS_DEG_PER_DUTY * (double)CAPSENS_DUTY_SPAN;
  ok(fabs(prod - 360.0) < 1e-3, "наклон x размах обязано быть ровно 360");
  double dead = 100.0 * (1.0 - (double)CAPSENS_DUTY_SPAN);
  ok(dead >= 0.7 && dead <= 1.1, "мёртвый участок кадра вне измеренных 0.7..1.1%");
}

// ============================================================== 3. шов кадра
//
// На шве скважность падает с верхней границы диапазона на нижнюю. Угол обязан
// пройти через 360->0 БЕЗ РАЗРЫВА: шаг по углу такой же, как вне шва. Именно
// здесь ошибка наклона видна, а внутри кадра она размазана и мала.
static void test_seam() {
  cur = "шов кадра";
  Sim s; Probe p(&s.t);
  const double du = 0.002;
  double prev = 0.0; bool have = false;
  for (int i = -6; i <= 6; i++) {
    double u = i * du; while (u < 0.0) u += 1.0;
    s.poll_paired(u);
    double a = p.getSensorAngle() * DEG;
    ok(a >= 0.0 && a < 360.0, "угол на шве вышел из [0, 360)");
    if (have) {
      double step = wrap_deg(a - prev);
      if (fabs(step - du * 360.0) > 0.05) {
        printf("        доля %.4f: шаг %.4f°, ожидался %.4f°\n",
               u, step, du * 360.0);
        ok(false, "разрыв угла на шве кадра — наклон не сходится с размахом");
      }
    }
    prev = a; have = true;
  }
}

// =================================================== 4. несогласованная пара
//
// Опрос в мёртвом участке кадра: в CCR2 уже импульс СЛЕДУЮЩЕГО кадра, в CCR1
// ещё период предыдущего. Такое чтение обязано называться негодным.
static void test_mixed_pair() {
  cur = "несогласованная пара";
  Sim s; Probe p(&s.t);
  CaptureStatus st;

  s.poll_mixed(0.4);
  p.readRaw(st);
  ok(!st.paired, "разнокадровая пара обязана быть названа несогласованной");
  ok(!st.ok, "разнокадровое чтение обязано быть негодным");
  ok(st.period_ok, "период здесь исправен — беда именно в паре");

  s.poll_paired(0.4);
  p.readRaw(st);
  ok(st.paired, "однокадровая пара обязана приниматься");
  ok(st.ok, "исправное чтение обязано быть годным");
}

// ==================================================== 5. частота опроса
//
// ПОЧЕМУ ПРЕЖНЯЯ ПРОВЕРКА ПО CC2IF НЕ РАБОТАЛА. Флаг CC2IF отвечает на вопрос
// «был ли спад с моего прошлого чтения», то есть зависит от ЧАСТОТЫ ОПРОСА, а
// не от данных: при редком опросе он взведён всегда (проверка тождественно
// истинна), при частом — почти никогда (тождественно ложна). Приговор класса
// обязан от него не зависеть вовсе.
static void test_poll_rate_independence() {
  cur = "частота опроса";
  Sim s; Probe p(&s.t);
  CaptureStatus st;

  // Редкий опрос: между чтениями ~20 кадров, спады были, CC2IF взведён.
  s.poll_mixed(0.6, /*cc2if_set=*/true);
  p.readRaw(st);
  ok(!st.ok, "при редком опросе взведённый CC2IF не должен оправдывать смесь кадров");

  // Частый опрос: спада с прошлого чтения не было, CC2IF чист — но пара
  // однокадровая, и чтение обязано быть принято.
  s.poll_paired(0.6, /*cc2if_set=*/false);
  p.readRaw(st);
  ok(st.ok, "при частом опросе чистый CC2IF не должен браковать исправную пару");
}

// ==================================================== 6. период вне границ
static void test_period_bounds() {
  cur = "период вне границ";
  Sim s; Probe p(&s.t);
  CaptureStatus st;

  s.period = 100000;   // 588 мкс — ниже нижней границы 136000 тиков
  s.poll_paired(0.3);
  p.readRaw(st);
  ok(!st.period_ok, "короткий период обязан быть назван негодным");
  ok(!st.ok, "чтение с коротким периодом обязано быть негодным");

  s.period = 300000;   // 1765 мкс — выше верхней границы 212500 тиков
  s.poll_paired(0.3);
  p.readRaw(st);
  ok(!st.period_ok, "длинный период обязан быть назван негодным");
  ok(!st.ok, "чтение с длинным периодом обязано быть негодным");

  s.period = SIM_PERIOD;
  s.poll_paired(0.3);
  p.readRaw(st);
  ok(st.period_ok, "нормальный период обязан приниматься");

  // Вырожденная ширина: импульса нет вовсе и импульс шире кадра.
  s.t.CCR1.set(SIM_PERIOD); s.t.CCR2.set(0); s.t.CNT.set(0); s.t.SR.set(0);
  p.readRaw(st);
  ok(!st.width_ok && !st.ok, "нулевая ширина импульса обязана быть негодной");
  s.t.CCR2.set(SIM_PERIOD + 10); s.t.CNT.set(0);
  p.readRaw(st);
  ok(!st.width_ok && !st.ok, "импульс шире кадра обязан быть негодным");
}

// ======================================================== 7. фронтов нет
//
// Датчик замолчал: регистры замерли на последних значениях, а CNT растёт,
// потому что обнулять его больше нечему. Это ЕДИНСТВЕННЫЙ способ отличить
// замерший датчик от неподвижного вала — по числам они одинаковы.
static void test_no_edges() {
  cur = "фронтов нет";
  Sim s; Probe p(&s.t);
  CaptureStatus st;

  s.poll_dead(CAPSENS_AGE_MAX_TICKS + 1);
  p.readRaw(st);
  ok(!st.edges, "отсутствие фронтов обязано быть НАЗВАНО, а не свалено в общий отказ");
  ok(!st.ok, "чтение без фронтов обязано быть негодным");

  s.poll_dead(CAPSENS_AGE_MAX_TICKS * 4);
  p.readRaw(st);
  ok(!st.edges && !st.ok, "долгое молчание обязано оставаться негодным");

  // Живой датчик: возраст меньше кадра, фронты есть.
  s.poll_paired(0.3);
  p.readRaw(st);
  ok(st.edges, "на живом датчике признак фронтов обязан быть истинным");
}

// ============================================ 8. годность не тождественна
//
// Признак, который всегда истинен, бесполезен; признак, который всегда ложен,
// будет отключён на второй день. Проверяется ОБА края и переходы между ними.
static void test_health() {
  cur = "годность не тождественна";
  Sim s; Probe p(&s.t);

  ok(!p.isHealthy(), "до первого чтения датчик обязан считаться негодным");

  for (int i = 0; i < 20; i++) { s.poll_paired(0.001 * i); p.getSensorAngle(); }
  ok(p.isHealthy(), "на исправных данных годность обязана быть ИСТИННОЙ");
  ok(p.badStreak() == 0, "счётчик негодных обязан обнуляться исправным чтением");

  // Одиночный провал (попадание в мёртвый участок кадра) годность НЕ роняет:
  // при частом опросе такое случается сотни раз в секунду и на исправном
  // железе, а выключение контура от каждого — это ложная тревога.
  s.poll_mixed(0.5); p.getSensorAngle();
  ok(p.isHealthy(), "одиночное негодное чтение не должно ронять годность");
  s.poll_paired(0.5); p.getSensorAngle();

  // Замерший датчик: годность обязана упасть.
  for (int i = 0; i < 40; i++) { s.poll_dead(CAPSENS_AGE_MAX_TICKS + 7); p.getSensorAngle(); }
  ok(!p.isHealthy(), "на замершем датчике годность обязана быть ЛОЖНОЙ");

  // Возврат требует нескольких подряд годных чтений, а не одного: одиночное
  // правдоподобное чтение бывает и у мёртвого датчика (CNT 32-битный и раз в
  // 25.3 с проходит через ноль).
  s.poll_paired(0.5); p.getSensorAngle();
  ok(!p.isHealthy(), "одно годное чтение не должно возвращать годность");
  for (int i = 0; i < 3; i++) { s.poll_paired(0.5); p.getSensorAngle(); }
  ok(p.isHealthy(), "после нескольких годных подряд годность обязана вернуться");

  // Удержание угла: на негодном чтении отдаётся ПОСЛЕДНИЙ ГОДНЫЙ, не ноль.
  s.poll_paired(0.25);
  float good = p.getSensorAngle();
  s.poll_dead(CAPSENS_AGE_MAX_TICKS + 7);
  float held = p.getSensorAngle();
  ok(fabs(held - good) < 1e-6f, "на негодном чтении обязан отдаваться последний годный угол");
}

// ============================================== 9. цена чтения (без ожидания)
//
// В контуре управления getSensorAngle() зовётся сотни-тысячи раз в секунду.
// Любой цикл ожидания «сейчас придёт хороший захват» останавливает контур с
// включёнными ключами. Проверяется не «быстро», а ФИКСИРОВАННОСТЬ: столько же
// обращений к регистрам на негодных данных, сколько на годных.
static void test_no_waiting() {
  cur = "цена чтения";
  Sim s; Probe p(&s.t);

  s.poll_paired(0.3);
  HostReg::reads = 0; HostReg::writes = 0;
  p.getSensorAngle();
  unsigned long r_good = HostReg::reads, w_good = HostReg::writes;
  ok(r_good == 4, "годное чтение обязано стоить ровно 4 чтения регистров (SR, CCR2, CCR1, CNT)");
  ok(w_good == 1, "годное чтение обязано стоить ровно 1 запись (сброс CC1OF)");

  s.poll_dead(CAPSENS_AGE_MAX_TICKS * 3);
  HostReg::reads = 0; HostReg::writes = 0;
  p.getSensorAngle();
  ok(HostReg::reads == r_good && HostReg::writes == w_good,
     "негодное чтение обязано стоить столько же — значит цикла ожидания нет");
}

// ======================================================= 10. настройка таймера
//
// Настроечные слова сверяются с captureBegin() из phone_link.ino побитно.
// Это не косметика: перепутанный CC2P ловил бы не тот фронт, снятый SMS
// оставил бы CNT свободно бегущим, и «возраст кадра» перестал бы быть
// возрастом кадра — а по числам это заметно не сразу.
static void test_timer_setup() {
  cur = "настройка таймера";
  Sim s; CaptureSensor c(&s.t);
  c.configureTimer();
  ok(s.t.CCMR1.get() == 0x2221u, "CCMR1: CC1S=01, CC2S=10, фильтры по 2");
  ok(s.t.CCER.get() == (TIM_CCER_CC1E | TIM_CCER_CC2E | TIM_CCER_CC2P),
     "CCER: оба канала включены, второй ловит СПАД");
  ok(s.t.SMCR.get() == ((5u << TIM_SMCR_TS_Pos) | (4u << TIM_SMCR_SMS_Pos)),
     "SMCR: TS=TI1FP1, SMS=reset — иначе CNT не возраст кадра");
  ok(s.t.PSC.get() == 0u, "PSC=0: тик TIM2 равен такту 170 МГц");
  ok(s.t.ARR.get() == 0xFFFFFFFFu, "ARR на максимум: TIM2 32-битный");
  ok((s.t.CR1.get() & TIM_CR1_CEN) != 0u, "таймер обязан быть запущен");
}

int main() {
  test_scale();
  test_bridge_constants();
  test_seam();
  test_mixed_pair();
  test_poll_rate_independence();
  test_period_bounds();
  test_no_edges();
  test_health();
  test_no_waiting();
  test_timer_setup();
  printf("ИТОГ: %s (%d провалов)\n", failed ? "ЕСТЬ ПРОВАЛЫ" : "всё сошлось", failed);
  return failed ? 1 : 0;
}
