#!/usr/bin/env python3
"""Обучение в боевом режиме — 640, окна из dataset_gen.py (тикет, п.6).

Переиспользует проверенный рецепт гиперпараметров Datasets/dataset_v1/train.py
(single_cls, degrees=3, flipud=0, hsv_h=0.005, close_mosaic=20, patience=30) —
тот же набор, другие данные/разрешение/модель. Автоматическая пост-обработка
train.py (val на surf_dist.yaml/surf_dist_full.yaml + eval_size_bins) сюда не
перенесена: те yaml привязаны к датасету цельных кадров (dataset_v1) и не
имеют смысла для нарезанных окон — метрики для окон считает eval_640.py.

    python train_640.py --data windows_v1/surf.yaml --model yolo11s.pt \
        --project runs/surf_640 --name v1
"""

import argparse
import datetime
import hashlib
import json
import os
import re

DEF_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")


def config_hash(config_path: str = DEF_CONFIG_PATH) -> str:
    return hashlib.sha256(open(config_path, "rb").read()).hexdigest()[:16]


def frame_list_hash(data_yaml_path: str) -> "str | None":
    """Хеш отсортированного списка имён train-картинок — фиксирует ровно
    то, на чём училась модель (тикет "подготовка ночи", патч 5)."""
    import yaml
    cfg = yaml.safe_load(open(data_yaml_path))
    base = cfg.get("path", ".")
    # относительный path: в yaml задан ОТНОСИТЕЛЬНО САМОГО yaml, а не cwd.
    # Пока резолвили от cwd, поле молча выходило null во всех манифестах —
    # провенанс, ради которого патч и делался, не работал ни разу.
    if not os.path.isabs(base):
        base = os.path.join(os.path.dirname(os.path.abspath(data_yaml_path)), base)
    train_rel = cfg.get("train", "images/train")
    train_dir = train_rel if os.path.isabs(train_rel) else os.path.join(base, train_rel)
    if not os.path.isdir(train_dir):
        print(f"WARNING: frame_list_hash: не нашёл train-каталог {train_dir} — поле останется null")
        return None
    names = sorted(os.listdir(train_dir))
    return hashlib.sha256("\n".join(names).encode()).hexdigest()[:16]


def labeling_version(data_yaml_path: str):
    """Читает report.json рядом с датасетом (пишет dataset_gen.py) и
    возвращает его поле labeling_version, если есть — иначе None."""
    dataset_dir = os.path.dirname(os.path.abspath(data_yaml_path))
    report_path = os.path.join(dataset_dir, "report.json")
    if not os.path.exists(report_path):
        return None
    return json.load(open(report_path)).get("labeling_version")


def describe_optimizer(optimizer) -> str:
    """Реальная строка оптимизатора из живого torch-объекта после
    model.train(), а не из args.yaml (тикет "патч v2", п.1): args.yaml
    фиксирует что попросили, не что сработало — при optimizer='auto'
    ultralytics пересчитывает lr/momentum сам и печатает только в лог, не
    в args.yaml (см. "ignoring lr0=..." в истории этой сессии)."""
    pg = optimizer.param_groups[0]
    lr = pg.get("lr")
    if "momentum" in pg:
        momentum = pg["momentum"]
    elif "betas" in pg:
        momentum = pg["betas"][0]
    else:
        momentum = None
    return f"{type(optimizer).__name__}(lr={lr}, momentum={momentum})"


def effective_mismatches(desc: str, want_optimizer: str, want_lr0: float,
                          lr_tol: float = 1e-9) -> "list[str]":
    """Расхождения между тем, что попросили, и тем, что реально построилось.

    Регламент (тикет "ночь", блок 3): прогон, у которого effective расходится
    с заявкой, обязан падать НА СТАРТЕ, а не давать через час результат, из
    которого потом делают выводы. Так уже случилось: с optimizer='auto'
    ultralytics молча игнорировал переданный lr0 ("ignoring lr0=..." в логе),
    и целая серия сравнений по lr оказалась сравнением одинаковых прогонов.

    want_optimizer='auto' проверку имени и lr снимает: там пересчёт —
    заявленное поведение ultralytics, а не расхождение. Но такой прогон и
    нельзя сравнивать по lr, поэтому 'auto' в этом проекте не используется.
    """
    out = []
    if want_optimizer and want_optimizer.lower() == "auto":
        return out
    m = re.match(r"^([A-Za-z]+)\(lr=([^,]+)", desc or "")
    if not m:
        return [f"не удалось разобрать строку оптимизатора: {desc!r}"]
    got_name, got_lr = m.group(1), float(m.group(2))
    if want_optimizer and got_name.lower() != want_optimizer.lower():
        out.append(f"оптимизатор: просили {want_optimizer}, построился {got_name}")
    if want_lr0 is not None and abs(got_lr - want_lr0) > lr_tol:
        out.append(f"lr0: просили {want_lr0}, построился {got_lr}")
    return out


def weight_hash(module) -> str:
    """SHA256 по всем float-параметрам модели (тикет "патч v2", п.1) —
    сравнение весов после эпохи 1 между прогонами ловит случаи, когда
    CLI-параметр был принят, но не подействовал на само обучение (то, что
    args.yaml и даже 'эффективный' лог-текст не поймают, если сама
    оптимизация в итоге не изменилась)."""
    h = hashlib.sha256()
    sd = module.state_dict()
    for k in sorted(sd.keys()):
        t = sd[k]
        if t.is_floating_point():
            h.update(t.detach().float().cpu().numpy().tobytes())
    return h.hexdigest()[:16]


def param_counts(module):
    total = sum(p.numel() for p in module.parameters())
    trainable = sum(p.numel() for p in module.parameters() if p.requires_grad)
    return trainable, total


def make_online_trainer_class(frames_dir: str, variants_dir: str, neg_ratio: float,
                               bin_first: bool = True):
    """Фабрика, а не готовый класс на модульном уровне: ultralytics строит
    trainer сам (model.train(trainer=Cls, ...) -> Cls(overrides=..)),
    поэтому параметры online-датасета (пути, neg_ratio) должны попасть внутрь
    через замыкание, а не через cfg-словарь ultralytics (он валидирует
    известные ключи и не пропустит наши произвольные --online-* аргументы).
    """
    from ultralytics.models.yolo.detect import DetectionTrainer
    from ultralytics.utils import LOGGER
    from ultralytics.utils.torch_utils import unwrap_model

    from online_dataset import OnlineCropYOLODataset

    class OnlineCropTrainer(DetectionTrainer):
        def build_dataset(self, img_path: str, mode: str = "train", batch=None):
            if mode != "train":
                # val — без изменений, статичный сплит из --data yaml
                # (тикет "патч v2", п.5: "Валидация без изменений").
                return super().build_dataset(img_path, mode, batch)
            gs = max(int(unwrap_model(self.model).stride.max()), 32)
            return OnlineCropYOLODataset(
                frames_dir=frames_dir, variants_dir=variants_dir,
                data=self.data, imgsz=self.args.imgsz, hyp=self.args,
                augment=True, single_cls=self.args.single_cls,
                seed=self.args.seed, neg_ratio=neg_ratio, stride=gs, bin_first=bin_first,
                prefix="online-train: ",
            )

        def plot_training_labels(self):
            # DetectionTrainer.plot_training_labels() сканирует
            # train_loader.dataset.labels целиком, ожидая статичный список
            # с реальными bboxes — у online-датасета метки считаются заново
            # на каждый __getitem__, статичного списка нет (self.labels —
            # плейсхолдер только для auto_batch). labels.jpg по плейсхолдеру
            # был бы просто неверным графиком, а не диагностикой — пропускаем.
            LOGGER.info("online-crop: labels.jpg пропущен (метки не статичны, см. train_640.py)")

    return OnlineCropTrainer


def absolutize(yaml_path: str) -> str:
    """Ultralytics резолвит `path:` от cwd — подставляем абсолютный путь."""
    import yaml
    cfg = yaml.safe_load(open(yaml_path))
    cfg["path"] = os.path.dirname(os.path.abspath(yaml_path))
    out = os.path.splitext(yaml_path)[0] + ".abs.yaml"
    yaml.safe_dump(cfg, open(out, "w"), allow_unicode=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="yaml нарезанного датасета (dataset_gen.py)")
    ap.add_argument("--allow-effective-mismatch", action="store_true",
                     help="не падать, если реально построенный оптимизатор/lr расходятся "
                          "с заявленными (по умолчанию прогон останавливается на старте)")
    ap.add_argument("--model", default="yolo11s.pt")
    ap.add_argument("--project", default="runs/surf_640")
    ap.add_argument("--name", default="v1")
    ap.add_argument("--imgsz", type=int, default=640)  # боевой режим, не предразметка
    ap.add_argument("--epochs", type=int, default=150)
    ap.add_argument("--batch", type=float, default=-1)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--mosaic", type=float, default=1.0,
                     help="0 для окон (мозаика ломает масштаб/центровку, ради которых нарезали); "
                          "оставить по умолчанию для целых кадров")
    ap.add_argument("--fliplr", type=float, default=0.5,
                     help="0, если входные окна уже прошли свой hflip в augment.py "
                          "(не дублировать с встроенной аугментацией ultralytics)")
    ap.add_argument("--lr0", type=float, default=0.01, help="ultralytics default 0.01")
    ap.add_argument("--optimizer", default="AdamW",
                     help="ultralytics default 'auto' IGNORES любой переданный --lr0/--momentum "
                          "(build_optimizer пересчитывает их из nc/iterations и печатает "
                          "'ignoring lr0=...') — весь предыдущий рецепт (dataset_v1/train.py тоже) "
                          "использовал 'auto', поэтому явный --lr0 в более ранних прогонах мог не "
                          "иметь эффекта; явное имя оптимизатора обязательно, чтобы --lr0 применился")
    ap.add_argument("--freeze", type=int, default=None,
                     help="сколько первых слоёв заморозить (хребет); None = не морозить")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--save-period", type=int, default=-1,
                     help="сохранять веса каждые N эпох (weights/epochN.pt); -1 = не сохранять")
    ap.add_argument("--copy-paste", type=float, default=0.0, help="ultralytics default 0.0")
    ap.add_argument("--patience", type=int, default=30,
                     help="0 отключает раннюю остановку (тикет 'подготовка ночи', патч 6 — "
                          "фиксированные epochs, отбор чекпойнта по кривой оценки, не по best.pt)")
    ap.add_argument("--config-path", default=DEF_CONFIG_PATH,
                     help="какой config.py хешировать (записывается рядом с весами)")
    ap.add_argument("--baseline-run", default=None,
                     help="путь к run_dir другого прогона (с windowing_config.json) — если "
                          "задан, effective.epoch1_weight_hash сравнивается с его хешем "
                          "автоматически (тикет 'патч v2', п.1: ловит 'параметр не применился')")
    ap.add_argument("--online-crop", action="store_true",
                     help="тикет 'патч v2', п.5 (вариант D): train-сплит кропится на лету из "
                          "офлайн-фильтрованных полнокадровых версий (online_variants.py), вместо "
                          "чтения статичной нарезки из --data. val — как обычно, из --data yaml")
    ap.add_argument("--online-frames-dir", default=None,
                     help="папка с исходными кадрами (*.jpg/*.json) для --online-crop")
    ap.add_argument("--online-variants-dir", default=None,
                     help="папка с офлайн-версиями от online_variants.py (variants_manifest.json)")
    ap.add_argument("--size-bins-floor", type=float, default=None,
                     help="нижняя граница нижней корзины config.SIZE_BINS для ОНЛАЙН-семплера; "
                          "без этого floor из --data yaml до него не доходит (yaml используется "
                          "только для val), и прогон молча идёт с дефолтом config.py")
    ap.add_argument("--legacy-sampler", action="store_true",
                     help="прежний розыгрыш окон ОТ БОКСА (целевые доли SIZE_BINS не работают) — "
                          "для честного сравнения со старой сборкой одним кодом")
    ap.add_argument("--online-neg-ratio", type=float, default=1.0,
                     help="тот же смысл, что --neg-ratio в dataset_gen.py, но для online-датасета")
    args, _ = ap.parse_known_args()

    if args.online_crop and not (args.online_frames_dir and args.online_variants_dir):
        raise SystemExit("--online-crop требует --online-frames-dir и --online-variants-dir")

    if args.size_bins_floor is not None:
        import config as _cfg
        lo, hi, frac = _cfg.SIZE_BINS[0]
        _cfg.SIZE_BINS[0] = (args.size_bins_floor, hi, frac)
        print(f"SIZE_BINS[0] нижняя граница -> {args.size_bins_floor}")

    from ultralytics import YOLO

    data_abs = absolutize(args.data)
    run_dir = os.path.join(args.project, args.name)
    last = os.path.join(run_dir, "weights", "last.pt")

    epoch1_state = {}

    def _capture_epoch1_hash(trainer):
        # trainer.epoch — 0-индексный; 0 здесь значит "эпоха 1 только что
        # завершилась" (см. engine/trainer.py: self.epoch = epoch стартует
        # с self.start_epoch=0 для свежего прогона, on_train_epoch_end
        # стреляет в конце итерации до валидации). Для --resume с
        # start_epoch>0 колбэк на этом хуке не сработает — не наш случай
        # в этом патче (--resume нигде не используется).
        if trainer.epoch == 0 and "hash" not in epoch1_state:
            epoch1_state["hash"] = weight_hash(trainer.model)

    def _capture_start_optimizer(trainer):
        # on_train_start стреляет сразу после _setup_train() (который
        # строит self.optimizer) и ДО первого scheduler.step() — та же
        # точка, где сам ultralytics печатает "optimizer: ...(lr=..,
        # momentum=..)" в лог старта. Если читать trainer.optimizer уже
        # ПОСЛЕ model.train() (как было раньше), там окажется lr после
        # затухания по расписанию за все эпохи, а не стартовый — не то,
        # чем "подтверждается в логе старта" (тикет "патч v2", п.1).
        if "start" not in epoch1_state:
            epoch1_state["start"] = describe_optimizer(trainer.optimizer)
            bad = effective_mismatches(epoch1_state["start"], args.optimizer, args.lr0)
            if bad and not args.allow_effective_mismatch:
                raise SystemExit(
                    "ПРОГОН ОСТАНОВЛЕН НА СТАРТЕ: обучение построилось не тем, что попросили.\n  "
                    + "\n  ".join(bad)
                    + f"\n  (строка оптимизатора: {epoch1_state['start']})"
                    + "\n  Если расхождение осознанное — --allow-effective-mismatch.")

    if args.resume and os.path.exists(last):
        # см. dataset_v1/train.py: save_dir и data — явно, иначе resume
        # тихо возьмёт путь датасета из чекпойнта (площадка могла смениться).
        model = YOLO(last)
        model.add_callback("on_train_epoch_end", _capture_epoch1_hash)
        model.add_callback("on_train_start", _capture_start_optimizer)
        model.train(resume=True, save_dir=run_dir, data=data_abs)
    else:
        model = YOLO(args.model)
        model.add_callback("on_train_epoch_end", _capture_epoch1_hash)
        model.add_callback("on_train_start", _capture_start_optimizer)
        train_kwargs = dict(
            data=data_abs,
            imgsz=args.imgsz, batch=args.batch,
            epochs=args.epochs, patience=args.patience,
            single_cls=True,
            degrees=3, flipud=0, hsv_h=0.005,
            mosaic=args.mosaic, fliplr=args.fliplr,
            lr0=args.lr0, optimizer=args.optimizer, seed=args.seed, save_period=args.save_period,
            copy_paste=args.copy_paste,
            close_mosaic=20,
            project=args.project, name=args.name, exist_ok=True,
        )
        if args.freeze is not None:
            train_kwargs["freeze"] = args.freeze
        if args.online_crop:
            trainer_cls = make_online_trainer_class(
                args.online_frames_dir, args.online_variants_dir, args.online_neg_ratio,
                bin_first=not args.legacy_sampler)
            model.train(trainer=trainer_cls, **train_kwargs)
        else:
            model.train(**train_kwargs)

    # Реальный save_dir может отличаться от project/name (ultralytics в
    # некоторых версиях подставляет runs/detect/<project>/<name>) — берём
    # путь у самого trainer'а, а не угадываем (та же ловушка, что и в v1/v2).
    run_dir = str(model.trainer.save_dir)

    # Полные аргументы, реально применённые ultralytics, — уже лежат рядом
    # в args.yaml; переносим их сюда же, чтобы не открывать два файла
    # (тикет "подготовка ночи", патч 5).
    ultralytics_args = None
    args_yaml_path = os.path.join(run_dir, "args.yaml")
    if os.path.exists(args_yaml_path):
        import yaml
        ultralytics_args = yaml.safe_load(open(args_yaml_path))

    trainable_params, total_params = param_counts(model.trainer.model)

    effective = {
        # то же, что печатает сам ultralytics в логе старта (сразу после
        # построения оптимизатора, до затухания lr по расписанию) — НЕ
        # значение trainer.optimizer после всех эпох (оно уже другое из-за
        # scheduler.step()).
        "optimizer": epoch1_state.get("start") or describe_optimizer(model.trainer.optimizer),
        "trainable_params": trainable_params,
        "total_params": total_params,
        "epoch1_weight_hash": epoch1_state.get("hash"),
    }
    if args.baseline_run:
        baseline_meta_path = args.baseline_run
        if os.path.isdir(baseline_meta_path):
            baseline_meta_path = os.path.join(baseline_meta_path, "windowing_config.json")
        # ultralytics кладёт прогон в runs/detect/<project>/<name>, а не в
        # <project>/<name> — та же ловушка, что дважды ловила нас в v1/v2.
        # Без этого fallback путь baseline просто не находился.
        if not os.path.exists(baseline_meta_path) and not os.path.isabs(args.baseline_run):
            alt = os.path.join("runs", "detect", args.baseline_run)
            if os.path.isdir(alt):
                alt = os.path.join(alt, "windowing_config.json")
            if os.path.exists(alt):
                baseline_meta_path = alt

        baseline_hash = None
        if os.path.exists(baseline_meta_path):
            baseline_meta = json.load(open(baseline_meta_path))
            baseline_hash = (baseline_meta.get("effective") or {}).get("epoch1_weight_hash")
        effective["baseline_run"] = os.path.abspath(baseline_meta_path)
        effective["baseline_epoch1_weight_hash"] = baseline_hash
        # None, а НЕ False, когда сравнивать было не с чем: False читается как
        # "проверили, всё в порядке" — то есть отчёт врал ровно в том случае,
        # ради которого эта проверка и заводилась.
        effective["epoch1_hash_matches_baseline"] = (
            None if baseline_hash is None else baseline_hash == effective["epoch1_weight_hash"]
        )
        if baseline_hash is None:
            print(f"WARNING: baseline {args.baseline_run} не найден или без epoch1-хеша "
                  f"({baseline_meta_path}) — сравнение НЕ выполнено")
        if effective["epoch1_hash_matches_baseline"]:
            print(f"WARNING: epoch1 weight hash matches baseline {args.baseline_run} — "
                  f"varied parameter likely had no effect")

    meta = {
        "date": datetime.datetime.now().isoformat(),
        "config_hash": config_hash(args.config_path),
        "config_path": os.path.abspath(args.config_path),
        # при --online-crop train-сплит из --data yaml НЕ используется
        # (только val) — frame_list_hash тут был бы про неиспользуемую
        # папку, поэтому считаем его только в статичном режиме.
        "frame_list_hash": None if args.online_crop else frame_list_hash(args.data),
        "labeling_version": labeling_version(args.data),
        "data_yaml": os.path.abspath(args.data),
        "model": args.model,
        "imgsz": args.imgsz,
        "epochs": args.epochs,
        "patience": args.patience,
        "mosaic": args.mosaic,
        "fliplr": args.fliplr,
        "lr0": args.lr0,
        "optimizer": args.optimizer,
        "freeze": args.freeze,
        "seed": args.seed,
        "save_period": args.save_period,
        "copy_paste": args.copy_paste,
        "online_crop": args.online_crop,
        "online_frames_dir": os.path.abspath(args.online_frames_dir) if args.online_frames_dir else None,
        "online_variants_dir": os.path.abspath(args.online_variants_dir) if args.online_variants_dir else None,
        "online_neg_ratio": args.online_neg_ratio if args.online_crop else None,
        "sampler": ("legacy_per_box" if args.legacy_sampler else "bin_first_weighted"),
        "size_bins_floor": args.size_bins_floor,
        "size_bins": [list(b) for b in __import__("config").SIZE_BINS],
        "ultralytics_args": ultralytics_args,
        "effective": effective,
    }
    meta_path = os.path.join(run_dir, "windowing_config.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"config hash {meta['config_hash']} -> {meta_path}")
    print(f"effective optimizer: {effective['optimizer']}  "
          f"trainable/total params: {trainable_params}/{total_params}  "
          f"epoch1_hash: {effective['epoch1_weight_hash']}")


if __name__ == "__main__":
    main()
