import os, random, shutil

random.seed(0)
dataset_path = "YOLO_Surf/Data/People/Ueno640"
subdir="valid"
src_imgs = f"{dataset_path}/{subdir}/images"
src_lbls = f"{dataset_path}/{subdir}/labels"
dst_imgs = f"{dataset_path}_sub/{subdir}/images"
dst_lbls = f"{dataset_path}_sub/{subdir}/labels"

os.makedirs(dst_imgs, exist_ok=True)
os.makedirs(dst_lbls, exist_ok=True)

all_files = [f for f in os.listdir(src_imgs) if f.endswith(".jpg")]
sample = random.sample(all_files, int(len(all_files) * 0.333))  # 50%

for fn in sample:
    shutil.copy(os.path.join(src_imgs, fn), os.path.join(dst_imgs, fn))
    lbl = fn.replace(".jpg", ".txt")
    shutil.copy(os.path.join(src_lbls, lbl), os.path.join(dst_lbls, lbl))
