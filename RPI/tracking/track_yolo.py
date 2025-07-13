from picamera2 import Picamera2
import libcamera
import datetime
from ultralytics import YOLO
import serial
import numpy as np
import time
import math
from collections import deque

FOV = (54 / 180) * math.pi
steps_per_rev = 3200
# Initialize the Picamera2
picam2 = Picamera2()
# picam2.preview_configuration.main.size = (1280, 720)
# picam2.preview_configuration.main.format = "RGB888"
# picam2.preview_configuration.align()
# picam2.configure("preview")
config = picam2.create_preview_configuration(transform=libcamera.Transform(hflip=1, vflip=1),
                                             main={"size": (800, 600), "format": "RGB888"})

# config.main.transform=
# config.main.size = (800, 450)
# config.main.format = "RGB888"
# config.align()
picam2.configure(config)
picam2.start()

# Load the YOLO11 model
# model = YOLO("yolo11n.pt")
# model = YOLO("yolo11n_openvino_model/")
model = YOLO("yolo11n_ncnn_model/")
start_tm = datetime.datetime.now()

inference_times = deque(maxlen=5)
ser = serial.Serial('/dev/ttyACM0', 115200, timeout=1)
ser.reset_input_buffer()
cur_pos = 0

fps = 0

while True:
    if len(inference_times) > 0:
        fps = len(inference_times) / (time.time() - inference_times[0])
        print(f"FPS: {fps}")

    inference_times.append(time.time())
    if ser.in_waiting > 30:
        text = str(ser.read_all(), encoding='utf-8')
        text = text.split()
        if len(text) < 4:
            break

        print('read', text[-3])
        cur_pos = int(text[-3])

    fps = round((1000000) / (datetime.datetime.now() - start_tm).microseconds, 1)
    # print(f"fps: {fps}")
    start_tm = datetime.datetime.now()
    # Capture frame-by-frame
    frame = picam2.capture_array()

    res = model.track(frame, persist=True, classes=[39])[0]

    if len(res.boxes.conf) > 0:
        targ = np.argmax(list(res.boxes.conf))
        pos = res.boxes.xywhn[targ][:2]
        ang_dif = -(float(pos[0]) - 0.5) * FOV
        dsteps = round(steps_per_rev * ang_dif / (2 * math.pi))
        dsteps //= 2
        new_pos = cur_pos + dsteps

        # print(res.probs)
        # for (id, prob) in zip(res.names, res.probs):
        #     print(id, prob)
        print('Found at', pos, '  Rotating by', dsteps, 'to', new_pos)
        ser.write(str(new_pos).encode('utf-8'))

# https://roboticsbackend.com/raspberry-pi-arduino-serial-communication/#Serial_via_USB
# !/usr/bin/env python3

# npos=posf()
# ser.write(str(npos).encode('utf-8'))
# print('wrote', npos)
# if ser.in_waiting>0:
#     while ser.in_waiting>0:
#         line = ser.readline()
#     line=str(line, encoding='utf-8').rstrip()
#     print('read', line)
# time.sleep(1)
# time.sleep(0.03)
