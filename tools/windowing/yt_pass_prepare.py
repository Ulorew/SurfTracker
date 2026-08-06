#!/usr/bin/env python3
"""Отбор ютубных видео для прохода (тикет "ночь", блок 2).

Три отсева, каждый найден валидацией:
  - ДУБЛИКАТЫ по содержимому: PWA_00_45.mp4 и PWA_02_45.mp4 побайтово
    идентичны, и без отсева их 5 минут вошли бы в статистику дважды;
  - НЕДЕКОДИРУЕМЫЕ: два 4K-файла в кодеке AV1 локальная сборка OpenCV не
    открывает вовсе, и прогон падал бы SystemExit ДО печати шапки — то есть
    молча, не дав ни одной цифры;
  - .part-файлы недокачанных загрузок.

Выводит json со списком годных и, отдельно, со списком отсеянных с причиной:
отсев обязан быть виден в отчёте, а не превращать 174 минуты в 145 без
объяснения.
"""
import hashlib
import json
import os
import subprocess
import sys

import cv2


def probe_duration(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=60,
                           env={**os.environ, "LC_ALL": "C"})
        return float(r.stdout.strip() or 0.0)
    except Exception:
        return 0.0


def head_hash(path, n=8 << 20):
    """Хеш первых 8 МБ: полный md5 на 8 ГБ занял бы минуты, а совпадение
    начала у разных видео практически исключено."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read(n))
    return h.hexdigest()


def main():
    roots = sys.argv[1:] or ["Data/videos/YT_1", "Data/videos/YT_SH", "Data/YT_1"]
    files = []
    for root in roots:
        for dirpath, _, names in os.walk(root):
            for n in sorted(names):
                if n.lower().endswith(".mp4") and "_annotated" not in dirpath:
                    files.append(os.path.join(dirpath, n))

    good, skipped, seen = [], [], {}
    for f in sorted(files):
        dur = probe_duration(f)
        hh = head_hash(f)
        if hh in seen:
            skipped.append({"файл": f, "причина": "дубликат", "совпал_с": seen[hh],
                            "минут": round(dur / 60, 2)})
            continue
        cap = cv2.VideoCapture(f)
        ok, _ = cap.read()
        cap.release()
        if not ok:
            skipped.append({"файл": f, "причина": "не декодируется (кодек)",
                            "минут": round(dur / 60, 2)})
            continue
        seen[hh] = f
        good.append({"файл": f, "минут": round(dur / 60, 2)})

    out = {"годных": good, "отсеяно": skipped,
           "минут_годных": round(sum(g["минут"] for g in good), 1),
           "минут_отсеяно": round(sum(s["минут"] for s in skipped), 1)}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
