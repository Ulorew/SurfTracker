/*
 * Закон управления приёмника: уставка -> экстраполяция -> пределы -> рампа,
 * плюс сторож и детектор срыва.
 *
 * Спецификация: docs/ПРОТОКОЛ_ТЕЛЕФОН_STM32.md, §4, §6, §8, §9.
 *
 * ЗАЧЕМ ОТДЕЛЬНЫМ ЗАГОЛОВКОМ. §9 требует, чтобы четыре мутации роняли четыре
 * названных теста. Пока логика жила внутри loop() боевой прошивки, каждая
 * мутация означала перепрошивку и прогон на железе — то есть тесты, которые
 * никто не станет гонять. Здесь она не зависит ни от Arduino, ни от
 * SimpleFOC, ни от времени: время подаётся снаружи аргументом. Поэтому
 * тысяча сценариев прогоняется на ноутбуке за миллисекунды, и прогоняется
 * ТОТ ЖЕ код, который поедет.
 *
 * Никаких обращений к millis(), micros() или железу. Это не стилистика: тест
 * должен уметь подать любое время, включая переполнение и разрывы.
 *
 * МУТАЦИИ для tools/link/control_tests.cpp. В боевой сборке не определены ни
 * одна — если определить, прошивка соберётся заведомо неисправной.
 *   MUT_NO_FRESHNESS   — свежесть seq выключена
 *   MUT_NO_EXTRAP_CAP  — потолок экстраполяции снят
 *   MUT_NO_RAMP        — рампа обойдена
 *   MUT_NO_SLIP_RESET  — интеграл срыва не сбрасывается при возобновлении
 */
#pragma once
#include <stdint.h>
#include <math.h>
#include "proto_v2.h"

namespace ctl {

struct Params {
  uint32_t watchdog_ms;
  uint32_t extrap_cap_ms;
  float    setpoint_limit;
  float    hw_limit;
  float    max_accel;
  float    slip_threshold;      // рад
  uint16_t slip_window_ms;
  uint16_t slip_step_ms;
};

inline Params defaults() {
  Params p;
  p.watchdog_ms    = 300;
  p.extrap_cap_ms  = 150;
  p.setpoint_limit = 2.0f;
  p.hw_limit       = 6.0f;
  p.max_accel      = 1.0f;
  p.slip_threshold = 3.0f * 3.14159265358979f / 180.0f;
  p.slip_window_ms = 1000;
  p.slip_step_ms   = 10;
  return p;
}

static const uint8_t SLIP_MAX = 100;   // 1000 мс / 10 мс

struct Ctl {
  Params p;

  // принятая уставка
  float    w_pkt, wdot_pkt;
  uint32_t t_rx_ms;
  uint8_t  last_seq;
  bool     had_first;

  // исполняемая команда
  float    w_ramp;

  // биты
  bool st_watchdog, wd_latch, st_extrap_cap, st_ramp_sat, st_clamp, st_slip;

  // детектор срыва
  float    integ_cmd;
  float    slip_theta[SLIP_MAX];
  float    slip_integ[SLIP_MAX];
  uint8_t  slip_i;
  bool     slip_full;
  uint32_t slip_last_ms;

  void init(Params pp) {
    p = pp;
    w_pkt = wdot_pkt = 0.0f;
    t_rx_ms = 0; last_seq = 0; had_first = false;
    w_ramp = 0.0f;
    // Сторож взведён ДО первого кадра: пока телефон не заговорил, вал стоять
    // обязан, и доложить об этом надо в первом же ответе.
    st_watchdog = true; wd_latch = true;
    st_extrap_cap = st_ramp_sat = st_clamp = st_slip = false;
    slipReset();
    slip_last_ms = 0;
  }

  void slipReset() {
    slip_i = 0; slip_full = false; integ_cmd = 0.0f; st_slip = false;
  }

  uint8_t slipN() const {
    uint8_t n = (uint8_t)(p.slip_window_ms / p.slip_step_ms);
    return n > SLIP_MAX ? SLIP_MAX : n;
  }

  /**
   * Принять кадр уставки. Возвращает true, если уставка ПРИМЕНЕНА.
   *
   * Устаревший кадр пачки не применяется — но ответ на него шлётся всё равно,
   * иначе телефон не сможет сопоставить его по seq и посчитать потери.
   *
   * ПЕРЕСИНХРОНИЗАЦИЯ РАЗРЕШЕНА ТОЛЬКО ПОСЛЕ СТОРОЖА, и это исправление
   * настоящего дефекта, найденного этим тестом.
   *
   * Было: «кадр вне окна свежести принимается безусловно — вдруг телефон
   * перезапустился», с условием (seq - last) & 0x7F > 64. Но кадр, отставший
   * ВСЕГО НА ЕДИНИЦУ, даёт разность 127, то есть больше 64. Под это условие
   * попадало любое отставание от 1 до 63, и применялось всё, кроме точных
   * дубликатов. Правило свежести не работало вовсе.
   *
   * Корень в том, что по одному seq перезапуск телефона и устаревший кадр
   * НЕРАЗЛИЧИМЫ: обе ситуации выглядят как «номер уехал назад». Различать их
   * можно только временем. Перезапуск телефона неизбежно даёт паузу, пауза
   * дольше watchdog_ms взводит сторож — вот он и служит разрешением на
   * пересинхронизацию. Устаревший кадр пачки паузы не даёт и потому
   * отвергается.
   */
  bool accept(uint8_t seq, float w, float wdot, uint32_t now) {
#ifdef MUT_NO_FRESHNESS
    bool fresh = true, resync = true;
#else
    bool fresh = !had_first || proto::isFresher(seq, last_seq);
    bool resync = st_watchdog;   // сторож взведён — принимаем любой номер
#endif
    if (!(fresh || resync)) return false;

#ifndef MUT_NO_SLIP_RESET
    // Возобновление после сторожа: вал стоял, а интеграл команды за это время
    // не рос — но угол мог измениться от руки или инерции. Не сбросив
    // интеграл, детектор срыва увидел бы это расхождение и поднял бит на
    // ровном месте.
    if (st_watchdog) slipReset();
#endif
    w_pkt = w; wdot_pkt = wdot;
    t_rx_ms = now;
    last_seq = seq;
    had_first = true;
    st_watchdog = false;
    return true;
  }

  /** Шаг управления. dt в секундах, theta — угол вала в радианах. */
  void step(uint32_t now, float dt, float theta) {
    // --- уставка: экстраполяция по w_dot с потолком ---
    float goal;
    if (!had_first || (uint32_t)(now - t_rx_ms) > p.watchdog_ms) {
      goal = 0.0f;
#ifndef MUT_NO_SLIP_RESET
      if (!st_watchdog) slipReset();
#endif
      st_watchdog = true;
      wd_latch = true;
      st_extrap_cap = false;
    } else {
      uint32_t age = now - t_rx_ms;
      st_extrap_cap = age > p.extrap_cap_ms;
#ifdef MUT_NO_EXTRAP_CAP
      float t = age * 1e-3f;
#else
      float t = (st_extrap_cap ? p.extrap_cap_ms : age) * 1e-3f;
#endif
      goal = w_pkt + wdot_pkt * t;
    }

    // --- пределы: уставка, затем аппаратный ---
    st_clamp = false;
    if (goal >  p.setpoint_limit) { goal =  p.setpoint_limit; st_clamp = true; }
    if (goal < -p.setpoint_limit) { goal = -p.setpoint_limit; st_clamp = true; }
    if (goal >  p.hw_limit) { goal =  p.hw_limit; st_clamp = true; }
    if (goal < -p.hw_limit) { goal = -p.hw_limit; st_clamp = true; }

    // --- рампа: последний рубеж, активна всегда ---
    float stp = p.max_accel * dt;
    st_ramp_sat = fabsf(goal - w_ramp) > stp && stp > 0.0f;
#ifdef MUT_NO_RAMP
    w_ramp = goal;
#else
    if (w_ramp < goal) w_ramp = (w_ramp + stp > goal) ? goal : w_ramp + stp;
    else if (w_ramp > goal) w_ramp = (w_ramp - stp < goal) ? goal : w_ramp - stp;
#endif

    integ_cmd += w_ramp * dt;

    // --- детектор срыва: угол против интеграла команды на окне ---
    if ((uint32_t)(now - slip_last_ms) >= p.slip_step_ms) {
      slip_last_ms = now;
      uint8_t n = slipN();
      slip_theta[slip_i] = theta;
      slip_integ[slip_i] = integ_cmd;
      slip_i = (uint8_t)((slip_i + 1) % n);
      if (slip_i == 0) slip_full = true;
      if (slip_full) {
        // slip_i — теперь самый старый отсчёт окна
        float d_theta = theta - slip_theta[slip_i];
        float d_cmd   = integ_cmd - slip_integ[slip_i];
        st_slip = fabsf(d_theta - d_cmd) > p.slip_threshold;
      }
    }
  }

  /** Байт статуса. Защёлка сторожа СНИМАЕТСЯ чтением — см. §8. */
  uint8_t statusByte(bool enc_ok) {
    uint8_t st = 0;
    if (wd_latch) st |= proto::ST_WATCHDOG;
    wd_latch = false;
    if (st_extrap_cap) st |= proto::ST_EXTRAP_CAP;
    if (st_ramp_sat)   st |= proto::ST_RAMP_SAT;
    if (enc_ok)        st |= proto::ST_ENC_OK;
    if (st_clamp)      st |= proto::ST_CLAMP;
    if (st_slip)       st |= proto::ST_SLIP;
    return st;
  }
};

}  // namespace ctl
