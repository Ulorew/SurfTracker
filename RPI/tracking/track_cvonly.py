import cv2
import libcamera
import numpy as np
from picamera2 import Picamera2

IMG_W, IMG_H = 640, 480
picam = None
clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))

def init_camera():
    global picam
    picam = Picamera2()
    cam_w, cam_h = picam.sensor_resolution

    config = picam.create_preview_configuration(
        transform=libcamera.Transform(hflip=1, vflip=1),
        main={"size": (cam_w, cam_h), "format": "RGB888"},
        controls={
            "ExposureTime": 20000,
            "AwbEnable": True
        }
    )
    picam.configure(config)
    picam.start()
    print("Камера запущена")

def normalize_frame(frame):
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    L, A, B = cv2.split(lab)
    L_eq = clahe.apply(L)
    lab_eq = cv2.merge((L_eq, A, B))
    eq_bgr = cv2.cvtColor(lab_eq, cv2.COLOR_LAB2BGR)
    denoised = cv2.bilateralFilter(eq_bgr, d=5, sigmaColor=75, sigmaSpace=75)
    gaussian = cv2.GaussianBlur(denoised, (0, 0), sigmaX=1.0)
    sharpened = cv2.addWeighted(denoised, 1.5, gaussian, -0.5, 0)
    return sharpened


def take_photo():
    frame = picam.capture_array()
    frame = cv2.resize(frame, (IMG_W, IMG_H), interpolation=cv2.INTER_AREA)
    return normalize_frame(frame)

init_camera()

# Получение первого кадра и выбор области интереса
frame = take_photo()
bbox = cv2.selectROI('select', frame, False)
cv2.destroyWindow('select')

# Инициализация трекера KCF
tracker = cv2.TrackerKCF_create()
tracker.init(frame, bbox)

# Основной цикл захвата и трекинга
while True:
    frame = take_photo()
    success, bbox = tracker.update(frame)
    if success:
        x, y, w, h = map(int, bbox)
        cv2.rectangle(frame, (x, y), (x + w, y + h), (255, 0, 0), 2)
    else:
        cv2.putText(frame, 'Tracking failure', (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)

    cv2.imshow('KCF Tracker', frame)

    if cv2.waitKey(30) & 0xFF == 27:  # Esc key to exit
        break

cv2.destroyAllWindows()
