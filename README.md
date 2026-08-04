# SurfTracker — windsurf detection (ML pipeline)

`master` — этот код: оконный (windowed) пайплайн обучения/оценки YOLO для
детекции виндсёрфов на видео, с прицелом на будущий трекинг и портирование
части примитивов на Android (Kotlin, бит-точно).

Старый (аппаратный, Arduino/RPI/Kalman-трекер) проект той же репы — ветка
[`legacy-hardware-tracker`](https://github.com/Ulorew/SurfTracker/tree/legacy-hardware-tracker),
не связан с содержимым `master`.

## Структура

- `tools/windowing/` — основной пайплайн:
  - `geometry.py`, `crop.py`, `track_window.py` — чистая геометрия без ML-зависимостей,
    кандидаты на побитовый Kotlin-порт (round-half-up, а не банковское округление).
  - `sample_window.py`, `negatives.py`, `augment.py`, `dataset_gen.py` — генерация
    обучающего датасета из размеченных кадров (X-AnyLabeling json) в окна YOLO-формата.
  - `online_dataset.py`, `online_variants.py` — кроп на лету (custom ultralytics
    Dataset/Trainer): офлайн 3-4 фильтрованные полнокадровые версии + онлайн
    выбор версии/кроп/флип, вместо статичной нарезки на диске.
  - `train_640.py` — обучение (обёртка над ultralytics), пишет манифест с
    эффективными (не декларативными) параметрами прогона.
  - `eval_track.py` — оценка в режиме слежения (окно вокруг известной цели);
    основная метрика — полнота при выровненных ложных срабатываниях.
  - `eval_640.py` — оценка на статичной нарезке окон (второй, более простой режим).
  - `tile_infer_video.py` — поклеточный инференс на реальном видео (перекрывающиеся
    тайлы + NMS-склейка) — визуализация модели на полном кадре.
  - `plot_training_curves.py` — генератор интерактивных HTML-графиков обучения.
  - `tests/` — юнит-тесты (pytest).
- `tools/extract_frames.py`, `tools/extract_uniform.py` — извлечение кадров из видео.
- `reports/` — текстовые отчёты и своды по датасету/обучению v1.

## Запуск тестов

```
cd tools/windowing
python -m pytest tests/ -q
```

Данные, веса моделей, видео и сгенерированные артефакты (`.pt`, изображения,
`output/`) в репозиторий не входят — см. `.gitignore`.
