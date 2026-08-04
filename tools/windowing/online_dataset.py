"""Кроп на лету — вариант D (тикет "патч v2", п.5).

Офлайн уже сделан заранее (online_variants.py): по 3-4 полнокадровые версии
на исходный кадр (чистая + tone/degrade/blur). Здесь — онлайн-часть: на
каждый вызов __getitem__ читается кадр целиком уже выбранной версии,
выбирается новое окно и флип поверх той же геометрии, что и офлайн-нарезка
(sample_window/negatives/crop/geometry), координаты боксов пересчитываются
той же clip_box_to_window, что dataset_gen.py — не дублируем ни один из этих
модулей.

Зерно: base_seed = --seed прогона (тикет: "зерно онлайн-семплирования = сид
прогона, в манифест"). Каждый вызов __getitem__ детерминирован по
(base_seed, index, счётчик_вызова_воркера) — не зависит от общего
изменяемого состояния RNG, поэтому не чувствителен к порядку диспетчеризации
между воркерами даталоадера.

Воспроизводимость проверена эмпирически в этой же сессии: два независимых
запуска с одинаковыми гиперпараметрами дали бит-идентичный хеш весов после
эпохи 1 (см. weight_hash в train_640.py) — то же ожидается и для этого RNG.
"""

import hashlib
import json
import os
import random

import cv2
import numpy as np
import torch

import config
from crop import crop
from dataset_gen import clip_box_to_window, load_frame_boxes, split_target_ignore, video_prefix
from geometry import resolve_placement
from negatives import sample_negatives
from sample_window import reachable_bin_indices, sample_window, window_for_bin

try:
    from ultralytics.data.dataset import YOLODataset
    from ultralytics.utils.instance import Instances
except ImportError:  # тесты без ultralytics в окружении
    YOLODataset = object
    Instances = None


def _local_rng(base_seed: int, index: int, call_count: int) -> random.Random:
    """Детерминированный RNG на конкретный вызов — без общего мутируемого
    состояния между воркерами (см. докстринг модуля)."""
    h = hashlib.sha256(f"{base_seed}:{index}:{call_count}".encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def build_frame_index(frames_dir, variants_dir, horizon_overrides, neg_ratio, enum_seed,
                       bin_first=True):
    """Перечисление кадров/боксов + слотов позитивов/негативов — тот же
    подсчёт, что в dataset_gen.py.process_frame, но без записи файлов:
    здесь только считаем, СКОЛЬКО слотов какого типа будет у эпохи, сама
    геометрия окна перевычисляется заново на каждый __getitem__.
    """
    variants_manifest = json.load(open(os.path.join(variants_dir, "variants_manifest.json")))
    variant_names = variants_manifest["variant_names"]
    by_bin = {}          # корзина -> [(кадр, индекс бокса)], кто её способен закрыть
    total_pos = 0        # сколько позитивов дала бы прежняя схема — держим объём

    enum_rng = random.Random(enum_seed)
    frames = {}
    slots = []  # (stem, kind, box_idx|None)

    jsons = sorted(f for f in os.listdir(frames_dir) if f.endswith(".json"))
    for jf in jsons:
        stem = jf[:-5]
        jpg_path = os.path.join(frames_dir, stem + ".jpg")
        if not os.path.exists(jpg_path):
            continue
        boxes, img_w, img_h = load_frame_boxes(os.path.join(frames_dir, jf))
        targets, ignore = split_target_ignore(boxes)

        variant_paths = {}
        for v in variant_names:
            p = os.path.join(variants_dir, f"{stem}__{v}.jpg")
            if os.path.exists(p) or os.path.islink(p):
                variant_paths[v] = p
        if not variant_paths:
            continue  # нет офлайн-версий для этого кадра — пропускаем целиком

        prefix = video_prefix(stem)
        horizon_frac = horizon_overrides.get(prefix, config.DEFAULT_HORIZON_Y_FRAC)
        frames[stem] = {
            "targets": targets, "ignore": ignore,
            "img_w": img_w, "img_h": img_h,
            "horizon_y": horizon_frac * img_h,
            "variant_paths": variant_paths,
            "variant_names": sorted(variant_paths.keys()),
        }

        n_pos = 0
        for bi, b in enumerate(targets):
            windows = sample_window(b, enum_rng)
            n_pos += len(windows)
            if bin_first:
                for i in reachable_bin_indices(max(b.w, b.h)):
                    by_bin.setdefault(i, []).append((stem, bi))
            else:
                # прежняя схема: слот на каждое окно, разыгранное ОТ БОКСА.
                # Оставлена, чтобы сравнение "доли против перекоса" гонялось
                # одним кодом, а не сверкой с git-историей.
                for _ in windows:
                    slots.append((stem, "pos", bi))

        total_pos += n_pos
        base = n_pos if n_pos > 0 else config.EMPTY_FRAME_NEGATIVE_COUNT
        neg_count = round(base * neg_ratio)
        for _ in range(neg_count):
            slots.append((stem, "neg", None))

    # Позитивы разыгрываются ОТ КОРЗИНЫ К БОКСУ (тикет "ночь", п.1.1): сначала
    # корзина по целевой доле SIZE_BINS, потом случайный бокс из тех, кто её
    # способен закрыть. Прямой порядок целевые доли не воспроизводит — см.
    # sample_window.reachable_bin_indices.
    avail = [i for i in by_bin if by_bin[i]] if bin_first else []
    if avail:
        weights = [max(config.SIZE_BINS[i][2], 0.0) for i in avail]
        if sum(weights) <= 0:
            weights = [1.0] * len(avail)
        for _ in range(total_pos):
            bi = enum_rng.choices(avail, weights=weights, k=1)[0]
            stem, box_idx = enum_rng.choice(by_bin[bi])
            slots.append((stem, "pos", (box_idx, bi)))

    return frames, slots


class OnlineCropYOLODataset(YOLODataset):
    """YOLODataset, у которого __getitem__ каждый раз вырезает новое окно из
    полнокадровой (офлайн-фильтрованной) версии — вместо чтения статичного
    заранее нарезанного файла. Наследуем build_transforms/update_labels_info/
    collate_fn от YOLODataset без изменений (та же online-аугментация
    ultralytics поверх, что и раньше); НЕ вызываем BaseDataset.__init__ —
    он рассчитан на статичный список файлов с кешем, здесь его семантика не
    подходит."""

    def __init__(self, frames_dir, variants_dir, data, imgsz, hyp, augment,
                 single_cls, seed, neg_ratio=1.0, horizon_overrides=None,
                 stride=32, prefix="", bin_first=True):
        self.data = data
        self.use_segments = False
        self.use_keypoints = False
        self.use_obb = False
        self.imgsz = imgsz
        self.augment = augment
        self.single_cls = single_cls
        self.prefix = prefix
        self.fraction = 1.0
        self.channels = 3
        self.rect = False  # кроп на лету несовместим с rect-батчами
        self.stride = stride
        self.base_seed = seed
        # атрибуты, которые ultralytics читает при ПОСТРОЕНИИ transform-пайплайна
        # (даже если сама Mosaic/CopyPaste никогда не вызовется при p=0) —
        # BaseDataset их обычно выставляет в своём __init__, который мы here
        # намеренно не вызываем (он рассчитан на статичный список файлов).
        self.cache = False
        self.buffer = []
        self.max_buffer_length = 1
        # DetectionTrainer.auto_batch() (--batch -1) читает dataset.labels
        # для оценки объектов/кадр — статичного списка меток у нас нет (они
        # считаются заново на каждый __getitem__), даём правдоподобную
        # оценку по типичной плотности окна вместо реального скана.
        self.labels = [{"cls": np.zeros((2, 1), dtype=np.float32)}]

        self.frames, self.slots = build_frame_index(
            frames_dir, variants_dir, horizon_overrides or {}, neg_ratio, enum_seed=seed,
            bin_first=bin_first)
        if not self.slots:
            raise ValueError(f"online dataset: 0 слотов из {frames_dir} / {variants_dir}")

        self.im_files = [f"{stem}::{kind}::{bi}" for stem, kind, bi in self.slots]  # для логов  # для логов/прогресс-бара
        self.ni = len(self.slots)
        self._call_count = 0

        self.transforms = self.build_transforms(hyp)

    def __len__(self):
        return len(self.slots)

    def _draw_positive(self, stem, box_idx, rng, bin_idx=None):
        f = self.frames[stem]
        box = f["targets"][box_idx]
        if bin_idx is not None:
            sq = window_for_bin(box, bin_idx, rng)
            if sq is not None:
                return sq, f
        windows = sample_window(box, rng)
        if not windows:
            # редкий случай (см. dataset_gen.py report boxes_zero_windows) —
            # другая попытка с новым rng, затем — как негатив на этом кадре
            windows = sample_window(box, _local_rng(self.base_seed, box_idx, rng.randint(0, 2**31 - 1)))
        if not windows:
            return self._draw_negative(stem, rng)
        square = rng.choice(windows)
        return square, f

    def _draw_negative(self, stem, rng):
        f = self.frames[stem]
        boxes = f["targets"] + f["ignore"]
        squares = sample_negatives(f["img_w"], f["img_h"], boxes, 1, rng, horizon_y=f["horizon_y"])
        if squares:
            return squares[0], f
        # не нашли непересекающееся место (плотный кадр) — редкий сбой
        # коллизии; не роняем воркер, просто ставим окно естественного
        # размера в случайной точке (тот же компромисс, что в negatives.py:
        # лучше недодать/промахнуться на одном сэмпле, чем зависнуть).
        from geometry import Square
        side = rng.uniform(config.SIZE_BINS[0][0], config.SIZE_BINS[-1][1] if config.SIZE_BINS[-1][1] != float("inf") else 400)
        cx = rng.uniform(0, f["img_w"])
        cy = rng.uniform(0, f["img_h"])
        return Square(cx=cx, cy=cy, side=side), f

    def get_image_and_label(self, index):
        stem, kind, payload = self.slots[index]
        box_idx = payload[0] if isinstance(payload, tuple) else payload
        bin_idx = payload[1] if isinstance(payload, tuple) else None
        self._call_count += 1
        rng = _local_rng(self.base_seed, index, self._call_count)

        f = self.frames[stem]
        if kind == "pos":
            square, f = self._draw_positive(stem, box_idx, rng, bin_idx)
        else:
            square, f = self._draw_negative(stem, rng)

        variant = rng.choice(f["variant_names"])
        variant_path = f["variant_paths"][variant]
        big = cv2.imread(variant_path)
        if big is None:
            raise FileNotFoundError(variant_path)

        placement = resolve_placement(square, f["img_w"], f["img_h"], config.WINDOW_SIZE)
        img = crop(big, square)

        yolo_boxes = []
        for b in f["targets"]:  # ignore-боксы никогда не попадают в разметку (тот же контракт, что dataset_gen.py)
            local = clip_box_to_window(b, placement, config.WINDOW_SIZE)
            if local is not None:
                yolo_boxes.append(local)

        if rng.random() < config.HFLIP_PROB:
            img = np.ascontiguousarray(img[:, ::-1])
            yolo_boxes = [(1.0 - cx, cy, w, h) for (cx, cy, w, h) in yolo_boxes]

        if yolo_boxes:
            bboxes = np.array(yolo_boxes, dtype=np.float32)
            cls = np.zeros((len(yolo_boxes), 1), dtype=np.float32)
        else:
            bboxes = np.zeros((0, 4), dtype=np.float32)
            cls = np.zeros((0, 1), dtype=np.float32)

        label = {
            "im_file": f"{stem}::{kind}::{variant}::{index}",
            "cls": cls,
            "bboxes": bboxes,
            "segments": [],
            "keypoints": None,
            "bbox_format": "xywh",
            "normalized": True,
            "ori_shape": (config.WINDOW_SIZE, config.WINDOW_SIZE),
            "resized_shape": (config.WINDOW_SIZE, config.WINDOW_SIZE),
            "ratio_pad": (1.0, 1.0),
            "img": img,
        }
        return self.update_labels_info(label)

    def __getitem__(self, index):
        return self.transforms(self.get_image_and_label(index))
