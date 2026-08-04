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


def _run(tmp_path, tag, neg_ratio, seed=0, augment=False, split="train"):
    stem, jpg, js = _make_synthetic_frame_and_json(
        tmp_path, [(600, 400, 750, 550), (200, 300, 260, 420)], size=(1920, 1080))
    out = str(tmp_path / f"out_{tag}")
    report = {"frames": 0, "positives": 0, "negatives": 0, "size_samples": [],
              "boxes_total": 0, "boxes_zero_windows": 0, "windows_per_box_sum": 0,
              "ignore_boxes_total": 0}
    dataset_gen.process_frame(stem, jpg, js, out, split, seed,
                              horizon_overrides={}, generate_negatives=True,
                              do_augment=augment, report=report, neg_ratio=neg_ratio)
    labels = os.path.join(out, "labels", split)
    pos = {n: open(os.path.join(labels, n)).read()
           for n in os.listdir(labels) if "_pos" in n}
    neg = [n for n in os.listdir(labels) if "_neg" in n]
    return pos, neg, report


def _images(tmp_path, tag, **kw):
    """Сами пиксели окон — по ним и видно, применилась аугментация или нет."""
    import cv2
    _run(tmp_path, tag, neg_ratio=1.0, **kw)
    split = kw.get("split", "train")
    d = os.path.join(str(tmp_path / f"out_{tag}"), "images", split)
    return {n: cv2.imread(os.path.join(d, n)) for n in sorted(os.listdir(d)) if "_pos" in n}


def test_neg_ratio_does_not_change_positives(tmp_path):
    pos1, neg1, _ = _run(tmp_path, "a", neg_ratio=1.0)
    pos2, neg2, _ = _run(tmp_path, "b", neg_ratio=3.0)
    assert pos1, "тест бесполезен, если позитивов не сгенерировано"
    assert set(pos1) == set(pos2), "изменился СОСТАВ позитивных окон"
    for name in pos1:
        assert pos1[name] == pos2[name], f"содержимое позитива {name} зависит от neg-ratio"
    assert len(neg2) > len(neg1), "негативы обязаны масштабироваться"


def test_augment_does_not_change_window_geometry(tmp_path):
    """Аугментация меняет пиксели, но не должна сдвигать РОЗЫГРЫШ окон:
    иначе прогон с --augment и без несравним по составу выборки."""
    pos_plain, _, _ = _run(tmp_path, "c", neg_ratio=1.0, augment=False)
    pos_aug, _, _ = _run(tmp_path, "d", neg_ratio=1.0, augment=True)
    assert set(pos_plain) == set(pos_aug)


class TestAugmentIsTrainOnly:
    """Тикет "ночь", п.1.4. Аугментировать измерительный сплит — значит мерить
    не ту картинку, на которой модель будет работать, и сравнивать прогоны с
    разной случайной порчей вместо одной и той же. Раньше --augment
    применялся к любому сплиту; не выстрелило только потому, что оконные
    датасеты состоят из одного train."""

    def test_val_split_is_not_augmented_even_when_asked(self, tmp_path):
        import numpy as np
        plain = _images(tmp_path, "v1", augment=False, split="val")
        asked = _images(tmp_path, "v2", augment=True, split="val")
        assert plain, "тест бесполезен, если окон не сгенерировано"
        assert set(plain) == set(asked)
        for n in plain:
            assert np.array_equal(plain[n], asked[n]), (
                f"окно val {n} изменено аугментацией — измерительный сплит испорчен")

    def test_train_split_IS_augmented(self, tmp_path):
        """Обратная сторона: если бы аугментация не применялась нигде, тест
        выше проходил бы сам собой и ничего не проверял."""
        import numpy as np
        plain = _images(tmp_path, "t1", augment=False)
        aug = _images(tmp_path, "t2", augment=True)
        assert any(not np.array_equal(plain[n], aug[n]) for n in plain), (
            "аугментация не изменила ни одного окна train — проверять нечего")

    def test_report_records_which_splits_were_augmented(self, tmp_path):
        _, _, rep_val = _run(tmp_path, "r1", neg_ratio=1.0, augment=True, split="val")
        _, _, rep_train = _run(tmp_path, "r2", neg_ratio=1.0, augment=True)
        assert rep_val.get("augmented_splits", set()) == set()
        assert rep_train["augmented_splits"] == {"train"}


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
