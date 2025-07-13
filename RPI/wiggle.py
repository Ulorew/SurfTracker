from picamera2 import Picamera2
import libcamera
import datetime
from ultralytics import YOLO
import serial
import numpy as np
import time
import math
from collections import deque

ser = serial.Serial('/dev/ttyACM0', 115200, timeout=1)
ser.reset_input_buffer()


def send_int(value):
    data = f"{value}\n".encode('ascii')
    ser.write(data)
    ser.flush()

while True:
    send_int(32123)
    time.sleep(0.1)

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
