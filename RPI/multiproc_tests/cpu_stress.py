import numpy as np
import numexpr as ne
import multiprocessing
import time
import psutil
from gpiozero import CPUTemperature
import os

# Параметры безопасности
MAX_SAFE_TEMP = 65  # Остановка при этой температуре
CHECK_INTERVAL = 1  # Проверка температуры каждую секунду
stress_dur = 15


def stress_core(core_id):
    """Интенсивная нагрузка с использованием SIMD и больших матриц"""
    np.random.seed(core_id)
    size = 1024  # Размер матрицы (увеличиваем для нагрузки на память)

    # Выделяем память под матрицы
    a = np.random.rand(size, size).astype(np.float64)
    b = np.random.rand(size, size).astype(np.float64)
    c = np.zeros((size, size), dtype=np.float64)

    end_time = time.time() + stress_dur
    # Цикл интенсивных вычислений
    # print("Начинаем грузить!")
    while time.time() < end_time:
        # 1. Матричные операции (нагрузка на FPU)
        np.matmul(a, b, out=c)

        # 2. Вычисления с numexpr (JIT-оптимизация, использование SIMD)
        ne.evaluate("sin(a) * exp(b) + log1p(abs(c))", out=c)

        # 3. Тяжелые трансцендентные функции
        c = np.arctan2(np.sin(c), np.cos(b)) * np.tanh(a)

        # 4. Операции с двойной точностью
        c = (a ** 3.7 + b ** 2.8) / (np.abs(c) + 1e-8)

        # 5. Периодическое обновление данных
        if time.time() % 5 < 0.01:  # Каждые 5 сек
            a = np.roll(a, 1, axis=0)
            b = np.roll(b, -1, axis=1)
    # print("Закончили грузить(")


def monitor_temp(exit_flag):
    """Мониторинг температуры с расширенной диагностикой"""
    cpu = CPUTemperature()
    while True:
        temp = cpu.temperature
        load = os.getloadavg()[0]
        mem = psutil.virtual_memory().percent

        print(f"Темп: {temp:.1f}°C | Нагрузка: {load:.1f} | Память: {mem}% | Ядра: {psutil.cpu_percent(percpu=True)}")

        if temp >= MAX_SAFE_TEMP:
            print(f"! ДОСТИГНУТ МАКСИМУМ {temp:.1f}°C ! Останавливаю")
            exit_flag.value = True
            return

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    print("ИНТЕНСИВНЫЙ СТРЕСС-ТЕСТ RASPBERRY PI 5")
    print(f"Цель: >60°C | Остановка при {MAX_SAFE_TEMP}°C")
    print("Запуск... (Ctrl+C для остановки)")

    num_cores = multiprocessing.cpu_count()
    exit_flag = multiprocessing.Value('b', False)

    # Увеличиваем приоритет процесса
    # os.nice(-20)

    # Мониторинг температуры
    monitor_process = multiprocessing.Process(target=monitor_temp, args=(exit_flag,))
    monitor_process.start()

    # Запускаем рабочие процессы
    processes = []
    for i in range(num_cores):
        p = multiprocessing.Process(target=stress_core, args=(i,))
        p.start()
        processes.append(p)

    try:
        # Дополнительная нагрузка на память в основном процессе
        large_buffer = bytearray(1024 * 1024 * 500)  # 500 MB буфер
        while not exit_flag.value:
            # Шифрование XOR для нагрузки на память
            key = os.urandom(1)[0]
            for i in range(len(large_buffer)):
                large_buffer[i] ^= key
            time.sleep(0.1)
    except KeyboardInterrupt:
        exit_flag.value = True
    finally:
        for p in processes:
            p.join()
        monitor_process.join()
        print("Тест завершен")
