import math
import multiprocessing as mp
import sys
from collections import deque

import psutil
from picamera2 import Picamera2
import libcamera
import os
import time
import cv2
import numpy as np
from ultralytics import YOLO
from scipy.interpolate import CubicSpline, CubicHermiteSpline, PPoly
from time import perf_counter
from motor_driver import motor_driver_process


class KalmanFilterVarDT:
    def __init__(self, process_var, meas_var):
        # H и R неизменны, измеряется только положение
        self.H = np.array([[1., 0., 0.]])
        self.R = np.array([[meas_var]])

        # Q задаётся динамически в predict(), init тут только форма
        self.process_var = process_var

        # Состояние: [p, v, a]
        self.x = np.zeros((3, 1))
        self.P = np.eye(3) * 1000

    def _make_F_Q(self, dt):
        # Составляем матрицу перехода и ковариацию шума процесса для данного dt
        F = np.array([
            [1, dt, 0.5 * dt ** 2],
            [0, 1, dt],
            [0, 0, 1]
        ])
        q = self.process_var
        Q = q * np.array([
            [dt ** 4 / 4, dt ** 3 / 2, dt ** 2 / 2],
            [dt ** 3 / 2, dt ** 2, dt],
            [dt ** 2 / 2, dt, 1]
        ])
        return F, Q

    def predict(self, dt):
        """Шаг предсказания с произвольным шагом времени dt."""
        F, Q = self._make_F_Q(dt)
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q

    def update(self, z):
        """Корректировка по измерению позиции z."""
        z = np.array([[z]])
        y = z - (self.H @ self.x)
        S = self.H @ self.P @ self.H.T + self.R
        K = self.P @ self.H.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        I = np.eye(self.P.shape[0])
        self.P = (I - K @ self.H) @ self.P

    def forecast(self, T):
        """
        Прогноз положения через произвольный промежуток T.
        Возвращает одно число — прогнозируемую позицию.
        """
        # Матрица перехода на шаг T
        F_T, _ = self._make_F_Q(T)
        x_pred = F_T @ self.x
        return float(x_pred[0])

class catchtime:
    def __init__(self, name: str = "Time"):
        self.name = name

    def __enter__(self):
        self.start = perf_counter()
        return self

    def __exit__(self, type, value, traceback):
        self.time = perf_counter() - self.start
        self.readout = f'{self.name}: {self.time * 1000:.0f} ms'
        print(self.readout)


def normalize_frame(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    v = cv2.equalizeHist(v)
    frame_eq = cv2.merge([h, s, v])
    frame_eq = cv2.cvtColor(frame_eq, cv2.COLOR_HSV2BGR)

    blurred = cv2.GaussianBlur(frame_eq, (0, 0), sigmaX=3)
    sharp = cv2.addWeighted(frame_eq, 1.5, blurred, -0.5, 0)
    return sharp


IMG_W, IMG_H = 640, 480
FOV = (63.5 / 180) * math.pi
CAM_DEPTH = (0.5) / math.tan(FOV / 2)  # H/w
JOIN_TIME = 1.5
LOSE_TIME = 2.

STEPS_PER_REV = 8800

streak_frame_id = 0

print(f"Working in resolution {IMG_W}x{IMG_H}")
print(f"FOV: {FOV} rad, cam_depth: {CAM_DEPTH}")
print(f"Join time: {JOIN_TIME}")


surf_traj = CubicSpline([0, 1], [0, 0])
cam_traj = CubicSpline([0, 1], [0, 0])
X, Y = [], []

target_classes = [0]
draw = True
inference_times = deque(maxlen=5)



def eval_traj(traj, dur):
    tm = time.time()
    t = np.linspace(tm, tm + dur, 6)
    return ', '.join([f"{float(traj(i)):0.1f}" for i in t])


def update_traj(ns):
    global surf_traj, X, Y, cam_traj
    if len(X) == 0:
        return lambda x: 0
    if len(X) == 1:
        return CubicSpline([0., 1.], [Y[0], Y[0]])

    length = min(len(X), 2)
    surf_traj = CubicSpline(X[-length:], Y[-length:])

    # print(f"Surf traj pred: {eval_traj(surf_traj, join_time)}")
    # print(f"Cam traj previous: {eval_traj(cam_traj, join_time)}")

    tl, tr = time.time(), time.time() + JOIN_TIME

    yl, yr = cam_traj(tl), surf_traj(tr)
    dl, dr = cam_traj.derivative()(tl), surf_traj.derivative()(tr)
    cam_traj = CubicHermiteSpline([tl, tr], [yl, yr], [dl, dr])
    # print(f"Cam traj new: {eval_traj(cam_traj, join_time)}")


def process_image(ns, picam, model):
    global cam_traj, X, Y, streak_frame_id
    cap_pos = ns.cam_pos
    frame = picam.capture_array()
    frame = cv2.resize(frame, (IMG_W, IMG_H), interpolation=cv2.INTER_AREA)
    frame = normalize_frame(frame)
    inference_times.append(time.time())
    if len(inference_times) > 1:
        print(f"Infer FPS: {(len(inference_times) - 1.0) / (inference_times[-1] - inference_times[0]):.1f}")

    print(f"Capturing at {cap_pos}")

    res = model.predict(frame, verbose=False, classes=target_classes, conf=0.6)[0]

    if len(res.boxes.conf) > 0:
        targ = np.argmax(list(res.boxes.conf))
        b_x, b_y, b_w, b_h = res.boxes.xywhn[targ][:4]

        ang_dif = math.atan2((float(b_x) - 0.5), CAM_DEPTH)
        dsteps = -round(STEPS_PER_REV * ang_dif / (2 * math.pi))
        # dsteps //= 2
        obj_pos = cap_pos + dsteps

        if draw:
            x1 = int((b_x - b_w / 2) * IMG_W)
            y1 = int((b_y - b_h / 2) * IMG_H)
            w1 = int(b_w * IMG_W)
            h1 = int(b_h * IMG_H)
            cv2.rectangle(frame, (x1, y1), (x1 + w1, y1 + h1), (0, 255, 0), 2)
            cv2.putText(frame, f"Obj at {obj_pos:.0f}", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

        # print(res.probs)
        # for (id, prob) in zip(res.names, res.probs):
        #     print(id, prob)
        # print(f'Angular difference: {ang_dif}')
        print(f'Found at {b_x}. Object pos: {obj_pos}')
        X.append(float(time.time()))
        Y.append(float(obj_pos))
        update_traj(ns)
        print("Planned trajectories:")
        print(f"Surf traj: {eval_traj(surf_traj, JOIN_TIME)}")
        print(f"Cam  traj: {eval_traj(cam_traj, JOIN_TIME)}")

        ns.last_det = time.time()
    else:
        print("Nothing found")
        if time.time() - ns.last_det > LOSE_TIME:
            streak_frame_id = 0
            print("Lost target")
            X, Y = [], []
            if time.time() - ns.last_det <= LOSE_TIME + 5:
                cam_traj = CubicSpline([0, 1], [ns.cam_pos, ns.cam_pos])
            else:
                cam_traj = CubicSpline([0, 1], [0., 0.])

    ns.cam_traj_knots = cam_traj.x
    ns.cam_traj_coeffs = cam_traj.c

    if draw:
        cv2.imwrite(f"images/Last.jpg", frame)
        cv2.imwrite(f"images/YOLOchka_{streak_frame_id}.jpg", frame)
        # cv2.imshow("frame", frame)
        streak_frame_id += 1


def inference_process(ns):
    p = psutil.Process(os.getpid())
    p.cpu_affinity([2, 3])

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

    ns.cam_traj_knots = cam_traj.x
    ns.cam_traj_coeffs = cam_traj.c

    # model = YOLO("models/yolo11n_ncnn_model/", task="detect")
    model = YOLO("models/people_sub_3_ncnn_model/", task="detect")
    print("Модель загружена")
    run_time = time.time()
    while True:
        if time.time() - run_time > 0.5:
            process_image(ns, picam, model)
        else:
            time.sleep(0.1)



if __name__ == "__main__":
    mp.set_start_method('fork')  # на Raspberry Pi обычно 'fork' работает лучше

    mgr = mp.Manager()
    ns = mgr.Namespace()

    ns.last_det = time.time() - 10000
    ns.cam_pos = 0
    ns.cam_traj_knots = [0, 1]
    ns.cam_traj_coeffs = [[0], [0]]

    p1 = mp.Process(target=inference_process, name="InferenceProcess", args=(ns,))
    p2 = mp.Process(target=motor_driver_process, name="MotorDriverProcess", args=(ns,))

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
