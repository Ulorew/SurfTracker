import math
import time
from collections import deque

from ultralytics import YOLO
from scipy.interpolate import CubicSpline, CubicHermiteSpline
from time import perf_counter
from phone_communication.full_communication import *

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


class TrajectoryPlanner:
    def __init__(self, join_time, process_var=250., measure_var=1.):
        self.join_time = join_time
        self.X = []
        self.Y = []
        self.prey_traj = CubicSpline([0, 1], [0, 0])
        self.hunter_traj = CubicSpline([0, 1], [0, 0])
        self.kalman = Kalman1D(0, 0, process_var, measure_var)

    def update(self, x, y):
        self.X.append(x)
        self.Y.append(y)
        xl, xr = x, x + JOIN_TIME
        traj_info = self.kalman.step(y, x)
        pos, vel = traj_info.squeeze()
        self.prey_traj = CubicSpline([x, x + 1], [pos, pos + vel])
        yl, yr = self.hunter_traj(xl), self.prey_traj(xr)
        dl, dr = self.hunter_traj.derivative()(xl), self.prey_traj.derivative()(xr)
        self.hunter_traj = CubicHermiteSpline([xl, xr], [yl, yr], [dl, dr])

    def reset(self, prey_pos=0., hunter_pos=0.):
        self.kalman.reset(pos0=prey_pos, t0=time.perf_counter() - start_time)

        self.prey_traj = CubicSpline([0., 1.], [prey_pos, prey_pos])
        self.hunter_traj = CubicSpline([0., 1.], [hunter_pos, hunter_pos])


# def init_camera():
#     global picam
#     picam = Picamera2()
#     # create_video_configuration(main={"size": (1920, 1080)}, lores={"size": (640, 480)}, display="lores")
#     config = picam.create_video_configuration(
#         transform=libcamera.Transform(hflip=1, vflip=1),
#         main={"size": (CAM_W, CAM_H), "format": "RGB888"},
#         # lores={"size": (640, 480)},
#         controls={
#             # "FrameDurationLimits": (100000//5, 300000//5),
#             # "AnalogueGain": 1.0,
#             # "AwbEnable": True,
#             "ExposureTime": 20000,  # 10 мс (1/100 секунд) — уменьшает размытие
#             # "AnalogueGain": 2.5,  # ISO ~ 2.5 * базового — баланс шум/светочувствительность
#             "AwbEnable": True  # авто-баланс белого
#         }
#     )
#     picam.configure(config)
#     encoder = H264Encoder(bitrate=10000000)
#     output = "images/video.h264"
#     picam.start_recording(encoder, output)
#     print("Камера запущена")


SHARPEN_KERNEL = np.array([[0, -1, 0],
                           [-1, 5, -1],
                           [0, -1, 0]], dtype=np.float32)


def normalize_frame(frame):
    yuv = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV)
    # эквализуем Y-канал
    yuv[:, :, 0] = cv2.equalizeHist(yuv[:, :, 0])
    eq = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)

    denoised = cv2.GaussianBlur(eq, (3, 3), sigmaX=0.5)

    gauss = cv2.GaussianBlur(denoised, (0, 0), sigmaX=1.0)
    sharpened = cv2.addWeighted(denoised, 1.3, gauss, -0.3, 0)
    return sharpened


# def take_photo():
#     frame = picam.capture_array()
#     if len(frame) != IMG_W or len(frame[0]) != IMG_H:
#         frame = cv2.resize(frame, (IMG_W, IMG_H), interpolation=cv2.INTER_LINEAR)
#     # frame[:IMG_H//2, :] = normalize_frame(frame[:IMG_H//2, :])
#     frame = normalize_frame(frame)
#     return frame


IMG_W, IMG_H = 640, 480
CAM_FOV = (72 / 180) * math.pi
# REC_FOV = np.arctan(IMG_W /)
CAM_DEPTH = (0.5) / math.tan(CAM_FOV / 2)  # H/w
JOIN_TIME = 2.
LOSE_TIME = 2.
WAIT_TIME = 3.
SHOT_OFFSET = 0.030
CAM_PRED_COEF = 0.0
DRAW = False

STEPS_PER_REV = 6600 * 4

YOLO_CONF_TH = 0.5
MIN_SWITCH_CONF = 0.6

# kalman = Kalman1D(0, 0, 250, 1)
yolo = None

streak_frame_id = 0

logging.info(f"Working in resolution {IMG_W}x{IMG_H}")
logging.info(f"FOV: {CAM_FOV} rad, cam_depth: {CAM_DEPTH}")
logging.info(f"Join time: {JOIN_TIME}")

last_det = 0
last_yolo_infer = time.perf_counter() - 10000

start_time = None

target_classes = [0]
target_id = None
inference_times = deque(maxlen=10)
last_seen_pos = 0
cam_planner = TrajectoryPlanner(join_time=JOIN_TIME, process_var=250., measure_var=1.)


def eval_traj(traj, dur):
    tm = time.perf_counter() - start_time
    t = np.linspace(tm, tm + dur, 6)
    return ', '.join([f"{float(traj(i)):0.1f}" for i in t])


def poly_coeffs(poly, x, deg=3):
    answer = []
    mult = 1.
    for i in range(deg + 1):
        answer.append(poly(x) / mult)
        poly = poly.derivative()
        # mult *= (i + 1)
    return answer


def _to_numpy(x):
    """Универсальное безопасное чтение тензора / списка в numpy array"""
    try:
        return x.cpu().numpy()
    except Exception:
        return np.array(x)


def track(frame):
    global last_yolo_infer, last_det, target_id
    switch = False
    # пометим время вызова инференса (абсолютное perf_counter)
    last_yolo_infer = time.perf_counter()
    logging.info("Tracking with YOLO")

    with catchtime(name="YOLO tracker"):
        det = yolo.track(frame, verbose=False, persist=True, classes=target_classes, conf=YOLO_CONF_TH)[0]

    # безопасно получить confs / ids / xywhn как numpy
    try:
        confs = _to_numpy(det.boxes.conf)
    except Exception:
        confs = np.array([])

    if confs.size == 0 or det.boxes.id is None:
        if confs.size > 0:
            logging.debug("Ids are not ready, but there are boxes. Still skipping")
        # нет боксов
        return (None, None, None, None), False

    ids = _to_numpy(det.boxes.id)
    xywhn = _to_numpy(det.boxes.xywhn)  # shape (N,4)

    # защита: ids может быть float / negative. Приведём к int-list для сравнения
    try:
        ids_list = [int(x) for x in ids]
    except Exception:
        ids_list = list(ids.astype(int))

    # если цели нет (target_id==None) — сразу выбрать самый confident
    if target_id is None:
        idx = int(np.argmax(confs))
        if confs[idx] >= MIN_SWITCH_CONF:
            target_id = int(ids_list[idx])
            last_det = last_yolo_infer
            switch = True
            logging.info(f"Initial target -> id={target_id} conf={float(confs[idx]):.3f}")
        else:
            # самый уверенный слишком слабый — не брать
            return (None, None, None, None), False
    else:
        # если текущий id есть в новых треках
        if target_id in ids_list:
            idx = ids_list.index(target_id)
            last_det = last_yolo_infer
            # продолжаем отслеживать этот id
        else:
            # текущий id отсутствует на кадре
            time_since_last_det = last_yolo_infer - last_det
            if time_since_last_det > LOSE_TIME:
                # переключаемся на наиболее уверенный (если он достаточно уверенный)
                idx_best = int(np.argmax(confs))
                if confs[idx_best] >= MIN_SWITCH_CONF:
                    target_id = int(ids_list[idx_best])
                    idx = idx_best
                    last_det = last_yolo_infer
                    print(f"Switched target -> id={target_id} conf={float(confs[idx]):.3f}")
                else:
                    # нет достойной цели
                    return (None, None, None, None), True
            else:
                # цель временно пропала — не переключаемся; возвращаем None для краткого loss
                print(f"Target {target_id} missing but within LOSE_TIME ({time_since_last_det:.3f}s).")
                return (None, None, None, None), False

    # извлекаем bbox (нормализованные)
    b_x, b_y, b_w, b_h = xywhn[idx][:4].astype(float)
    return (b_x, b_y, b_w, b_h), switch


def process_image(frame):
    global streak_frame_id, last_det, last_seen_pos
    infer_time = time.perf_counter() - start_time
    cap_time = infer_time - SHOT_OFFSET
    cur_pos = upd_cur_pos()
    pred_pos = max(min(cam_planner.hunter_traj(cap_time), STEPS_PER_REV / 4), -STEPS_PER_REV / 4)
    cap_pos = cur_pos * (1 - CAM_PRED_COEF) + pred_pos * CAM_PRED_COEF

    inference_times.append(time.perf_counter())
    if len(inference_times) > 1:
        logging.info(f"Infer FPS: {(len(inference_times) - 1.0) / (inference_times[-1] - inference_times[0]):.1f}")

    logging.info(f"Capturing at {cap_pos}, {cap_time:.2f} s")

    (b_x, b_y, b_w, b_h), switch = track(frame)

    if b_x is not None:
        ang_dif = math.atan2((float(b_x) - 0.5), CAM_DEPTH)
        dsteps = round(STEPS_PER_REV * ang_dif / (2 * math.pi))
        obj_pos = cap_pos + dsteps

        last_seen_pos = obj_pos
        last_det = cap_time

        if DRAW:
            x1, y1 = int((b_x - b_w / 2) * IMG_W), int((b_y - b_h / 2) * IMG_H)
            w1, h1 = int(b_w * IMG_W), int(b_h * IMG_H)
            cv2.rectangle(frame, (x1, y1), (x1 + w1, y1 + h1), (0, 255, 0), 2)
            cv2.putText(frame, f"Obj at {obj_pos:.0f}", (200, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

        logging.info(f'Found at {b_x}. Object pos: {obj_pos}')

        if switch:
            cam_planner.reset(prey_pos=obj_pos, hunter_pos=cap_pos)
        cam_planner.update(float(cap_time), float(obj_pos))
        logging.info("Planned trajectories:")
        logging.info(f"Surf traj: {eval_traj(cam_planner.prey_traj, JOIN_TIME)}")
        logging.info(f"Cam  traj: {eval_traj(cam_planner.hunter_traj, JOIN_TIME)}")
    else:
        logging.info("Nothing found")
        if cap_time - last_det > LOSE_TIME:
            streak_frame_id = 0
            logging.info("Lost target")

            if cap_time - last_det <= LOSE_TIME + WAIT_TIME:
                cam_planner.reset(prey_pos=last_seen_pos)
                logging.info("Waiting on last seen pos")
            else:
                cam_planner.reset(prey_pos=0)
                logging.info("Returning to home")
        else:
            logging.info("following predicted trajectories")
    upd_cur_pos()
    set_traj(*poly_coeffs(cam_planner.hunter_traj, x=cap_time, deg=3))

    if DRAW:
        with catchtime(name="Drawing&Saving"):
            cv2.putText(frame, f"Cam at {cap_pos:.0f}", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            cv2.putText(frame, f"Time: {cap_time:.2f} s", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            cv2.imwrite(f"images/Last.png", frame)
            cv2.imwrite(f"images/YOLOchka_{streak_frame_id}.png", frame)
            # cv2.imshow("frame", frame)
            streak_frame_id += 1
    logging.info(f"Processed image in {(time.perf_counter() - start_time - infer_time) * 1000:.0f} ms")


def frame_meta_callback(data):
    global CAM_FOV, CAM_DEPTH
    CAM_FOV = (data.get("hfov") / 180) * math.pi
    CAM_DEPTH = (0.5) / math.tan(CAM_FOV / 2)  # H/w
    logging.debug(f"New CAM FOV: {CAM_FOV:.2f} rad, Depth: {CAM_DEPTH:.2f}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler("surftracker.log"),  # пишем в файл
            logging.StreamHandler()  # и в консоль
        ],
        force=True
    )
    connect_serial()
    init_pos()

    start_time = time.perf_counter()
    last_det = - 100
    set_traj(0, 0, 0, 0)
    # model = YOLO("models/yolo11n_ncnn_model/", task="detect")
    yolo = YOLO("models/people_sub_3_ncnn_model/")
    logging.info("Модель загружена")
    run_time = time.time()

    register_camera_callback(process_image)
    register_meta_callback(frame_meta_callback)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Interrupted by user — exiting")
    finally:
        logging.info("That's all, folks!")
