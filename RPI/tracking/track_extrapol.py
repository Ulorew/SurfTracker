import math
from collections import deque

from picamera2 import Picamera2
import libcamera
import os
import time
import cv2
import numpy as np
from scipy.misc import derivative
from ultralytics import YOLO
from scipy.interpolate import CubicSpline, CubicHermiteSpline
from time import perf_counter

from motor_driver import *


class catchtime:
    def __init__(self, name: str = "Time", target_duration=None):
        self.name = name
        self.target_duration = target_duration

    def __enter__(self):
        self.start = perf_counter()
        return self

    def __exit__(self, type, value, traceback):
        self.time = perf_counter() - self.start
        if self.target_duration is not None:
            if self.time < self.target_duration:
                time.sleep(self.target_duration - self.time)
            elif self.time > self.target_duration * 2:
                print("Time overhead!")

        if self.target_duration is not None:
            self.readout = f'{self.name}: {self.time * 1000:.0f} + {(self.target_duration - self.time) * 1000:.0f} +  ms'
        else:
            self.readout = f'{self.name}: {self.time * 1000:.0f} ms'
        print(self.readout)


class Kalman1D:
    def __init__(self, pos0, vel0=0, process_var=1.0, meas_var=10.0):
        # Состояние: [позиция, скорость, ускорение]
        self.x = np.array([[pos0], [vel0]])

        # Начальная ковариация
        self.P = np.eye(2) * 100.0

        # Дисперсии
        self.process_var = process_var  # шум модели
        self.meas_var = meas_var  # шум измерения

        # Матрица наблюдения (мы наблюдаем только позицию)
        self.H = np.array([[1, 0]])

        # Дисперсия измерения
        self.R = np.array([[meas_var]])

        self.last_t = None

    def reset(self, pos0=None, t0=None):
        if pos0 is not None:
            self.x = np.array([[pos0], [0]])
        self.P = np.eye(2) * 100.0

        self.last_t = t0

    def predict(self, dt):
        # Модель перехода
        F = np.array([
            [1, dt],
            [0, 1],
        ])
        # Модель шумов (дискретизированная для постоянного ускорения)
        G = np.array([
            [dt],
            [1]
        ])
        # Q = self.process_var * (G @ G.T)
        dynamic_coef = (dt / 0.2)  # (dt ** 2)
        Q = self.process_var * dynamic_coef * (G @ G.T)
        # Предсказание
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q

    def update(self, z):
        # Ошибка
        y = np.array([[z]]) - self.H @ self.x
        S = self.H @ self.P @ self.H.T + self.R
        K = self.P @ self.H.T @ np.linalg.inv(S)  # Калмановское усиление

        # Коррекция
        self.x += K @ y
        I = np.eye(self.P.shape[0])
        self.P = (I - K @ self.H) @ self.P

    def step(self, z, t=None):
        now = t if t is not None else time.time()
        if self.last_t is None:
            self.last_t = now
            return self.x.copy()

        dt = now - self.last_t
        self.last_t = now

        self.predict(dt)
        self.update(z)

        return self.x.copy()

    def forecast(self, dt_future, steps=1):
        # Предсказание вперёд без измерений
        x_pred = self.x.copy()
        for _ in range(steps):
            F = np.array([
                [1, dt_future],
                [0, 1],
            ])
            x_pred = F @ x_pred
        return x_pred


picam = None


def init_camera():
    global picam
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

SHARPEN_KERNEL = np.array([[ 0, -1,  0],
                           [-1,  5, -1],
                           [ 0, -1,  0]], dtype=np.float32)

def normalize_frame(frame):
    yuv = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV)
    # эквализуем Y-канал
    yuv[:, :, 0] = cv2.equalizeHist(yuv[:, :, 0])
    eq = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)

    denoised = cv2.GaussianBlur(eq, (3, 3), sigmaX=0.5)


    gauss = cv2.GaussianBlur(denoised, (0, 0), sigmaX=1.0)
    sharpened = cv2.addWeighted(denoised, 1.3, gauss, -0.3, 0)
    return sharpened

def take_photo():
    frame = picam.capture_array()
    frame = cv2.resize(frame, (IMG_W, IMG_H), interpolation=cv2.INTER_LINEAR)
    # frame[:IMG_H//2, :] = normalize_frame(frame[:IMG_H//2, :])
    frame = normalize_frame(frame)
    return frame


IMG_W, IMG_H = 640, 480
FOV = (63.7 / 180) * math.pi
CAM_DEPTH = (0.5) / math.tan(FOV / 2)  # H/w
JOIN_TIME = 2.
LOSE_TIME = 2.
WAIT_TIME = 3.
YOLO_TRACK_INTERVAL = 0.1

STEPS_PER_REV = 4400

kalman = Kalman1D(0, 0, 250, 1)
yolo = None
cvtracker = None

streak_frame_id = 0

print(f"Working in resolution {IMG_W}x{IMG_H}")
print(f"FOV: {FOV} rad, cam_depth: {CAM_DEPTH}")
print(f"Join time: {JOIN_TIME}")

last_det = 0
last_yolo_infer = time.perf_counter() - 10000
surf_traj = CubicSpline([0, 1], [0, 0])
cam_traj = CubicSpline([0, 1], [0, 0])
start_time = 0.
last_tracker=""

X = deque(maxlen=50)
Y = deque(maxlen=50)

target_classes = [0]
draw = False
inference_times = deque(maxlen=10)
last_seen_pos = 0


def eval_traj(traj, dur):
    tm = time.perf_counter() - start_time
    t = np.linspace(tm, tm + dur, 6)
    return ', '.join([f"{float(traj(i)):0.1f}" for i in t])


class Parabola:
    def __init__(self, a, b, c):
        self.a = a
        self.b = b
        self.c = c

    def __call__(self, x):
        return self.a * (x ** 2) + self.b * x + self.c

    def derivative(self):
        return Parabola(0, self.a * 2, self.b)


def shifted_parabola(x, y, dy, ddy):
    ta, tb, tc = ddy / 2., dy, y
    return Parabola(ta, tb - 2 * ta * x, tc + ta * (x ** 2) - tb * x)


def update_traj():
    global cam_traj, surf_traj
    cur_time = time.perf_counter() - start_time
    cur_surf_pos = Y[-1]

    traj_info = kalman.step(cur_surf_pos, cur_time)
    pos, vel = traj_info.squeeze()

    print(f"Kalman info | pos: {pos:.1f}     vel: {vel:.1f}")
    surf_traj = CubicSpline([cur_time, cur_time + 1], [pos, pos + vel])

    print(f"Surf traj pred: {eval_traj(surf_traj, JOIN_TIME)}")
    print(f"Cam traj previous: {eval_traj(cam_traj, JOIN_TIME)}")

    tl, tr = cur_time, cur_time + JOIN_TIME

    yl, yr = cam_traj(cur_time), surf_traj(tr)
    dl, dr = cam_traj.derivative()(tl), surf_traj.derivative()(tr)
    cam_traj = CubicHermiteSpline([tl, tr], [yl, yr], [dl, dr])

    cam_vel = cam_traj.derivative()
    cam_acc = cam_vel.derivative()
    cam_thd = cam_acc.derivative()

    # set_traj(pos, cam_vel, cam_acc, cam_thd)
    set_traj(cam_thd(cur_time) / 6., cam_acc(cur_time) / 2., cam_vel(cur_time), cam_traj(cur_time))
    # print(f"Cam traj new: {eval_traj(cam_traj, join_time)}")


def track(frame):
    global last_yolo_infer, cvtracker, last_tracker

    if time.perf_counter() - last_yolo_infer >= YOLO_TRACK_INTERVAL or cvtracker is None:
        print("Tracking with YOLO")
        last_tracker="YOLO"
        last_yolo_infer = time.perf_counter()
        with catchtime(name="YOLO tracker"):
            det = yolo.predict(frame, verbose=False, classes=target_classes, conf=0.6)[0]
        if len(det.boxes.conf) == 0:
            return None, None, None, None

        targ = np.argmax(list(det.boxes.conf))
        b_x, b_y, b_w, b_h = det.boxes.xywhn[targ][:4]
        x1, y1 = int((b_x - b_w / 2) * IMG_W), int((b_y - b_h / 2) * IMG_H)
        w1, h1 = int(b_w * IMG_W), int(b_h * IMG_H)

        cvtracker = cv2.TrackerKCF_create()
        cvtracker.init(frame, (x1, y1, w1, h1))
        return b_x, b_y, b_w, b_h

    else:
        print("Tracking with CV2")
        last_tracker = "CV2"
        with catchtime(name="CV tracker"):
            success, bbox = cvtracker.update(frame)
        if not success:
            return None, None, None, None
        x1, y1, w1, h1 = map(int, bbox)
        b_x = (x1 + w1 / 2) / IMG_W
        b_y = (y1 + h1 / 2) / IMG_H
        return b_x, b_y, w1 / IMG_W, h1 / IMG_H


def process_image():
    global cam_traj, X, Y, streak_frame_id, last_det, last_seen_pos
    cap_pos = upd_cur_pos()
    frame = take_photo()
    inference_times.append(time.perf_counter())
    if len(inference_times) > 1:
        print(f"Infer FPS: {(len(inference_times) - 1.0) / (inference_times[-1] - inference_times[0]):.1f}")

    cap_time = time.perf_counter() - start_time

    print(f"Time: {cap_time:.2f} s  |  Capturing at {cap_pos}")

    b_x, b_y, b_w, b_h = track(frame)
    if b_x is not None:
        ang_dif = math.atan2((float(b_x) - 0.5), CAM_DEPTH)
        dsteps = -round(STEPS_PER_REV * ang_dif / (2 * math.pi))
        obj_pos = cap_pos + dsteps

        last_seen_pos = obj_pos
        last_det = time.perf_counter() - start_time

        if draw:
            x1, y1 = int((b_x - b_w / 2) * IMG_W), int((b_y - b_h / 2) * IMG_H)
            w1, h1 = int(b_w * IMG_W), int(b_h * IMG_H)
            cv2.rectangle(frame, (x1, y1), (x1 + w1, y1 + h1), (0, 255, 0) if last_tracker=="YOLO" else (255, 55, 0), 2)
            cv2.putText(frame, f"Obj at {obj_pos:.0f}", (200, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

        print(f'Found at {b_x}. Object pos: {obj_pos}')

        X.append(float(cap_time))
        Y.append(float(obj_pos))
        update_traj()
        print("Planned trajectories:")
        print(f"Surf traj: {eval_traj(surf_traj, JOIN_TIME)}")
        print(f"Cam  traj: {eval_traj(cam_traj, JOIN_TIME)}")
    else:
        print("Nothing found")
        if cap_time - last_det > LOSE_TIME:
            streak_frame_id = 0
            print("Lost target")

            kalman.reset(pos0=last_seen_pos, t0=time.perf_counter())
            X.clear()
            Y.clear()

            if cap_time - last_det <= LOSE_TIME + WAIT_TIME:
                cam_traj = CubicSpline([0., 1.], [last_seen_pos, last_seen_pos])
                set_traj(last_seen_pos, 0, 0, 0)
            else:
                cam_traj = CubicSpline([0., 1.], [0, 0])
                set_traj(0, 0, 0, 0)

    if draw:
        with catchtime(name="Drawing&Saving"):
            cv2.putText(frame, f"Cam at {cap_pos:.0f}", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            cv2.putText(frame, f"Time: {cap_time:.2f} s", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            cv2.imwrite(f"images/Last.png", frame)
            cv2.imwrite(f"images/YOLOchka_{streak_frame_id}.png", frame)
            # cv2.imshow("frame", frame)
            streak_frame_id += 1
    print()


if __name__ == "__main__":
    init_camera()
    start_time = time.perf_counter()
    last_det = - 100
    set_traj(0, 0, 0, 0)

    # model = YOLO("models/yolo11n_ncnn_model/", task="detect")
    yolo = YOLO("models/people_sub_3_ncnn_model/", task="detect")
    print("Модель загружена")
    run_time = time.time()
    while True:
        if time.time() - run_time > 0.5:
            process_image()
        else:
            time.sleep(0.1)
        # if time.perf_counter() - start_time > 30:
        #     break
