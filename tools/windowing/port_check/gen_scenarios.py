#!/usr/bin/env python3
"""Сценарии для сличения телефонного переноса с офлайновым трекером.

Почему сценарии, а не только запись: в записи frozen_track.jsonl нет ни одной
ДОЛГОЙ потери с возвратом и ни одного такта с крупной целью у края кадра —
то есть ровно те места, где перенос и офлайн могут разойтись, записью не
покрыты вовсе. Проверка, которая гоняет только запись, объявит согласие, ни
разу не тронув механизм расширения окна и повторного захвата.

Координаты — ПИКСЕЛИ сенсора 3840x2160. Питоновская сторона переводит их в
свою систему вычитанием центра: логика масштабно-инвариантна (всё меряется
долями стороны окна), поэтому такой перевод точен, а не приблизителен.
"""
import json, math, os

W, H = 3840, 2160
DT = 1.0 / 12.0          # такт инференса, как на телефоне
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scenarios.json')


def det(cx, cy, size):
    return [round(cx, 4), round(cy, 4), round(size, 4)]


def sc_straight():
    """Ровный проход поперёк кадра, одна цель. База: должно совпасть точно."""
    ticks = []
    for i in range(60):
        ticks.append([det(400 + i * 50, 1080 + 40 * math.sin(i / 6.0), 300)])
    return ticks


def sc_two_cross():
    """Две цели, встречное пересечение. Здесь решает выбор ближайшего."""
    ticks = []
    for i in range(60):
        a = det(600 + i * 45, 1100, 300)
        b = det(3200 - i * 45, 1060, 280)
        ticks.append([a, b] if i % 2 == 0 else [b, a])   # порядок скачет: перенос
    return ticks                                          # не должен зависеть от него


def sc_loss_return():
    """Шесть промахов подряд и возврат — сценарий, названный в тикете.

    Шесть больше MISS_TO_LOST=5, значит трек обязан пройти в LOST, расширить
    окно и принять цель обратно. Возврат ставится на 380 пикселей в стороне:
    внутри раздувшегося окна, но вне нераздутого.
    """
    ticks = [[det(1900, 1080, 300)] for _ in range(10)]
    ticks += [[] for _ in range(6)]
    ticks += [[det(2280, 1080, 300)] for _ in range(10)]
    return ticks


def sc_long_loss():
    """Двадцать промахов: расширение упирается в короткую сторону кадра."""
    ticks = [[det(1900, 1080, 300)] for _ in range(8)]
    ticks += [[] for _ in range(20)]
    ticks += [[det(1500, 900, 300)] for _ in range(6)]
    return ticks


def sc_big_target():
    """Крупная цель (filteredSize >= 411, тикет) у края кадра.

    Здесь окно упирается в потолок и в прижатие к кадру одновременно — место,
    где расходятся два режима прижатия центра.
    """
    ticks = []
    for i in range(40):
        s = 300 + i * 12                      # растёт: цель приближается
        x = 3400 - i * 10
        ticks.append([det(x, 300 + i * 5, s)])
    return ticks


def sc_edge():
    """Цель уходит в самый угол и возвращается: проверка прижатия убеждения."""
    ticks = []
    for i in range(30):
        ticks.append([det(max(60, 1900 - i * 130), max(60, 1080 - i * 70), 260)])
    for i in range(20):
        ticks.append([det(60 + i * 120, 60 + i * 60, 260)])
    return ticks


def sc_shrink():
    """Цель уходит вдаль: размер падает с 900 до 200.

    Добавлен не для полноты. Без него стенд не ловил подмену
    SIZE_FILTER_SHRINK_RATE (0.1 -> 0.5): ни в одном сценарии размер не
    убывал, а именно на убывании асимметричная EMA и отличается от обычной.
    Скорость спада решает, как быстро сжимается окно за уходящим сёрфером —
    ошибиться тут значит потерять цель на дальнем галсе.
    """
    ticks = []
    for i in range(35):
        s = max(200.0, 900.0 - i * 25.0)
        ticks.append([det(1900 + i * 20, 1080 - i * 8, s)])
    return ticks


def sc_empty_start():
    """Пусто на старте, потом цель. Затравка обязана открыться одинаково."""
    return [[] for _ in range(3)] + [[det(1000, 800, 240)] for _ in range(20)]


SCENARIOS = {
    'straight':    sc_straight(),
    'two_cross':   sc_two_cross(),
    'loss_return': sc_loss_return(),
    'long_loss':   sc_long_loss(),
    'big_target':  sc_big_target(),
    'edge':        sc_edge(),
    'shrink':      sc_shrink(),
    'empty_start': sc_empty_start(),
}

TXT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scenarios.txt')


def write_txt(path):
    """Плоский формат: обе стороны читают ОДИН файл.

    JSON тут был бы обузой — на телефонной стороне нет разбора JSON, и писать
    его пришлось бы вручную, то есть завести второй источник расхождения в
    самой проверке.
    """
    with open(path, 'w') as f:
        f.write(f'FRAME {W} {H} {DT!r} 640\n')
        for name, ticks in SCENARIOS.items():
            f.write(f'SCENARIO {name} {len(ticks)}\n')
            for t in ticks:
                parts = [str(len(t))]
                for d in t:
                    parts += [repr(d[0]), repr(d[1]), repr(d[2])]
                f.write(' '.join(parts) + '\n')


if __name__ == '__main__':
    write_txt(TXT)
    n = sum(len(v) for v in SCENARIOS.values())
    print(f'сценариев {len(SCENARIOS)}, тактов всего {n} -> {TXT}')
