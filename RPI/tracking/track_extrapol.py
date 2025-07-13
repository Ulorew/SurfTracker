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
from scipy.interpolate import CubicSpline, CubicHermiteSpline, PPoly
from time import perf_counter


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


img_w, img_h = 640, 480
FOV = (63.5 / 180) * math.pi
cam_depth = (0.5) / math.tan(FOV / 2)  # H/w
join_time = 1.5
lose_time = 2.
ser_upd_time = 0.01

streak_frame_id = 0

print(f"Working in resolution {img_w}x{img_h}")
print(f"FOV: {FOV} rad, cam_depth: {cam_depth}")
print(f"Join time: {join_time}")

steps_per_rev = 8800

surf_traj = CubicSpline([0, 1], [0, 0])
cam_traj = CubicSpline([0, 1], [0, 0])
X, Y = [], []

target_classes = [0]
draw = True
inference_times = deque(maxlen=5)


def upd_cur_pos(ser):
    st = time.time()
    lns = ser.read_all().decode().split('\n')
    if time.time() - st > 0.2:
        print(f"I've been reading for {time.time() - st:.2f} seconds!")
    if len(lns) <= 2:
        print("Unable to get current position! Not enough position marks from arduino")
        print(f"lns = {lns}")
        return 0
    cur_pos = int(lns[-2])
    # print(f"Read {cur_pos}")
    return cur_pos


def set_goal(ser, goal):
    goal = int(round(goal))
    try:
        ser.write(f"{goal}\n".encode())
    except serial.serialutil.SerialTimeoutException as e:
        print("Serial write timed out")


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

    tl, tr = time.time(), time.time() + join_time

    yl, yr = cam_traj(tl), surf_traj(tr)
    dl, dr = cam_traj.derivative()(tl), surf_traj.derivative()(tr)
    cam_traj = CubicHermiteSpline([tl, tr], [yl, yr], [dl, dr])
    # print(f"Cam traj new: {eval_traj(cam_traj, join_time)}")


def process_image(ns, picam, model):
    global cam_traj, X, Y, streak_frame_id
    cap_pos = ns.cam_pos
    frame = picam.capture_array()
    frame = cv2.resize(frame, (img_w, img_h), interpolation=cv2.INTER_AREA)
    frame = normalize_frame(frame)
    inference_times.append(time.time())
    if len(inference_times) > 1:
        print(f"Infer FPS: {(len(inference_times) - 1.0) / (inference_times[-1] - inference_times[0]):.1f}")

    print(f"Capturing at {cap_pos}")

    res = model.predict(frame, verbose=False, classes=target_classes, conf=0.6)[0]

    if len(res.boxes.conf) > 0:
        targ = np.argmax(list(res.boxes.conf))
        b_x, b_y, b_w, b_h = res.boxes.xywhn[targ][:4]

        ang_dif = math.atan2((float(b_x) - 0.5), cam_depth)
        dsteps = -round(steps_per_rev * ang_dif / (2 * math.pi))
        # dsteps //= 2
        obj_pos = cap_pos + dsteps

        if draw:
            x1 = int((b_x - b_w / 2) * img_w)
            y1 = int((b_y - b_h / 2) * img_h)
            w1 = int(b_w * img_w)
            h1 = int(b_h * img_h)
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
        print(f"Surf traj: {eval_traj(surf_traj, join_time)}")
        print(f"Cam  traj: {eval_traj(cam_traj, join_time)}")

        ns.last_det = time.time()
    else:
        print("Nothing found")
        if time.time() - ns.last_det > lose_time:
            streak_frame_id = 0
            print("Lost target")
            X, Y = [], []
            if time.time() - ns.last_det <= lose_time + 5:
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


def arduino_communication_process(ns):
    # p = psutil.Process(os.getpid())
    # p.cpu_affinity([0, 1])

    ser = serial.Serial('/dev/ttyACM0', 115200, timeout=0.1, write_timeout=0.1)
    ser.reset_input_buffer()
    ser.write("0\n".encode())

    print("Starting serial monitoring")
    ser_upd_times = deque(maxlen=1000)
    last_msg_time = time.time()
    sum_lag = 0.
    silent_iter = 0

    while True:
        ser_upd_times.append(time.time())
        if len(ser_upd_times) > 1 and time.time() - last_msg_time >= 1:
            IPS = (len(ser_upd_times) - 1.0) / (ser_upd_times[-1] - ser_upd_times[0])
            avg_lag = sum_lag / silent_iter
            print(f"Serial update IPS: {IPS:.0f} | Avg lag: {lag:.1f}")
            sum_lag = 0
            silent_iter = 0
            last_msg_time = time.time()

        ns.cam_pos = upd_cur_pos(ser)
        cam_traj = PPoly(ns.cam_traj_coeffs, ns.cam_traj_knots)
        # print(f"Got cam traj: {eval_traj(cam_traj, join_time)}")
        loop_start = time.time()
        goal = float(cam_traj(loop_start))

        silent_iter += 1
        lag = abs(goal - ns.cam_pos)
        if lag > 300:
            print(f"Lag: {lag}! Pos: {ns.cam_pos}, Goal: {goal}")
        sum_lag += abs(goal - ns.cam_pos)
        # print(f"Going to {goal}, {type(goal)}")
        set_goal(ser, goal)

        sleep_time = max(0., ser_upd_time - (time.time() - loop_start))
        time.sleep(sleep_time)


if __name__ == "__main__":
    mp.set_start_method('fork')  # на Raspberry Pi обычно 'fork' работает лучше

    mgr = mp.Manager()
    ns = mgr.Namespace()

    ns.last_det = time.time() - 10000
    ns.cam_pos = 0
    ns.cam_traj_knots = [0, 1]
    ns.cam_traj_coeffs = [[0], [0]]

    p1 = mp.Process(target=inference_process, name="InferenceProcess", args=(ns,))
    p2 = mp.Process(target=arduino_communication_process, name="ArduinoCommunicationProcess", args=(ns,))

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
