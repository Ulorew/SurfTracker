"""Флаг, который включён, обязан на что-то влиять.

ЧТО ЭТИМ ЗАКРЫВАЕТСЯ. 5 августа правило «допуск по положению берётся из
предсказанного размера цели» реализовали в четырёх местах и починили три.
Непочиненным осталось единственное, которое в боевой конфигурации
исполняется. Флаг `MAHA_R_FROM_PREDICTED_SIZE` при этом стоял в True, и по
конфигу выглядело, что решение принято повсеместно.

Ни тесты, ни стенды, ни мутации поймать этого не могли: все они отвечают на
вопрос «верно ли то, что написано», и ни один — на вопрос «а это исполняется?».
Тринадцать дней прод работал по правилу, которое считалось отменённым.

Правило, выведенное из этого случая и проверяемое здесь:

    ЕСЛИ ФЛАГ ВКЛЮЧЁН, ХОТЯ БЫ ОДИН ЕГО ЧИТАТЕЛЬ ОБЯЗАН ИСПОЛНЯТЬСЯ.

Включённый флаг, все читатели которого мертвы, — это не «запас на будущее», а
ложное свидетельство: он документирует решение, которого в работающем коде
нет. Ровно этим и был MAHA_R_FROM_PREDICTED_SIZE тринадцать дней.
"""
import ast
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOWING = os.path.dirname(HERE)
sys.path.insert(0, WINDOWING)

import prod_reach  # noqa: E402
import tracking_config as tcfg  # noqa: E402


# Флаги, у которых живых читателей нет ЗАКОННО. Каждая запись — с причиной,
# и пустая причина не принимается: смысл списка в том, чтобы исключение
# приходилось объяснять, а не молча дописывать.
ОПРАВДАННЫЕ = {
    "ENABLE_MAHALANOBIS_GATE":
        "читается методом uses_mahalanobis_gate, который живой; "
        "сам флаг в теле функции не упоминается",
}


@pytest.fixture(scope="module")
def достижимость():
    got = prod_reach.reached()
    declared = prod_reach.declared_functions()
    live, dead = set(), set()
    for (m, qual) in declared:
        short = qual.split(".")[-1]
        (live if (m, short) in got else dead).add((m, qual))
    return live, dead


def читатели_флагов():
    """-> {имя флага: {(модуль, функция), ...}}

    Через ast, а не построчным grep: нужно знать, в ТЕЛЕ КАКОЙ функции стоит
    обращение, а не на какой строке файла.
    """
    out = {}
    for m in prod_reach.MODULES:
        path = os.path.join(WINDOWING, m)
        if not os.path.exists(path):
            continue
        src = open(path).read()
        tree = ast.parse(src, filename=path)

        def обойти(node, prefix, внутри):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    обойти(child, prefix + child.name + ".", внутри)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    обойти(child, prefix + child.name + ".",
                           (m, prefix + child.name))
                else:
                    if внутри is not None:
                        for имя in имена_флагов(child):
                            out.setdefault(имя, set()).add(внутри)
                    обойти(child, prefix, внутри)

        обойти(tree, "", None)
    return out


def имена_флагов(node):
    """Имена вида cfg.ИМЯ и getattr(cfg, "ИМЯ", ...) внутри узла."""
    найдено = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and re.fullmatch(r"[A-Z][A-Z0-9_]+", n.attr):
            найдено.add(n.attr)
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "getattr" and len(n.args) >= 2
                and isinstance(n.args[1], ast.Constant)
                and isinstance(n.args[1].value, str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]+", n.args[1].value)):
            найдено.add(n.args[1].value)
    return найдено


def включённые_флаги():
    """Булевы настройки конфига, стоящие в True."""
    return {k for k in dir(tcfg)
            if re.fullmatch(r"[A-Z][A-Z0-9_]+", k)
            and isinstance(getattr(tcfg, k), bool)
            and getattr(tcfg, k) is True}


class TestВключённыеФлагиРаботают:

    def test_у_каждого_включённого_флага_есть_живой_читатель(self, достижимость):
        live, dead = достижимость
        читатели = читатели_флагов()
        беда = []
        for флаг in sorted(включённые_флаги()):
            если_читают = читатели.get(флаг, set())
            if not если_читают:
                continue                      # модули петли его не читают вовсе
            if если_читают & live:
                continue
            if флаг in ОПРАВДАННЫЕ and ОПРАВДАННЫЕ[флаг].strip():
                continue
            беда.append(
                f"{флаг} = True, но все его читатели мертвы: "
                + ", ".join(f"{m}:{q}" for m, q in sorted(если_читают)))
        assert not беда, (
            "Включённый флаг обязан на что-то влиять. Иначе он документирует "
            "решение, которого в работающем коде нет — ровно как "
            "MAHA_R_FROM_PREDICTED_SIZE с 5 по 18 августа:\n  "
            + "\n  ".join(беда))


class TestПравилоРеализованоОдинРаз:
    """Вторая половина того же урока. Даже правильный тест выше не спас бы,
    будь у правила две реализации: одна живая и починенная, другая живая и
    нет. Поэтому правило обязано существовать в единственном экземпляре."""

    def test_допуск_по_положению_считается_в_одном_месте(self):
        места = []
        for m in prod_reach.MODULES:
            path = os.path.join(WINDOWING, m)
            if not os.path.exists(path):
                continue
            for i, line in enumerate(open(path), 1):
                if "MAHA_R_FROM_PREDICTED_SIZE" in line:
                    места.append(f"{m}:{i}")
        assert len(места) == 1, (
            "Правило «допуск по положению — из предсказанного размера» обязано "
            "иметь ровно одну реализацию (_match_r_size). Иначе следующая "
            "правка снова достанется не всем: 5 августа их было четыре, "
            "починили три, а пропустили единственную живую.\n  "
            + "\n  ".join(места))

    def test_единственная_реализация_исполняется_в_проде(self, достижимость):
        live, _ = достижимость
        assert ("track_kalman.py", "KalmanAngularFilter._match_r_size") in live, \
            "правило перестало исполняться в боевой конфигурации — значит " \
            "боевой отбор снова идёт мимо него"
