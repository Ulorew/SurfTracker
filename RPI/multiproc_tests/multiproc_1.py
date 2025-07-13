import math
import multiprocessing as mp
import sys

import psutil
from picamera2 import Picamera2
import libcamera
import os
import time
import cv2
import numpy as np
from ultralytics import YOLO

FOV = (54 / 180) * math.pi
steps_per_rev = 3200

# Список цветов для различных классов
colors = [
    (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255),
    (255, 0, 255), (192, 192, 192), (128, 128, 128), (128, 0, 0), (128, 128, 0),
    (0, 128, 0), (128, 0, 128), (0, 128, 128), (0, 0, 128), (72, 61, 139),
    (47, 79, 79), (47, 79, 47), (0, 206, 209), (148, 0, 211), (255, 20, 147)
]


# Функция для обработки изображения
def process_image(picam, model):
    # ff = open(os.devnull, 'w')
    # sys.stdout = ff
    # sys.stderr = ff
    # Загрузка изображения
    st_time = time.time()
    image = picam.capture_array()
    results = model(image)[0]
    image_path = "out.png"

    # Получение оригинального изображения и результатов
    image = results.orig_img
    classes_names = results.names
    classes = results.boxes.cls.cpu().numpy()
    boxes = results.boxes.xyxy.cpu().numpy().astype(np.int32)

    # Подготовка словаря для группировки результатов по классам
    grouped_objects = {}

    # Рисование рамок и группировка результатов
    for class_id, box in zip(classes, boxes):
        class_name = classes_names[int(class_id)]
        color = colors[int(class_id) % len(colors)]  # Выбор цвета для класса
        if class_name not in grouped_objects:
            grouped_objects[class_name] = []
        grouped_objects[class_name].append(box)

        # Рисование рамок на изображении
        x1, y1, x2, y2 = box
        cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
        cv2.putText(image, class_name, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

    # Сохранение измененного изображения
    new_image_path = os.path.splitext(image_path)[0] + '_yolo' + os.path.splitext(image_path)[1]
    cv2.imwrite(new_image_path, image)

    # Сохранение данных в текстовый файл
    text_file_path = os.path.splitext(image_path)[0] + '_data.txt'
    with open(text_file_path, 'w') as f:
        for class_name, details in grouped_objects.items():
            f.write(f"{class_name}:\n")
            for detail in details:
                f.write(f"Coordinates: ({detail[0]}, {detail[1]}, {detail[2]}, {detail[3]})\n")

    print(f"Processed {image_path}:")
    print(f"Saved bounding-box image to {new_image_path}")
    print(f"Saved data to {text_file_path}")
    return time.time() - st_time


def inference_process():
    p = psutil.Process(os.getpid())
    p.cpu_affinity([2, 3])

    picam = Picamera2()
    config = picam.create_preview_configuration(transform=libcamera.Transform(hflip=1, vflip=1),
                                                main={"size": (640, 640), "format": "RGB888"})
    picam.configure(config)
    picam.start()

    # Загрузка модели YOLOv8
    model = YOLO("models/yolo11n_ncnn_model/")
    run_time = time.time()
    while True:
        if time.time() - run_time > 5:
            start = time.time()
            process_image(picam, model)  # заглушка
            elapsed = time.time() - start
            print(f"[Inference] обработано изображение за {elapsed * 1000:.1f} ms")
        else:
            time.sleep(0.1)


def light_task_process(matrix_size=100):
    # p = psutil.Process(os.getpid())
    # p.cpu_affinity([0, 1])

    # Генерируем две постоянные матрицы
    A = np.random.rand(matrix_size, matrix_size)
    B = np.random.rand(matrix_size, matrix_size)

    while True:
        loop_start = time.time()

        # Умножаем матрицы
        C = A.dot(B)
        # Берём, скажем, сумму всех элементов как «результат»
        result = np.sum(C)

        op_time = (time.time() - loop_start) * 1000  # в ms
        # Ждём остаток из 100мс
        sleep_time = max(0, 0.1 - (time.time() - loop_start))
        time.sleep(sleep_time)

        total_time = (time.time() - loop_start) * 1000
        print(f"[LightTask] result={result:.2f}, op_time={op_time:.1f} ms, total_loop={total_time:.1f} ms")


if __name__ == "__main__":
    mp.set_start_method('fork')  # на Raspberry Pi обычно 'fork' работает лучше
    p1 = mp.Process(target=inference_process, name="InferenceProcess")
    p2 = mp.Process(target=light_task_process, name="LightTaskProcess")

    p1.start()
    p2.start()

    try:
        # Опционально: ждём завершения обоих (никогда не наступит)
        p1.join()
        p2.join()
    except KeyboardInterrupt:
        print("Получен SIGINT, завершаем процессы...")
        p1.terminate()
        p2.terminate()
        p1.join()
        p2.join()
        print("Завершено.")
