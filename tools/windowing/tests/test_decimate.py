"""decimate_by_timestamp — какие кадры становятся тактами.

Мутационный прогон убил 0 из 7: функция не была покрыта вовсе. При этом она
определяет ВСЮ временную ось прогона: ошибка здесь смещает dt, а вместе с ним
экстраполяцию, гейты и все метрики — и выглядит это как «трекер стал хуже», а
не как дефект прореживания.

Такт симулируется по ТАЙМСТАМПАМ, а не постоянным шагом кадров: у выделенных
проездов частота исходников разная, и «каждый N-й кадр» дал бы разный такт на
разном материале.
"""
import pytest

from track_run import decimate_by_timestamp


def frames(times):
    return [{"name": f"f{i:04d}", "timestamp_sec": t} for i, t in enumerate(times)]


def times(recs):
    return [r["timestamp_sec"] for r in recs]


class TestTickRateIsHonoured:
    def test_picks_every_nth_frame_when_it_divides_evenly(self):
        """12 fps, такт 3 Гц -> каждый четвёртый кадр."""
        f = frames([i / 12 for i in range(25)])
        got = decimate_by_timestamp(f, 3.0)
        assert times(got) == pytest.approx([i / 12 for i in range(0, 25, 4)])

    def test_actual_intervals_match_the_requested_period(self):
        f = frames([i / 12 for i in range(121)])
        for hz in (2.0, 3.0, 4.0, 6.0):
            got = times(decimate_by_timestamp(f, hz))
            gaps = [b - a for a, b in zip(got, got[1:])]
            assert max(gaps) == pytest.approx(1 / hz, abs=1 / 12), f"такт {hz} Гц"

    def test_higher_rate_gives_more_ticks(self):
        f = frames([i / 12 for i in range(121)])
        assert len(decimate_by_timestamp(f, 6.0)) > len(decimate_by_timestamp(f, 3.0))

    def test_rate_above_native_fps_cannot_invent_frames(self):
        """Просить 30 Гц у 12 fps материала бессмысленно — вернуться должны
        реальные кадры без задвоений, а не 30 записей в секунду."""
        f = frames([i / 12 for i in range(13)])
        got = decimate_by_timestamp(f, 30.0)
        assert len(got) <= len(f)
        assert len({r["name"] for r in got}) == len(got), "один и тот же кадр выдан дважды"


class TestIrregularTimestamps:
    def test_nearest_frame_is_chosen_not_the_next_one(self):
        """Кадры неравномерны (пропуски в исходнике): к целевому времени
        берётся БЛИЖАЙШИЙ, а не первый следующий, иначе такт систематически
        уползает вперёд."""
        f = frames([0.0, 0.30, 0.95, 1.40, 2.05])
        got = times(decimate_by_timestamp(f, 1.0))
        assert got == pytest.approx([0.0, 0.95, 2.05])

    def test_gap_in_source_does_not_duplicate_frames(self):
        """Дыра шире такта: несколько целевых времён приходятся на один и тот
        же кадр — он обязан войти один раз."""
        f = frames([0.0, 0.1, 5.0, 5.1])
        got = decimate_by_timestamp(f, 1.0)
        assert len(got) == len({r["name"] for r in got})

    def test_unsorted_input_still_yields_time_ordered_output(self):
        f = frames([0.0, 1.0, 0.5, 1.5])
        got = times(decimate_by_timestamp(sorted(f, key=lambda r: r["timestamp_sec"]), 2.0))
        assert got == sorted(got)


class TestBoundaries:
    def test_first_frame_is_always_the_first_tick(self):
        """Отсчёт идёт от таймстампа первого кадра, а не от нуля: у вырезанного
        проезда время начинается не с нуля."""
        f = frames([58.0 + i / 12 for i in range(25)])
        got = decimate_by_timestamp(f, 3.0)
        assert got[0]["timestamp_sec"] == pytest.approx(58.0)

    def test_last_frame_is_not_skipped_when_it_falls_on_a_tick(self):
        f = frames([i / 12 for i in range(25)])   # последний кадр ровно на 2.0 с
        got = decimate_by_timestamp(f, 3.0)
        assert got[-1]["timestamp_sec"] == pytest.approx(2.0)

    def test_empty_input(self):
        assert decimate_by_timestamp([], 3.0) == []

    def test_single_frame(self):
        f = frames([1.23])
        assert times(decimate_by_timestamp(f, 3.0)) == [1.23]

    def test_all_returned_records_come_from_the_input(self):
        f = frames([i / 12 for i in range(25)])
        got = decimate_by_timestamp(f, 3.0)
        assert all(r in f for r in got)
