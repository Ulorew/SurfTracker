from ultralytics import YOLO

# Load a pretrained YOLO11n model
model = YOLO("YOLO_Surf/SurfTracker-4/runs/detect/surftracker_45/weights/people_sub_2.pt")

# Run inference on 'bus.jpg' with arguments
model.predict("YOLO_Surf/VID_20230710_153350.mp4", save=True, imgsz=640, conf=0.5, show=True)