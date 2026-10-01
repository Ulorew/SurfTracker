# SurfTracker

**In short (English).** SurfTracker is an autonomous pan camera for filming
windsurfers. An Android phone records video and runs an on-device YOLO detector
(TFLite / LiteRT); a tracker turns detections into an angular-velocity setpoint
and streams it over Bluetooth SPP to an ESP32. The ESP32 drives an iFlight 3506
gimbal motor in a closed velocity loop (SimpleFOC + SimpleFOCMini) with an
AS5048A magnetic encoder. Status: working prototype, verified on the bench and
in live tracking of a walking person; not yet proven on water. Documentation
and code comments are in Russian.

---

Следящая камера для съёмки виндсёрферов. Телефон снимает и сам находит
сёрфера в кадре, плата поворачивает телефон за ним.

## Как это устроено

```
телефон Android ──Bluetooth SPP, 10 Гц──▶ ESP32 ──▶ SimpleFOCMini ──▶ мотор iFlight 3506
  камера + детектор YOLO (LiteRT)           esp/field_link                    │
  трекер → уставка скорости ω, ω̇         ◀── телеметрия: угол, статус ── AS5048A (SPI)
```

- Телефон шлёт **уставку скорости**, а не команду поворота. Плавность, предел
  ускорения, остановка при обрыве связи (сторож 300 мс) — на плате.
- Положение замыкает камера: трекер держит цель в центре кадра по ошибке
  наведения. Плата держит скорость по энкодеру.
- Протокол — байтовые кадры с CRC8, одна реализация на каждой стороне:
  [`docs/ПРОТОКОЛ.md`](docs/ПРОТОКОЛ.md), заголовки
  [`lib/SurfProtoV2`](lib/SurfProtoV2) (общие для прошивки и тестов).

## Статус

Проверено (сентябрь 2026):

- цепь телефон → Bluetooth → мотор: 300/300 кадров без потерь, задержка
  туда-обратно p50 37 мс, p95 56 мс;
- живое слежение за идущим человеком, 90 с: на цели 99.5%, потерь 0,
  ошибка наведения p50 2.7°;
- ход на постоянной скорости 0.05–0.3 рад/с: отклонение угла 0.1–0.2° СКО.

Не проверено и известные ограничения — в
[`docs/ПОЛЕ_ЗАПУСК.md`](docs/ПОЛЕ_ЗАПУСК.md), §7: работа на воде и дальний
галс, ветер (позиционного контура на плате нет), разворот цели через ноль
скорости, ложная цель в пустой сцене, калибровка камеры.

## Железо

| узел | что | заметки |
|---|---|---|
| телефон | Android 10+, arm64 | проверено на Redmi Note 14 Pro 5G |
| плата | ESP32 WROOM DevKit | нужен классический Bluetooth (SPP) |
| драйвер | SimpleFOCMini (DRV8313) | IN1/IN2/IN3 = GPIO 17/16/4, EN = 22 |
| мотор | iFlight 3506, 11 пар полюсов | потолок напряжения 2 В для долгой работы |
| энкодер | AS5048A, SPI | SCK/MISO/MOSI/CS = GPIO 14/27/26/25; GPIO 12 не занимать |
| питание | 11.7–12 В на драйвер | константа `V_SUPPLY` в прошивке |

Подбор мотора и коэффициентов — [`docs/foc_tuning/`](docs/foc_tuning),
энкодер — [`docs/spi_encoder/`](docs/spi_encoder).

## Структура

| путь | что |
|---|---|
| `esp/field_link` | боевая прошивка ESP32: Bluetooth, протокол, мотор |
| `esp/foc_bench` | стенд подбора коэффициентов по USB |
| `esp/*_probe`, `esp/psram_check` | диагностические скетчи |
| `lib/SurfProtoV2` | протокол и закон приёмника уставок (C++, без зависимостей) |
| `android/camfps` | приложение: съёмка, детекция, трекинг, канал к плате |
| `android/{camprobe,verify,vibelog}` | утилиты: диагностика камеры, сверка инференса, лог акселерометра |
| `tools/link` | запуск прогона, предполёт, выгрузка и сводка сессий, тесты протокола |
| `tools/stand` | стендовые метрики: плавность хода, смаз, срыв, резкость по видео |
| `tools/windowing` | обучение и оценка детектора, офлайн-трекер, сверки Java ↔ Python |
| `tools/export` | экспорт модели в LiteRT и сверка на устройстве |
| `docs/` | протокол, полевая инструкция, замеры; `docs/journal/` — инженерный журнал |

Прежняя цепь на STM32 (B-G431B-ESC1, Nucleo) выведена; её код и документы —
под тегом [`stm-era`](../../tree/stm-era). Ранний прототип на Raspberry Pi —
ветка [`legacy-hardware-tracker`](../../tree/legacy-hardware-tracker).

## Сборка

Проверить, что окружение готово (собирает прошивку и APK, проверяет
Bluetooth, adb, ffmpeg):

```bash
tools/laptop_check.sh
```

### Прошивка ESP32

Нужны `arduino-cli` в `~/Android/arduino/`, ядро `esp32:esp32@3.3.11`,
библиотека SimpleFOC 2.4.0 в `~/Arduino/libraries/`.

```bash
esp/build.sh --список                 # какие скетчи есть
esp/build.sh field_link               # собрать
esp/build.sh field_link --прошить     # собрать и залить по USB
```

Отладка по USB, 115200: `STATE`, `TEST v=0.3 t=3000`, `SET P= I= Tf= COGK=`,
`SET ACC=` (рампа, хранится в памяти платы). Открытие порта перезагружает плату,
при старте вал коротко дёргается — это калибровка датчика.

### Приложение (без Gradle)

Нужны Android SDK (build-tools 35.0.0, platform android-35), JDK и LiteRT 2.1.6:
AAR, распакованный в `~/Android/libs/x` (API-часть — в `~/Android/libs/x/api`).
Пути задаёт `~/Android/env.sh`:

```bash
export ANDROID_HOME=$HOME/Android/sdk
export JAVA_HOME=$HOME/Android/jdk
export PATH=$JAVA_HOME/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/build-tools/35.0.0:$PATH
```

```bash
cd android/camfps && ./build.sh       # -> build/com.surftracker.camfps.apk
adb install -r build/com.surftracker.camfps.apk
```

### Модель

Приложение берёт `.tflite` из своего каталога на телефоне:

```bash
adb push <модель>.tflite /storage/emulated/0/Android/data/com.surftracker.camfps/files/
```

В репозитории — только стоковый детектор людей (YOLO11n, COCO),
`export/models_person/`: годится для проверки на берегу. Модель, обученная
на виндсёрферах, и обучающие данные в репозиторий не входят.

## Запуск

```bash
export ANDROID_SERIAL=<устройство из adb devices>
tools/link/run_track.sh --ez defaults true --ef zoom 2.125 --ef focus 0 \
    --es model <модель>.tflite --ei seconds 480 \
    --ez video true --es quality 1080 --es tag поле
```

Перед стартом идёт блокирующий предполёт: 20 нулевых уставок по Bluetooth с
ноутбука, плата обязана ответить и доложить живой энкодер. Без мотора —
`--ez dry true`. Адрес платы по умолчанию зашит в `run_track.sh` и
`preflight.sh` — замените на свой.

После съёмки — выгрузка с телефона и сводка (доля времени на цели, потери,
ошибка наведения, флаги мотора):

```bash
tools/link/pull_sessions.sh <ГГММ> [--без-видео]
```

Полная полевая инструкция — [`docs/ПОЛЕ_ЗАПУСК.md`](docs/ПОЛЕ_ЗАПУСК.md).

## Тесты без железа

```bash
tools/link/control_tests.sh                    # приёмник уставок + 4 мутации, каждая обязана уронить свой тест
tools/windowing/proto_stand_check/check.sh     # протокол: C++ против Python
tools/windowing/nms_check/check.sh             # NMS: Java против эталона
python -m pytest tools/windowing/tests -q      # пайплайн детектора и трекера
```

## Обучение детектора

Пайплайн — `tools/windowing/`: генерация обучающих окон из размеченных кадров
(разметка X-AnyLabeling, [`docs/РАЗМЕТКА.md`](docs/РАЗМЕТКА.md)), кроп на лету
поверх ultralytics, обучение `train_640.py`, оценка в режиме слежения
`eval_track.py`, офлайн-петля слежения `track_run.py` / `track_eval.py`.
Экспорт в LiteRT и сверка на устройстве — `tools/export/`. Зависимости
обучения (ultralytics, torch) в `requirements-*.txt` не входят.

## Безопасность

Мотор под током: потолок 2 В выбран для долгой работы без перегрева. Знак
вращения задаётся константой `DIR` в `esp/field_link/field_link.ino`: при
неверном знаке камера уходит от цели, а не за ней.

## Лицензия

[AGPL-3.0](LICENSE). Сторонние компоненты:

- [Ultralytics YOLO](https://github.com/ultralytics/ultralytics) — AGPL-3.0
  (обучение; модели в `export/models_person` экспортированы из YOLO11n);
- [COCO](https://cocodataset.org) — аннотации CC BY 4.0 (стоковая модель обучена на нём);
- [LiteRT](https://github.com/google-ai-edge/LiteRT) — Apache-2.0 (рантайм на телефоне);
- [SimpleFOC](https://github.com/simplefoc/Arduino-FOC) — MIT (управление мотором);
- [Arduino-ESP32](https://github.com/espressif/arduino-esp32) — LGPL-2.1.

## Язык

Документация, комментарии и часть имён файлов — на русском. Комментарии
подробные намеренно: это инженерный дневник — почему сделано так и какая
ошибка за этим стояла.
