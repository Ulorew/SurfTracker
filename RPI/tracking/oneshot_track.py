import math
import multiprocessing as mp
import sys
from collections import deque

import psutil
import serial
from picamera2 import Picamera2
import libcamera
import os
import time
import cv2
import numpy as np
from ultralytics import YOLO
from scipy.interpolate import CubicSpline, CubicHermiteSpline
from time import perf_counter

class catchtime:
    def __init__(self, name:str="Time"):
        self.name = name

    def __enter__(self):
        self.start = perf_counter()
        return self

    def __exit__(self, type, value, traceback):
        self.time = perf_counter() - self.start
        self.readout = f'{self.name}: {self.time*1000:.0f} ms'
        print(self.readout)

img_w, img_h = 640, 480
FOV = (62.2 / 180) * math.pi
cam_depth = (0.5) / math.tan(FOV / 2)  # H/w

print(f"Working in resolution {img_w}x{img_h}")
print(f"FOV: {FOV} rad, cam_depth: {cam_depth}")

steps_per_rev = 8900

track_traj = CubicSpline([0, 1], [0, 0])
X, Y = [], []
ser = serial.Serial('/dev/ttyACM0', 115200, timeout=0.1, write_timeout=0.1)
ser.reset_input_buffer()
ser.write("0\n".encode())

draw = True
cur_pos = 0
inference_times = deque(maxlen=5)


def upd_cur_pos():
    global cur_pos
    st=time.time()
    lns = ser.read_all().decode().split('\n')
    if time.time()-st>0.2:
        print(f"I've been reading for {time.time()-st:.2f} seconds!")
    if len(lns) <= 2:
        print(lns)
        raise IOError("Not enough position marks from arduino")
    cur_pos = int(lns[-2])
    print(f"Read {cur_pos}")
    return cur_pos

def set_goal(goal):
    try:
        ser.write(f"{goal}\n".encode())
    except serial.serialutil.SerialTimeoutException as e:
        print("Serial write timed out")



def process_image(picam, model):
    upd_cur_pos()

    frame = picam.capture_array()
    frame=cv2.resize(frame, (img_w, img_h), interpolation=cv2.INTER_AREA)

    inference_times.append(time.time())
    if len(inference_times) > 1:
        print(f"Infer FPS: {(len(inference_times)-1.0)/(inference_times[-1]-inference_times[0]):.1f}")

    if draw:
        cv2.imwrite("images/YOLOchka.jpg", frame)


    res = model.predict(frame, classes=[39])[0]

    if len(res.boxes.conf) > 0:
        targ = np.argmax(list(res.boxes.conf))
        pos = res.boxes.xywhn[targ][:2]
        ang_dif = math.atan2((float(pos[0]) - 0.5), cam_depth)
        dsteps = -round(steps_per_rev * ang_dif / (2 * math.pi))
        # dsteps //= 2
        obj_pos = cur_pos + dsteps

        # print(res.probs)
        # for (id, prob) in zip(res.names, res.probs):
        #     print(id, prob)
        print(f'Angular difference: {ang_dif}')
        print(f'Found at {pos}  Rotating by {dsteps} to {obj_pos}')
        X.append(time.time())
        Y.append(obj_pos)


        with catchtime(name="Ser write") as t:
            set_goal(obj_pos)
            time.sleep(2)


def inference_process():
    p = psutil.Process(os.getpid())
    p.cpu_affinity([2, 3])

    picam = Picamera2()
    cam_w, cam_h = picam.sensor_resolution

    config = picam.create_preview_configuration(
        transform=libcamera.Transform(hflip=1, vflip=1),
        main={"size": (cam_w, cam_h), "format": "RGB888"}
    )
    picam.configure(config)
    picam.start()
    print("Камера запущена")

    model = YOLO("models/yolo11n_ncnn_model/", task="detect")
    print("Модель загружена")
    run_time = time.time()
    while True:
        if time.time() - run_time > 1:
            start = time.time()
            process_image(picam, model)

            elapsed = time.time() - start
            print(f"[Inference] обработано изображение за {elapsed * 1000:.1f} ms")
        else:
            time.sleep(0.1)


if __name__ == "__main__":
    mp.set_start_method('fork')  # на Raspberry Pi обычно 'fork' работает лучше
    p1 = mp.Process(target=inference_process, name="InferenceProcess")
    # p2 = mp.Process(target=light_task_process, name="LightTaskProcess")

    p1.start()
    # p2.start()

    try:
        # Опционально: ждём завершения обоих (никогда не наступит)
        p1.join()
        #p2.join()
    except KeyboardInterrupt:
        print("Получен SIGINT, завершаем процессы...")
        p1.terminate()
        #p2.terminate()
        p1.join()
        #p2.join()
        print("Завершено.")
