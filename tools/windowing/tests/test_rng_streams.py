"""Независимость потоков случайности в dataset_gen (тикет "ночь", п.1.2).

Раньше позитивы, негативы и аугментации тянули из одного rng, поэтому
--neg-ratio сдвигал розыгрыш ПОЗИТИВОВ: при одинаковом seed и разном
neg-ratio 324 из 327 позитивных меток отличались. Значит сравнения "по доле
негативов" меняли не только негативы, и вывод о влиянии этой доли
недействителен.
"""
import os

import dataset_gen
from test_ignore_zone import _make_synthetic_frame_and_json


def _run(tmp_path, tag, neg_ratio, seed=0, augment=False):
    stem, jpg, js = _make_synthetic_frame_and_json(
        tmp_path, [(600, 400, 750, 550), (200, 300, 260, 420)], size=(1920, 1080))
    out = str(tmp_path / f"out_{tag}")
    report = {"frames": 0, "positives": 0, "negatives": 0, "size_samples": [],
              "boxes_total": 0, "boxes_zero_windows": 0, "windows_per_box_sum": 0,
              "ignore_boxes_total": 0}
    dataset_gen.process_frame(stem, jpg, js, out, "train", seed,
                              horizon_overrides={}, generate_negatives=True,
                              do_augment=augment, report=report, neg_ratio=neg_ratio)
    labels = os.path.join(out, "labels", "train")
    pos = {n: open(os.path.join(labels, n)).read()
           for n in os.listdir(labels) if "_pos" in n}
    neg = [n for n in os.listdir(labels) if "_neg" in n]
    return pos, neg


def test_neg_ratio_does_not_change_positives(tmp_path):
    pos1, neg1 = _run(tmp_path, "a", neg_ratio=1.0)
    pos2, neg2 = _run(tmp_path, "b", neg_ratio=3.0)
    assert pos1, "тест бесполезен, если позитивов не сгенерировано"
    assert set(pos1) == set(pos2), "изменился СОСТАВ позитивных окон"
    for name in pos1:
        assert pos1[name] == pos2[name], f"содержимое позитива {name} зависит от neg-ratio"
    assert len(neg2) > len(neg1), "негативы обязаны масштабироваться"


def test_augment_does_not_change_window_geometry(tmp_path):
    """Аугментация меняет пиксели, но не должна сдвигать РОЗЫГРЫШ окон:
    иначе прогон с --augment и без несравним по составу выборки."""
    pos_plain, _ = _run(tmp_path, "c", neg_ratio=1.0, augment=False)
    pos_aug, _ = _run(tmp_path, "d", neg_ratio=1.0, augment=True)
    assert set(pos_plain) == set(pos_aug)


def test_streams_are_independent_and_deterministic():
    a = dataset_gen.stream_rng(0, "frame_x", "positives").random()
    b = dataset_gen.stream_rng(0, "frame_x", "negatives").random()
    c = dataset_gen.stream_rng(0, "frame_x", "positives").random()
    assert a == c, "поток обязан быть воспроизводимым"
    assert a != b, "разные назначения обязаны давать разные потоки"


def test_stream_does_not_depend_on_frame_order():
    """Поток кадра выводится из его имени, а не из позиции в обходе —
    добавление кадра в начало папки не сдвигает розыгрыш остальных."""
    first = dataset_gen.stream_rng(7, "frame_b", "positives").random()
    assert dataset_gen.stream_rng(7, "frame_b", "positives").random() == first
    assert dataset_gen.stream_rng(7, "frame_a", "positives").random() != first
