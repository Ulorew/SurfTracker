"""Регламент: прогон падает на старте, если построилось не то, что попросили
(тикет "ночь", блок 3).

Повод конкретный: с optimizer='auto' ultralytics молча игнорировал переданный
lr0 (в логе "ignoring lr0=..."), и целая серия сравнений "по lr" оказалась
сравнением одинаковых прогонов. Проверка обязана срабатывать НА СТАРТЕ —
результат, полученный за час не тем оптимизатором, дороже упавшего прогона.
"""
import pytest

from train_640 import effective_mismatches


class TestMatchingRunIsAccepted:
    def test_exact_match_has_no_complaints(self):
        assert effective_mismatches("AdamW(lr=0.0003, momentum=0.937)", "AdamW", 0.0003) == []

    def test_optimizer_name_is_case_insensitive(self):
        assert effective_mismatches("AdamW(lr=0.001, momentum=0.9)", "adamw", 0.001) == []

    def test_momentum_is_not_part_of_the_contract(self):
        """Момент ultralytics подбирает сам и это не то, что мы задаём —
        расхождение по нему прогон ронять не должно."""
        assert effective_mismatches("SGD(lr=0.01, momentum=0.8)", "SGD", 0.01) == []


class TestMismatchIsCaught:
    def test_wrong_lr_is_caught(self):
        bad = effective_mismatches("AdamW(lr=0.001, momentum=0.937)", "AdamW", 0.0003)
        assert bad and "lr0" in bad[0]

    def test_wrong_optimizer_is_caught(self):
        bad = effective_mismatches("SGD(lr=0.0003, momentum=0.937)", "AdamW", 0.0003)
        assert bad and "оптимизатор" in bad[0]

    def test_both_wrong_reports_both(self):
        bad = effective_mismatches("SGD(lr=0.01, momentum=0.9)", "AdamW", 0.0003)
        assert len(bad) == 2

    def test_the_historical_case_auto_overriding_lr(self):
        """Ровно то, что произошло: попросили lr0=0.0003, ultralytics
        построил свой 0.001 — и это никак не проявилось до самого конца."""
        bad = effective_mismatches("AdamW(lr=0.001, momentum=0.937)", "AdamW", 0.0003)
        assert bad, "случай, ради которого регламент и заведён, не ловится"

    def test_unparsable_description_is_a_mismatch_not_a_pass(self):
        """Молчаливое "не разобрал — значит всё хорошо" — худший исход:
        проверка выглядит работающей и ничего не проверяет."""
        assert effective_mismatches("непонятно что", "AdamW", 0.0003)
        assert effective_mismatches("", "AdamW", 0.0003)
        assert effective_mismatches(None, "AdamW", 0.0003)


class TestAutoIsExempt:
    def test_auto_optimizer_is_not_checked(self):
        """При optimizer='auto' пересчёт lr — заявленное поведение
        ultralytics, а не сбой. Но и сравнивать такие прогоны по lr нельзя,
        поэтому 'auto' в проекте не используется."""
        assert effective_mismatches("AdamW(lr=0.001, momentum=0.937)", "auto", 0.0003) == []


class TestTolerance:
    def test_float_noise_does_not_trip_the_check(self):
        assert effective_mismatches("AdamW(lr=0.00030000000000000003, momentum=0.9)",
                                     "AdamW", 0.0003, lr_tol=1e-9) == []

    def test_but_a_real_difference_does(self):
        assert effective_mismatches("AdamW(lr=0.00031, momentum=0.9)",
                                     "AdamW", 0.0003, lr_tol=1e-9)


def test_scientific_notation_is_parsed():
    assert effective_mismatches("AdamW(lr=3e-04, momentum=0.9)", "AdamW", 0.0003) == []
