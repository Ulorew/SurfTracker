"""Цвета визуализации прогона.

Повод: на проезде Primbee t115 разметка кончается за три секунды до конца
клипа, трекер вёл правильную цель — а рамка рисовалась ПУРПУРНОЙ, то есть
видео утверждало подмену, которой не было. Причина: цвет выбирался по
истинности (`C_CHOSEN if on_target else C_SWAP`), и `None` («проверить
нечем») попадал в ту же ветку, что `False` («чужая цель»).

Отсюда правило, которое и проверяется ниже: состояний истины ТРИ, и третье
нельзя красить ни как подтверждение, ни как опровержение. Визуализация — то,
по чему принимают решения глазами, и ложное утверждение в ней дороже, чем
отсутствие утверждения.
"""
import pytest

from track_viz import (C_CHOSEN, C_LOST, C_MISS, C_SWAP, C_TRACK, C_UNKNOWN,
                       status_colour)


class TestNoGroundTruthMakesNoClaim:
    def test_unknown_is_not_painted_as_a_swap(self):
        _, chosen, note = status_colour("tracking", 0, None)
        assert chosen != C_SWAP, "такт без истины объявлен подменой"
        assert chosen == C_UNKNOWN
        assert "ЧУЖАЯ" not in note

    def test_unknown_is_not_painted_as_confirmed_either(self):
        _, chosen, _ = status_colour("tracking", 0, None)
        assert chosen != C_CHOSEN, "такт без истины объявлен подтверждённой целью"

    def test_unknown_is_stated_in_the_label(self):
        """Молчаливое отсутствие проверки хуже явного: смотрящий должен
        видеть, что этот участок не подтверждён ничем."""
        _, _, note = status_colour("tracking", 0, None)
        assert "истины нет" in note

    def test_window_colour_stays_normal_without_ground_truth(self):
        """Само состояние петли известно и без разметки: ведём — зелёное."""
        win, _, _ = status_colour("tracking", 0, None)
        assert win == C_TRACK


class TestClaimsThatAreMade:
    def test_confirmed_target_is_cyan_and_green(self):
        win, chosen, note = status_colour("tracking", 0, True)
        assert (win, chosen, note) == (C_TRACK, C_CHOSEN, "")

    def test_wrong_target_is_magenta_everywhere(self):
        win, chosen, note = status_colour("tracking", 0, False)
        assert win == C_SWAP and chosen == C_SWAP
        assert "ЧУЖАЯ ЦЕЛЬ" in note

    def test_swap_outranks_a_miss_streak(self):
        """Пропуск — мелочь рядом с подменой: если цель чужая, окно обязано
        быть пурпурным, а не оранжевым."""
        win, _, _ = status_colour("tracking", 3, False)
        assert win == C_SWAP

    def test_loss_outranks_everything(self):
        for on_target in (True, False, None):
            win, _, _ = status_colour("lost", 5, on_target)
            assert win == C_LOST

    def test_miss_streak_without_a_verdict_is_orange(self):
        win, _, _ = status_colour("tracking", 2, True)
        assert win == C_MISS


class TestColoursAreDistinguishable:
    def test_all_four_states_have_different_colours(self):
        """Если два состояния совпадут по цвету, видео перестанет их
        различать, а тесты выше продолжат проходить."""
        colours = [C_TRACK, C_MISS, C_LOST, C_SWAP, C_CHOSEN, C_UNKNOWN]
        assert len(set(colours)) == len(colours)

    @pytest.mark.parametrize("a,b", [(C_UNKNOWN, C_SWAP), (C_UNKNOWN, C_CHOSEN),
                                      (C_SWAP, C_CHOSEN)])
    def test_key_pairs_differ_by_a_lot_not_a_shade(self, a, b):
        """Пурпур, голубой и белый должны различаться на глаз, а не оттенком:
        именно по ним смотрящий отличает подмену от нормы."""
        assert sum(abs(x - y) for x, y in zip(a, b)) > 150
