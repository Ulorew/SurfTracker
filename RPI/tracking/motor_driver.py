import time
import serial

ser = serial.Serial('/dev/ttyACM0', 115200, timeout=0.1, write_timeout=0.1)
ser.reset_input_buffer()
ser.write("P0\n".encode())

def upd_cur_pos():
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


# def set_goal(ser, goal):
#     goal = int(round(goal))
#     try:
#         ser.write(f"{goal}\n".encode())
#     except serial.serialutil.SerialTimeoutException as e:
#         print("Serial write timed out")

def set_traj(A, B, C, D): # y = A dt^3 + B dt^2 + C dt + D
    msg=f"A{A:0.2f}\nB{B:0.2f}\nC{C:0.2f}\nD{D:0.2f}\n"

    print(f"New cam trajectory:\n{msg}", end='')
    ser.write(msg.encode())


# def motor_driver_process(ns):
#     # p = psutil.Process(os.getpid())
#     # p.cpu_affinity([0, 1])
#
#
#
#     print("Starting serial monitoring")
#     ser_upd_times = deque(maxlen=1000)
#     last_msg_time = time.time()
#     sum_lag = 0.
#     silent_iter = 0
#
#     while True:
#         ser_upd_times.append(time.time())
#         if len(ser_upd_times) > 1 and time.time() - last_msg_time >= 1:
#             IPS = (len(ser_upd_times) - 1.0) / (ser_upd_times[-1] - ser_upd_times[0])
#             avg_lag = sum_lag / silent_iter
#             print(f"Serial update IPS: {IPS:.0f} | Avg lag: {lag:.1f}")
#             sum_lag = 0
#             silent_iter = 0
#             last_msg_time = time.time()
#
#         ns.cam_pos = upd_cur_pos(ser)
#         cam_traj = PPoly(ns.cam_traj_coeffs, ns.cam_traj_knots)
#         # print(f"Got cam traj: {eval_traj(cam_traj, join_time)}")
#         loop_start = time.time()
#         goal = float(cam_traj(loop_start))
#
#         silent_iter += 1
#         lag = abs(goal - ns.cam_pos)
#         if lag > 300:
#             print(f"Lag: {lag}! Pos: {ns.cam_pos}, Goal: {goal}")
#         sum_lag += abs(goal - ns.cam_pos)
#         # print(f"Going to {goal}, {type(goal)}")
#         set_goal(ser, goal)
#
#         sleep_time = max(0., SERIAL_UPD_TIME - (time.time() - loop_start))
#         time.sleep(sleep_time)
