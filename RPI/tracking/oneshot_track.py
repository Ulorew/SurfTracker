import math
from collections import deque

from picamera2 import Picamera2
import libcamera
import os
import time
import cv2
import numpy as np
from responses import start
from scipy.misc import derivative
from ultralytics import YOLO
from scipy.interpolate import CubicSpline, CubicHermiteSpline, PPoly, make_interp_spline, KroghInterpolator
from time import perf_counter

from motor_driver import *

clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))


def normalize_frame(frame):
    """
    1) Адаптивное выравнивание гистограммы (CLAHE) на яркостном канале L в цветовом пространстве LAB
    2) Лёгкая шумоподавляющая фильтрация (билатеральный фильтр)
    3) Unsharp-маска для повышения чёткости без «ломки» текстур
    """
    # 1. Преобразуем в LAB и разделим каналы
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    L, A, B = cv2.split(lab)

    # 2. CLAHE для яркостного канала

    L_eq = clahe.apply(L)

    # 3. Снова собираем и конвертируем в BGR
    lab_eq = cv2.merge((L_eq, A, B))
    eq_bgr = cv2.cvtColor(lab_eq, cv2.COLOR_LAB2BGR)

    # 4. Лёгкое шумоподавление (билатеральный фильтр сохраняет края)
    denoised = cv2.bilateralFilter(eq_bgr, d=5, sigmaColor=75, sigmaSpace=75)

    # 5. Unsharp-маска: img + amount*(img – gaussian(img))
    gaussian = cv2.GaussianBlur(denoised, (0, 0), sigmaX=1.0)
    sharpened = cv2.addWeighted(denoised, 1.5, gaussian, -0.5, 0)
    return sharpened


IMG_W, IMG_H = 640, 480
FOV = (63.8 / 180) * math.pi
CAM_DEPTH = (0.5) / math.tan(FOV / 2)  # H/w
JOIN_TIME = 2.
LOSE_TIME = 2.
# 75 : -1475
STEPS_PER_REV = 8800

streak_frame_id = 0

print(f"Working in resolution {IMG_W}x{IMG_H}")
print(f"FOV: {FOV} rad, cam_depth: {CAM_DEPTH}")
print(f"Join time: {JOIN_TIME}")

last_det = 0
surf_traj = CubicSpline([0, 1], [0, 0])
cam_traj = CubicSpline([0, 1], [0, 0])
start_time = 0.

X = deque(maxlen=50)
Y = deque(maxlen=50)

target_classes = [0]
draw = True
inference_times = deque(maxlen=5)
last_seen_pos = 0


def eval_traj(traj, dur):
    tm = time.perf_counter() - start_time
    t = np.linspace(tm, tm + dur, 6)
    return ', '.join([f"{float(traj(i)):0.1f}" for i in t])


def process_image(picam, model):
    global cam_traj, X, Y, streak_frame_id, last_det, last_seen_pos
    cap_pos = upd_cur_pos()
    frame = picam.capture_array()
    frame = cv2.resize(frame, (IMG_W, IMG_H), interpolation=cv2.INTER_AREA)
    frame[:, :IMG_W//2] = normalize_frame(frame[:, :IMG_W//2])
    inference_times.append(time.perf_counter())
    if len(inference_times) > 1:
        print(f"Infer FPS: {(len(inference_times) - 1.0) / (inference_times[-1] - inference_times[0]):.1f}")
    cap_time = time.perf_counter() - start_time
    print(f"Time: {cap_time:.2f} s  |  Capturing at {cap_pos}")

    res = model.predict(frame, verbose=False, classes=target_classes, conf=0.6)[0]

    if len(res.boxes.conf) > 0:
        targ = np.argmax(list(res.boxes.conf))
        b_x, b_y, b_w, b_h = res.boxes.xywhn[targ][:4]

        ang_dif = math.atan2((float(b_x) - 0.5), CAM_DEPTH)
        dsteps = -round(STEPS_PER_REV * ang_dif / (2 * math.pi))
        obj_pos = cap_pos + dsteps
        last_seen_pos = obj_pos

        if draw:
            x1 = int((b_x - b_w / 2) * IMG_W)
            y1 = int((b_y - b_h / 2) * IMG_H)
            w1 = int(b_w * IMG_W)
            h1 = int(b_h * IMG_H)
            cv2.rectangle(frame, (x1, y1), (x1 + w1, y1 + h1), (0, 255, 0), 2)
            cv2.putText(frame, f"Obj at {obj_pos:.0f}", (200, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

        # print(res.probs)
        # for (id, prob) in zip(res.names, res.probs):
        #     print(id, prob)P-2200.131
        # print(f'Angular difference: {ang_dif}')
        print(f'Found at {b_x:.3f}. Object pos: {obj_pos}')
        set_traj(obj_pos, 0, 0, 0)
        last_det = time.perf_counter() - start_time
        time.sleep(3)
    else:
        print("Nothing found")

    if draw:
        cv2.putText(frame, f"Cam at {cap_pos:.0f}", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        cv2.putText(frame, f"Time: {cap_time:.2f} s", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        cv2.imwrite(f"images/Last.png", frame)
        cv2.imwrite(f"images/YOLOchka_{streak_frame_id}.png", frame)
        # cv2.imshow("frame", frame)
        streak_frame_id += 1
    print()


def tracking():
    global start_time, last_det
    start_time = time.perf_counter()
    last_det = start_time - 100

    picam = Picamera2()
    cam_w, cam_h = picam.sensor_resolution

    config = picam.create_preview_configuration(
        transform=libcamera.Transform(hflip=1, vflip=1),
        main={"size": (cam_w, cam_h), "format": "RGB888"},
        controls={
            # "FrameDurationLimits": (100000//5, 300000//5),
            # "AnalogueGain": 1.0,
            # "AwbEnable": True,
            "ExposureTime": 20000,  # 10 мс (1/100 секунд) — уменьшает размытие
            # "AnalogueGain": 2.5,  # ISO ~ 2.5 * базового — баланс шум/светочувствительность
            "AwbEnable": True  # авто-баланс белого
        }
    )
    picam.configure(config)
    picam.start()
    print("Камера запущена")

    # model = YOLO("models/yolo11n_ncnn_model/", task="detect")
    model = YOLO("models/people_sub_3_ncnn_model/", task="detect")
    print("Модель загружена")
    run_time = time.time()
    while True:
        if time.time() - run_time > 0.5:
            process_image(picam, model)
        else:
            time.sleep(0.1)


if __name__ == "__main__":
    tracking()
