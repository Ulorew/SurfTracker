import math
import time

import serial

ser = None 
try:
    ser = serial.Serial('/dev/ttyACM0', 115200, timeout=0.1, write_timeout=0.1)
    ser.reset_input_buffer()
    ser.write("P0\n".encode())
except serial.serialutil.SerialException as e:
    try:
        ser = serial.Serial('/dev/ttyACM1', 115200, timeout=0.1, write_timeout=0.1)
        ser.reset_input_buffer()
        ser.write("P0\n".encode())
    except serial.serialutil.SerialException as e:
        print("Could not connect to Arduino serial port")


def send_pos(value):
    data = f"P{value:0.2f}\n".encode()
    ser.read_all()
    ser.write(data)
    ser.flush()


AMPLITUDE = 4000
PERIOD_LEN = 10

while True:
    tm = time.time()
    send_pos(math.sin(tm / PERIOD_LEN * 2 * math.pi)*AMPLITUDE)
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
