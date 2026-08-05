"""Закрепление ошибки (error_lockin) — главный известный риск конструкции.

Механизм, вскрытый замером и задокументированный потактово на racing t415:
детекция, не выбранная целью, заводит СВОЙ теневой; после этого правило
исключения 0.7 запирает её, а теневой сыт (цель детектится каждый такт),
поэтому правило смерти "3 такта без питания" выхода не даёт. Ловушка
абсорбирующая.

Тесты ниже НЕ утверждают, что всё хорошо. Они фиксируют риск числом и
различают конструкции: если кто-то починит ловушку, они упадут — и это
правильно, число в отчёте придётся обновить вместе с кодом.

Ожидания были записаны ДО замера (мини-тикет заморозки):
  - прод: P(возврат) > 0.8       -> НЕ ВЫПОЛНИЛОСЬ, измерено 0.50;
  - голая база: P низкая (~0.2)  -> не выполнилось, 0.59 (ловушка бьёт по
    обеим конструкциям примерно одинаково, а не только по голой базе);
  - мутация "теневой бессмертен" роняет P -> НЕ ВЫПОЛНИЛОСЬ и не могло:
    теневой цели и так бессмертен, потому что его кормит сама цель.
"""
import random

import pytest

from track_bench import cfg_with, lockin_probe, scen_error_lockin

PROD = dict(ENABLE_SIZE_SCORING=True, FILTER_LEVEL=2,
            ENABLE_MAHALANOBIS_GATE=True, ENABLE_SHADOW_TRACKS=True)
NO_SHADOWS = dict(PROD, ENABLE_SHADOW_TRACKS=False)
IMMORTAL = dict(PROD, SHADOW_MAX_MISSES=10 ** 6)

# подсетка сетки отчёта: три ячейки, покрывающие узкое и широкое расхождение
CELLS = ((0.5, 10.0, 0.08), (1.0, 20.0, 0.15), (2.0, 40.0, 0.22))
SEEDS = 25


def tolerance(n):
    """Допуск сравнения долей на выборке n прогонов.

    Два прогона, а не три. Три — это ровно эффект мутации
    SHADOW_MAX_MISSES=1 (0.0400 при n=75), то есть допуск накрывал бы саму
    проверяемую поломку. Держится тестом test_this_test_can_actually_see_a_
    broken_death_rule: при изменении SEEDS он и покажет, если запас пропал.
    """
    return 2.0 / n


def measure(over, k_list=(5, 15)):
    cfg = cfg_with(**over)
    n = trapped = 0
    ret = {k: 0 for k in k_list}
    for d_min, alpha, speed in CELLS:
        for i in range(SEEDS):
            targets = scen_error_lockin(random.Random(i), d_min=d_min,
                                         alpha_deg=alpha, speed=speed)
            p = lockin_probe(targets, cfg, seed=i, k_list=k_list)
            if not p["separated"]:
                continue
            n += 1
            trapped += p["trapped_before"]
            for k in k_list:
                ret[k] += p["returned_by"][k]
    return {"n": n, "trapped": trapped / n, **{f"P{k}": ret[k] / n for k in k_list}}


class TestTheRiskIsReal:
    def test_prod_return_probability_is_the_documented_number(self):
        """Число, ради которого сценарий и написан. Ожидалось > 0.8,
        измерено около 0.5: ловушка активна и в прод-составе.

        Границы широкие намеренно — тест сторожит порядок величины, а не
        третий знак. Выход ВВЕРХ означает, что ловушку починили: обновить
        число здесь и в reports/СЧЁТ_КАНДИДАТА.md.
        """
        got = measure(PROD)
        assert 0.35 <= got["P15"] <= 0.65, (
            f"P(возврат) = {got['P15']:.3f}; ожидание тикета было > 0.8")

    def test_the_trap_is_absorbing_time_does_not_help(self):
        """Тройное K не добавляет возвратов: выйти из ловушки нечем.
        Именно это отличает её от обычной временной потери."""
        got = measure(PROD)
        assert got["P15"] == pytest.approx(got["P5"], abs=0.02)


class TestShadowsAreWhatCreatesIt:
    def test_without_shadows_the_trap_never_fires(self):
        """Тест обязан различать конструкции, иначе он декоративный."""
        assert measure(NO_SHADOWS)["trapped"] == 0.0

    def test_with_shadows_it_fires_on_a_large_share_of_runs(self):
        assert measure(PROD)["trapped"] > 0.2


class TestDeathRuleGivesNoEscape:
    def test_immortal_shadows_change_nothing(self):
        """Правило "смерть через 3 такта без питания" выхода из ловушки не
        даёт: теневой цели кормит сама цель, она детектится каждый такт.
        Единственный выход — не заводить теневой там, где цель просто не была
        выбрана.

        Допуск. Раньше здесь стояло abs=0.01 и утверждение "не меняет ни
        одного числа": при прежних размерах соседа (0.8-1.25 от цели) сосед
        никогда не покидал окно, ни один теневой не успевал умереть, и обе
        конфигурации давали БИТ В БИТ одно и то же. После перевода размеров
        соседа на измеренный диапазон (логравномерно 0.3-2.5) мелкий сосед
        иногда уходит из окна, смерть срабатывает, и числа расходятся на один
        прогон из ~75. Допуск 0.01 при выборке 75 лежал ниже шума выборки
        (одна ошибка = 0.013), то есть был не строгостью, а случайностью.
        Ставим порог по шуму и проверяем СУЩЕСТВО: смерть теневых на ловушку
        не влияет, в отличие от их отключения (см. соседний класс, там
        разница абсолютная).
        """
        a, b = measure(PROD), measure(IMMORTAL)
        assert a["P15"] == pytest.approx(b["P15"], abs=tolerance(a["n"]))
        assert a["trapped"] == pytest.approx(b["trapped"], abs=tolerance(a["n"]))

    def test_this_test_can_actually_see_a_broken_death_rule(self):
        """Страховка от того, что допуск выше проверяемого эффекта.

        Так уже случилось: допуск 3/n давал ровно 0.0400, а мутация
        SHADOW_MAX_MISSES=1 сдвигает P15 ровно на 0.0400 — мутант проходил, и
        соседний тест был декоративен. Здесь это проверяется ЯВНО, а не
        подразумевается: если различающая способность пропадёт, упадёт этот
        тест, а не молча пройдёт тот.
        """
        base = measure(IMMORTAL)
        broken = measure(dict(PROD, SHADOW_MAX_MISSES=1))
        gap = abs(broken["P15"] - base["P15"])
        tol = tolerance(base["n"])
        assert gap > tol, (
            f"сломанное правило смерти сдвигает P15 на {gap:.4f} при допуске {tol:.4f} — "
            "соседний тест такую мутацию не увидит")
