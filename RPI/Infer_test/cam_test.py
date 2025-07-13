import time
from time import sleep
from picamera2 import Picamera2
import libcamera
import cv2

# 1. Инициализация камеры и конфигурация
picam = Picamera2()
cam_w, cam_h = picam.sensor_resolution  # обычно 4056×3040 для IMX219
cam_w //= 4
cam_h //= 4

config = picam.create_preview_configuration(
    transform=libcamera.Transform(hflip=1, vflip=1),
    main={"size": (cam_w, cam_h), "format": "RGB888"}
)

picam.configure(config)
picam.start()

# 2. Дадим камере немного времени на автоэкспозицию и автофокус
sleep(1.0)

# 3. Захват одного кадра из буфера "main"
start_time = time.time()
iter = 100
for i in range(iter):
    full = picam.capture_array()
    compr = cv2.resize(full, (640, 480), interpolation=cv2.INTER_AREA)
total_time = time.time() - start_time
print(f"{iter / total_time:.1f} FPS")

# 4. Конвертация из RGB в BGR для OpenCV
# frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)

# 5. Показ изображения в окне
cv2.imshow("Photo", compr)
cv2.waitKey(0)  # ждём любую клавишу

# (опционально) Сохраняем снимок в файл
cv2.imwrite("photo.jpg", compr)

# 6. Освобождаем ресурсы
cv2.destroyAllWindows()
picam.stop()
