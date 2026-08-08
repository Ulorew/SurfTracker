#!/usr/bin/env python3
"""Стенд сквозной сверки камерного зрения (тикет «камерное зрение», блок А).

Один процесс делает всё: показывает кадр на мониторе, командует телефону снять,
забирает артефакты. Разнесённые скрипты пришлось бы синхронизировать вручную, а
между показом и снимком не должно быть человека.

Порядок:
    stand.py calib    — показать шахматную доску, снять, посчитать гомографию
                        монитор -> сенсор (по ней координаты цели с кадра
                        пересчитываются в точку наведения кропа)
    stand.py shots    — восемь кадров: показать, навести кроп, снять, забрать
    stand.py verify   — три уровня сверки на ноутбуке (см. verify.py)

Экран занимается на время прогона: монитор один, и кадры показываются во весь
экран. Это оговорено с владельцем стенда до запуска.
"""
import json
import os
import subprocess
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
IMG = os.path.join(ROOT, "Datasets/dataset_v6/images/val")
LAB = os.path.join(ROOT, "Datasets/dataset_v6/labels/val")
OUT = os.path.join(ROOT, "tools/stand/out")
PKG = "com.surftracker.camfps"
REMOTE = "/sdcard/Android/data/%s/files/stand" % PKG
WIN = "stand"

# Экран 1920x1200, кадры 1920x1080 — ложатся 1:1 без пересэмплинга. Масштабировать
# было бы вредно: интерполяция монитора добавилась бы к тому, что мы измеряем.
SCREEN_W, SCREEN_H = 1920, 1200
FRAME_W, FRAME_H = 1920, 1080
OFF_X, OFF_Y = (SCREEN_W - FRAME_W) // 2, (SCREEN_H - FRAME_H) // 2

CHESS = (9, 6)          # внутренних углов
MARK_XY = (110, 110)    # чёрный диск в левом верхнем углу кадра — метка
MARK_R = 55             # асимметрии, вне поля доски
SETTLE_SEC = 1.2        # дать монитору отрисоваться и экспозиции устояться


def adb(*args, timeout=90):
    return subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)


def show(img):
    """Показать во весь экран, дождаться отрисовки."""
    canvas = np.zeros((SCREEN_H, SCREEN_W, 3), np.uint8)
    h, w = img.shape[:2]
    canvas[OFF_Y:OFF_Y + h, OFF_X:OFF_X + w] = img
    cv2.imshow(WIN, canvas)
    for _ in range(12):
        cv2.waitKey(30)
    time.sleep(SETTLE_SEC)


def open_window():
    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)


def capture(tag, cx=None, cy=None, hist=False, side=640, profile=False,
             reps=None, sides=None):
    """Снимок на телефоне. cx/cy — точка наведения в пикселях СЕНСОРА."""
    cmd = ["shell", "am", "start", "-n", f"{PKG}/.StandActivity", "--es", "tag", tag,
           "--ei", "side", str(side)]
    if cx is not None:
        cmd += ["--ei", "cx", str(int(cx)), "--ei", "cy", str(int(cy))]
    if hist:
        cmd += ["--ez", "hist", "true"]
    if profile:
        cmd += ["--ez", "profile", "true"]
        if reps is not None:
            cmd += ["--ei", "reps", str(int(reps))]
        if sides:
            cmd += ["--es", "sides", ",".join(str(x) for x in sides)]
    # Удалить ПРЕЖНИЕ артефакты до съёмки. Без этого ожидание "файл появился"
    # выполняется мгновенно на файле с прошлого прогона, снимок не ждётся, и
    # забираются старые данные — а числа выглядят правдоподобно. Ровно это и
    # произошло на первой калибровке: результат совпал с предыдущим до знака.
    adb("shell", "rm", "-f", *[f"{REMOTE}/{tag}{e}" for e in
                                (".json", ".rgb.png", ".yuvmeta.json", ".y", ".u", ".v",
                                 ".hist.json")])
    r = adb(*cmd)
    if r.returncode != 0:
        raise SystemExit(f"am start не прошёл: {r.stderr.strip()}")
    # Ждём json — он пишется последним, в блоке finally
    for _ in range(60):
        time.sleep(1)
        if adb("shell", "ls", f"{REMOTE}/{tag}.json").returncode == 0:
            time.sleep(0.5)
            return
    raise SystemExit(f"телефон не отдал {tag}.json за 60 с")


def pull(tag):
    os.makedirs(OUT, exist_ok=True)
    for ext in (".json", ".rgb.png", ".yuvmeta.json", ".y", ".u", ".v", ".hist.json"):
        adb("pull", f"{REMOTE}/{tag}{ext}", os.path.join(OUT, tag + ext), timeout=300)


def chessboard():
    """Доска на всю площадь кадра. Белое поле по краям — чтобы углы у самой
    границы находились надёжно."""
    cols, rows = CHESS[0] + 1, CHESS[1] + 1
    cell = min(FRAME_W // (cols + 2), FRAME_H // (rows + 2))
    board = np.zeros((rows * cell, cols * cell), np.uint8)
    for r in range(rows):
        for c in range(cols):
            if (r + c) % 2 == 0:
                board[r * cell:(r + 1) * cell, c * cell:(c + 1) * cell] = 255
    img = np.full((FRAME_H, FRAME_W), 255, np.uint8)
    y0 = (FRAME_H - board.shape[0]) // 2
    x0 = (FRAME_W - board.shape[1]) // 2
    img[y0:y0 + board.shape[0], x0:x0 + board.shape[1]] = board
    # МЕТКА АСИММЕТРИИ. Доска 9x6 симметрична относительно поворота на 180
    # градусов, и findChessboardCorners вернёт углы в обратном порядке, если
    # камера смотрит на экран перевёрнуто. Гомография при этом ляжет ИДЕАЛЬНО
    # (обе сетки согласованы), ошибка подгонки останется меньше пикселя, а
    # наведение будет бить в точку, отражённую через центр доски. Ошибку
    # подгонки этим не поймать в принципе — нужна асимметрия в самой картинке.
    cv2.circle(img, MARK_XY, MARK_R, 0, -1)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), (x0, y0, cell)


def cmd_calib():
    from yuv_host import to_rgb, y_stats
    open_window()
    img, (bx0, by0, cell) = chessboard()
    show(img)
    capture("calib", hist=True)
    cv2.destroyAllWindows()
    pull("calib")
    base = os.path.join(OUT, "calib")

    full = to_rgb(base)
    gray = cv2.cvtColor(full, cv2.COLOR_RGB2GRAY)
    ok, corners = cv2.findChessboardCorners(
        gray, CHESS, cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE)
    if not ok:
        cv2.imwrite(os.path.join(OUT, "calib_full.jpg"),
                    cv2.cvtColor(cv2.resize(full, (1020, 765)), cv2.COLOR_RGB2BGR))
        raise SystemExit("доска не найдена — смотрите out/calib_full.jpg: "
                          "монитор целиком в кадре? нет бликов? резкость?")
    corners = cv2.cornerSubPix(
        gray, corners, (11, 11), (-1, -1),
        (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.01))

    # Углы доски в координатах КАДРА (не экрана): именно в них задана разметка
    obj = np.array([[bx0 + (c + 1) * cell, by0 + (r + 1) * cell]
                    for r in range(CHESS[1]) for c in range(CHESS[0])], np.float32)
    Hm, mask = cv2.findHomography(obj, corners.reshape(-1, 2), cv2.RANSAC, 3.0)
    # Проверка ориентации по метке: где по гомографии должен быть чёрный диск,
    # там сенсор обязан быть тёмным, а в диагонально противоположной точке —
    # светлым. Если наоборот, углы пришли в обратном порядке.
    def brightness(H, xy):
        q = cv2.perspectiveTransform(np.array([[xy]], np.float32), H).reshape(-1)
        u, v = int(round(q[0])), int(round(q[1]))
        if not (0 <= u < gray.shape[1] and 0 <= v < gray.shape[0]):
            return None
        return float(gray[max(0, v - 8):v + 8, max(0, u - 8):u + 8].mean())

    opp = (FRAME_W - MARK_XY[0], FRAME_H - MARK_XY[1])
    b_mark, b_opp = brightness(Hm, MARK_XY), brightness(Hm, opp)
    flipped = False
    if b_mark is None or b_opp is None or b_mark >= b_opp:
        flipped = True
        Hm, mask = cv2.findHomography(obj, corners.reshape(-1, 2)[::-1], cv2.RANSAC, 3.0)
        corners = corners[::-1]
        b_mark, b_opp = brightness(Hm, MARK_XY), brightness(Hm, opp)
        if b_mark is None or b_opp is None or b_mark >= b_opp:
            raise SystemExit(f"метка не найдена ни в одной ориентации "
                              f"(метка {b_mark}, напротив {b_opp}) — смотрите out/calib_full.jpg")
    err = np.linalg.norm(
        cv2.perspectiveTransform(obj.reshape(-1, 1, 2), Hm).reshape(-1, 2)
        - corners.reshape(-1, 2), axis=1)
    ys = y_stats(base)
    res = {"H": Hm.tolist(), "n_corners": int(len(obj)),
            "ошибка_px": {"медиана": float(np.median(err)), "p95": float(np.percentile(err, 95)),
                           "макс": float(err.max())},
            "яркость_Y": ys,
            "масштаб_кадр_в_сенсор": float(np.sqrt(abs(np.linalg.det(Hm[:2, :2])))),
            "углы_развёрнуты": flipped,
            "яркость_метки": round(b_mark, 1), "яркость_напротив": round(b_opp, 1)}
    json.dump(res, open(os.path.join(OUT, "calib.result.json"), "w"),
              ensure_ascii=False, indent=1)
    print(json.dumps(res["ошибка_px"], ensure_ascii=False))
    print("яркость Y:", json.dumps(ys, ensure_ascii=False))
    print("масштаб кадр->сенсор:", round(res["масштаб_кадр_в_сенсор"], 3))
    print(f"ориентация: {'РАЗВЁРНУТА на 180 (углы переставлены)' if flipped else 'прямая'}; "
          f"метка {b_mark:.0f} против {b_opp:.0f} напротив")
    if res["ошибка_px"]["p95"] > 12:
        print("ВНИМАНИЕ: гомография неточная (p95 > 12 px) — наведение кропа "
              "будет мазать; проверьте геометрию стенда")


def frame_manifest():
    return json.load(open(os.path.join(HERE, "frames.json")))


def cmd_shots():
    calib = json.load(open(os.path.join(OUT, "calib.result.json")))
    Hm = np.array(calib["H"])
    man = frame_manifest()
    open_window()
    done = []
    for rec in man:
        img = cv2.imread(os.path.join(IMG, rec["file"]))
        if img is None:
            raise SystemExit("нет кадра: " + rec["file"])
        show(img)
        if rec["aim"] is None:            # пустые кадры — центр экрана
            pt = np.array([[FRAME_W / 2, FRAME_H / 2]], np.float32)
        else:
            pt = np.array([rec["aim"]], np.float32)
        sensor = cv2.perspectiveTransform(pt.reshape(-1, 1, 2), Hm).reshape(-1)
        capture(rec["tag"], cx=sensor[0], cy=sensor[1])
        done.append({**rec, "sensor_aim": [float(sensor[0]), float(sensor[1])]})
        print(f"{rec['tag']}: цель кадра {rec['aim']} -> сенсор "
              f"({sensor[0]:.0f},{sensor[1]:.0f})")
    cv2.destroyAllWindows()
    for rec in done:
        pull(rec["tag"])
    json.dump(done, open(os.path.join(OUT, "shots.json"), "w"), ensure_ascii=False, indent=1)
    print("снято:", len(done))


def cmd_profile():
    """Блок Б: профиль кропа по стадиям и по стороне окна.

    Сцена не важна для времени, но кадр всё равно показывается: пустой экран
    дал бы почти однородный YUV, а на однородных данных ветвления и кэш ведут
    себя не так, как на реальной картинке.

    Считаются ДВА режима:
      warm  — reps повторов по одному удержанному кадру (стадии различимы,
              но кэш прогрет);
      cold  — по одному замеру на СВЕЖИХ кадрах (как в бою), меньше точек.
    Разница между ними и есть цена прогретого кэша; прятать её нельзя.
    """
    import glob
    reps = 50
    sides = [640, 960, 1440, 2160, 3060]
    frames = json.load(open(os.path.join(HERE, "frames.json")))
    calib = os.path.join(OUT, "calib.result.json")   # гомография, а не json снимка
    aim = None
    if os.path.exists(calib):
        c = json.load(open(calib))
        H = np.array(c["H"], dtype=float)
        # наводим в центр монитора — там, где реально стоит цель
        pt = cv2.perspectiveTransform(
            np.array([[[OFF_X + FRAME_W / 2, OFF_Y + FRAME_H / 2]]], dtype=np.float64), H)[0][0]
        aim = (pt[0], pt[1])
        print(f"наведение по гомографии: ({aim[0]:.0f}, {aim[1]:.0f})")
    else:
        print("калибровки нет — наводим в центр сенсора (для ВРЕМЕНИ это не важно)")

    open_window()
    img = cv2.imread(os.path.join(IMG, frames[0]["file"]))
    show(img)

    print(f"профиль warm: reps={reps}, S={sides}")
    capture("profile_warm", cx=aim[0] if aim else None, cy=aim[1] if aim else None,
            profile=True, reps=reps, sides=sides)
    pull("profile_warm")
    d = json.load(open(os.path.join(OUT, "profile_warm.json")))
    print(f"батарея {d.get('battery_c_before')} -> {d.get('battery_c_after')} °C")
    print(f"{'путь':>10} {'S':>5} {'место':>7} {'стадия':>7} {'p50':>8} {'p95':>8}")
    for r in d.get("runs", []):
        st = {1: "чтение", 2: "+конв", 0: "+тензор"}[r["stage"]]
        print(f"{r['path']:>10} {r['side']:>5} {r['pos']:>7} {st:>7} "
              f"{r['p50']:8.2f} {r['p95']:8.2f}")

    # cold: свежий кадр на каждый замер, по одной точке на S
    print("\nпрофиль cold (свежий кадр на каждый замер):")
    cold = {}
    for S in sides:
        tag = f"profile_cold_{S}"
        capture(tag, cx=aim[0] if aim else None, cy=aim[1] if aim else None,
                profile=True, reps=1, sides=[S])
        pull(tag)
        c = json.load(open(os.path.join(OUT, tag + ".json")))
        rows = [r for r in c.get("runs", []) if r["path"] == "scaled"
                and r["pos"] == "center" and r["stage"] == 0]
        if rows:
            cold[S] = rows[0]["p50"]
            print(f"  S={S:>5}: {rows[0]['p50']:.2f} мс")
    json.dump(cold, open(os.path.join(OUT, "profile_cold.json"), "w"), indent=1)
    cv2.destroyAllWindows()


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    what = sys.argv[1] if len(sys.argv) > 1 else "help"
    if what == "calib":
        cmd_calib()
    elif what == "shots":
        cmd_shots()
    elif what == "profile":
        cmd_profile()
    else:
        print(__doc__)
