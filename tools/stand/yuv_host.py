#!/usr/bin/env python3
"""НЕЗАВИСИМАЯ конвертация сырого YUV с телефона.

Смысл — именно в независимости. Прежний «контроль честности» сохранял буфер
ПОСЛЕ конвертации и прогонял его же на ноутбуке: совпадение было
гарантировано конструкцией и не могло обнаружить ни одной ошибки камерного
пути. Здесь читается сырой вход телефона и переводится своим кодом; если два
результата разойдутся — разошлись реализации, а не «сцена такая».

Формула — полнодиапазонная BT.601, та же, что на телефоне: совпадение
доказывает, что АРИФМЕТИКА телефона верна. Отдельный вопрос — та ли это
формула вообще; на него отвечает гистограмма Y (если Y не заходит ниже 16 и
выше 235, камера отдаёт видеодиапазон, и полнодиапазонная математика
растягивает контраст).
"""
import json
import os

import numpy as np


def load_planes(base):
    """base — путь без расширения; рядом лежат .y/.u/.v и .yuvmeta.json."""
    meta = json.load(open(base + ".yuvmeta.json"))
    planes = {}
    for p in meta["planes"]:
        planes[p["plane"]] = (np.fromfile(base + "." + p["plane"], dtype=np.uint8),
                               p["row_stride"], p["pixel_stride"])
    return meta, planes


def to_rgb(base, x0=0, y0=0, w=None, h=None, video_range=False):
    """Сырые плоскости -> RGB uint8 участка (x0,y0,w,h) в координатах сенсора.

    Арифметика повторяет телефонную побитово (целочисленные сдвиги), иначе
    расхождение в один-два уровня объяснялось бы округлением, а не путём.
    """
    meta, planes = load_planes(base)
    W, H = meta["width"], meta["height"]
    w = w or W
    h = h or H
    yb, y_row, _ = planes["y"]
    ub, u_row, u_pix = planes["u"]
    vb, v_row, v_pix = planes["v"]

    ys = (np.arange(y0, y0 + h)[:, None] * y_row + np.arange(x0, x0 + w)[None, :])
    Y = yb[ys].astype(np.int32)
    uvy = (np.arange(y0, y0 + h) >> 1)
    uvx = (np.arange(x0, x0 + w) >> 1)
    U = ub[(uvy[:, None] * u_row + uvx[None, :] * u_pix)].astype(np.int32) - 128
    V = vb[(uvy[:, None] * v_row + uvx[None, :] * v_pix)].astype(np.int32) - 128

    if video_range:
        # Контрольная гипотеза: камера отдаёт 16..235 -> сначала растянуть.
        Y = np.clip((Y - 16) * 255 // 219, 0, 255)
        U = U * 255 // 224
        V = V * 255 // 224

    R = Y + ((91881 * V) >> 16)
    G = Y - ((22554 * U + 46802 * V) >> 16)
    B = Y + ((116130 * U) >> 16)
    return np.clip(np.stack([R, G, B], axis=-1), 0, 255).astype(np.uint8)


def y_stats(base):
    """Диапазон яркости по всему кадру — главный признак full/limited range."""
    meta, planes = load_planes(base)
    W, H = meta["width"], meta["height"]
    yb, y_row, _ = planes["y"]
    # Последняя строка приходит обрезанной до ШИРИНЫ, а не до stride: буфер
    # короче H*row на (row-W) байт. Читать по полному stride нельзя.
    rows = len(yb) // y_row
    Y = yb[: rows * y_row].reshape(rows, y_row)[:, :W]
    return {
        "min": int(Y.min()), "max": int(Y.max()),
        "доля_ниже_16": float((Y < 16).mean()),
        "доля_выше_235": float((Y > 235).mean()),
        "медиана": float(np.median(Y)),
    }


if __name__ == "__main__":
    import sys
    b = sys.argv[1]
    print(json.dumps(y_stats(b), ensure_ascii=False, indent=1))
