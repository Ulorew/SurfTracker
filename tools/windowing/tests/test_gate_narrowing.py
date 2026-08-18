"""Три правки гейта (18.08.2026): допуск, сужение, прогрев.

Почему отдельным файлом и почему подробно. Гейт был заведён с обещанием «сам
расширяется, когда фильтр не уверен, и сам сужается на плотном треке — вручную
ничего подкручивать не нужно». Замер на телефонной геометрии показал, что
расширяется он гораздо охотнее, чем сужается, а 477 существующих тестов правку
не заметили ни одним — то есть поведение гейта не было прибито ничем.

Каждый тест здесь проверяет ОБА состояния флага: и что правка работает, и что
без неё воспроизводится прежнее (дефектное) поведение. Иначе тест не отличает
«починено» от «этой ветки вообще нет».
"""
import math
import types

import pytest

import tracking_config as base_cfg
from track_kalman import IDX_LOGH, KalmanAngularFilter
import track_logic as tl


def make_cfg(**overrides):
    d = {k: getattr(base_cfg, k) for k in dir(base_cfg) if k.isupper()}
    d.update(overrides)
    return types.SimpleNamespace(**d)


SIZE = math.radians(2.0)
DT = 0.25
# ПОТОЛОК ОКНА ОБЯЗАТЕЛЕН, иначе проверяется не тот режим. Без потолка
# сторона окна равна 3.5 размера, радиус приёма выходит 1.05 размера и почти
# совпадает с допуском сошедшегося гейта (1.28) — разница в 22% тонет.
# В прогоне 260818_1059 окно упиралось в потолок кадра медианно: 1440 при
# 3.5*960 = 3360, и радиус там был 0.45 размера, то есть гейт шире втрое.
# Здесь потолок ставится так, чтобы воспроизвести именно это отношение.
MAX_WINDOW = SIZE * 1.5          # радиус приёма 0.3*1.5 = 0.45 размера


def converged(**overrides):
    """Фильтр после долгого ровного ведения: ковариация сошлась."""
    f = KalmanAngularFilter(make_cfg(**overrides))
    f.seed(0.0, 0.0, SIZE)
    for _ in range(30):
        f.update(0.0, 0.0, DT, SIZE)
    return f


class TestДопускОтПредсказанногоРазмера:
    """Правка 1. R = (0.3*размер)^2, и в гейт подставлялся размер САМОГО
    КАНДИДАТА — то есть крупная чужая рамка получала более широкий допуск.
    Это ровно тот дефект, который стенд форм счёта поймал 5 августа (коммит
    00baa94, кандидат вдвое крупнее получал счёт 11.98 против 17.82), но
    исправлен он был тогда в score_distance2, а она при SCORE_FORM =
    "distance" не исполняется вовсе."""

    def test_размер_кандидата_не_влияет_на_гейт(self):
        f = converged(MAHA_R_FROM_PREDICTED_SIZE=True)
        d = SIZE * 1.5
        малый = f.gate_distance2(d, 0.0, SIZE * 0.5, DT)
        верный = f.gate_distance2(d, 0.0, SIZE, DT)
        крупный = f.gate_distance2(d, 0.0, SIZE * 3.0, DT)
        # В двумерном гейте сравниваются ТОЛЬКО положения; размер кандидата
        # там измерением не является и входить в допуск не должен.
        assert малый == pytest.approx(верный, rel=1e-12)
        assert крупный == pytest.approx(верный, rel=1e-12)

    def test_без_правки_крупный_кандидат_получает_поблажку(self):
        """Прежнее поведение обязано воспроизводиться выключением флага —
        иначе тест выше не доказывает, что чинилось именно это."""
        f = converged(MAHA_R_FROM_PREDICTED_SIZE=False)
        d = SIZE * 1.5
        верный = f.gate_distance2(d, 0.0, SIZE, DT)
        крупный = f.gate_distance2(d, 0.0, SIZE * 3.0, DT)
        assert крупный < верный * 0.5, (
            f"крупный кандидат обязан был получать МЕНЬШИЙ счёт: "
            f"{крупный:.3f} против {верный:.3f}")

    def test_поблажка_растёт_как_квадрат_размера(self):
        """Не просто «меньше», а во сколько именно: допуск R ~ размер^2, и на
        сошедшейся ковариации счёт падает почти вчетверо при удвоении рамки.
        Число фиксируется, чтобы правка не выродилась в «чуть-чуть иначе»."""
        f = converged(MAHA_R_FROM_PREDICTED_SIZE=False)
        d = SIZE * 1.5
        одинарный = f.gate_distance2(d, 0.0, SIZE, DT)
        двойной = f.gate_distance2(d, 0.0, SIZE * 2.0, DT)
        assert 2.0 < одинарный / двойной < 4.0


class TestГейтТолькоСужает:
    """Правка 2. Гейт ЗАМЕЩАЛ радиус приёма, а не добавлялся к нему, поэтому
    мог принять то, что радиус отверг бы. Замер: на сошедшемся треке допуск
    гейта 967 px против фиксированного радиуса 432, а сразу после затравки —
    1815 при полудиагонали кадра 1200."""

    @staticmethod
    def _состояние(**overrides):
        cfg = make_cfg(FILTER_LEVEL=2, ENABLE_MAHALANOBIS_GATE=True,
                       ENABLE_SHADOW_TRACKS=False, ENABLE_SIZE_SCORING=False,
                       ENABLE_OCCLUSION_HOLD=False, SCORE_FORM="distance",
                       KALMAN_GATE_MIN_UPDATES=0, **overrides)
        st = tl.TrackState(cfg, 0.0, 0.0, SIZE,
                           min_window=0.0, max_window=MAX_WINDOW,
                           view_half_w=5.0, view_half_h=5.0)
        for _ in range(30):
            st.step(DT, [_рамка(0.0, 0.0, SIZE)])
        return st

    def test_далёкий_кандидат_отвергается(self):
        st = self._состояние(KALMAN_GATE_ALSO_RADIUS=True)
        side = st.current_window_side()
        радиус = st.cfg.TARGET_SELECT_MAX_DIST_FRAC * side
        # Кандидат ЗА радиусом, но внутри ковариационного допуска: ровно тот
        # случай, ради которого правка и делалась.
        d = радиус * 1.4
        assert st.filter.gate_distance2(d, 0.0, SIZE, DT) <= st.cfg.KALMAN_GATE_CHI2, \
            "предпосылка теста: гейт сам по себе такого кандидата принимает"
        r = st.step(DT, [_рамка(d, 0.0, SIZE)])
        assert r.chosen is None

    def test_без_правки_тот_же_кандидат_принимается(self):
        st = self._состояние(KALMAN_GATE_ALSO_RADIUS=False)
        side = st.current_window_side()
        d = st.cfg.TARGET_SELECT_MAX_DIST_FRAC * side * 1.4
        r = st.step(DT, [_рамка(d, 0.0, SIZE)])
        assert r.chosen is not None

    def test_близкий_кандидат_проходит_в_обоих_режимах(self):
        """Сужение обязано быть сужением, а не запретом: то, что принималось и
        радиусом, и гейтом, обязано приниматься по-прежнему."""
        for also in (True, False):
            st = self._состояние(KALMAN_GATE_ALSO_RADIUS=also)
            side = st.current_window_side()
            d = st.cfg.TARGET_SELECT_MAX_DIST_FRAC * side * 0.3
            r = st.step(DT, [_рамка(d, 0.0, SIZE)])
            assert r.chosen is not None, f"also_radius={also}"


class TestПрогревГейта:
    """Правка 3. Начальная дисперсия скорости берётся из KALMAN_MAX_SPEED
    (20 м/с на 20 м = 1 рад/с) — это не знание, а его отсутствие. На таком
    фоне ковариационный допуск шире кадра, и судить по нему нечего."""

    def test_счётчик_обнуляется_затравкой(self):
        f = KalmanAngularFilter(make_cfg())
        f.seed(0.0, 0.0, SIZE)
        assert f.n_updates == 0
        for i in range(1, 4):
            f.update(0.0, 0.0, DT, SIZE)
            assert f.n_updates == i
        # Перезахват начинает счёт заново: ковариация снова широка.
        f.seed(1.0, 1.0, SIZE)
        assert f.n_updates == 0

    def test_допуск_после_затравки_шире_кадра(self):
        """Число, ради которого правка и заведена: измеренный допуск сразу
        после затравки — 21.8 размера цели, через один такт 2.2, после
        сходимости 1.28. То есть беда сосредоточена в первых тактах, и лечится
        не подстройкой ковариации, а тем, чтобы на них ей просто не верить."""
        def допуск(f):
            lo, hi = 0.0, SIZE * 200
            for _ in range(60):
                m = (lo + hi) / 2
                if f.gate_distance2(m, 0.0, SIZE, DT) <= f.cfg.KALMAN_GATE_CHI2:
                    lo = m
                else:
                    hi = m
            return lo / SIZE

        свежий = KalmanAngularFilter(make_cfg())
        свежий.seed(0.0, 0.0, SIZE)
        assert допуск(свежий) > 15.0
        assert 1.0 < допуск(converged()) < 1.5

    def test_до_прогрева_работает_радиус(self):
        cfg = make_cfg(FILTER_LEVEL=2, ENABLE_MAHALANOBIS_GATE=True,
                       ENABLE_SHADOW_TRACKS=False, ENABLE_SIZE_SCORING=False,
                       ENABLE_OCCLUSION_HOLD=False, SCORE_FORM="distance",
                       KALMAN_GATE_ALSO_RADIUS=False,   # чтобы мерить именно прогрев
                       KALMAN_GATE_MIN_UPDATES=5)
        st = tl.TrackState(cfg, 0.0, 0.0, SIZE, min_window=0.0,
                           max_window=MAX_WINDOW,
                           view_half_w=5.0, view_half_h=5.0)
        side = st.current_window_side()
        d = st.cfg.TARGET_SELECT_MAX_DIST_FRAC * side * 1.4
        # первый же такт: фильтр не прогрет, отбор обязан идти по радиусу
        r = st.step(DT, [_рамка(d, 0.0, SIZE)])
        assert r.chosen is None

    def test_после_прогрева_гейт_включается(self):
        cfg = make_cfg(FILTER_LEVEL=2, ENABLE_MAHALANOBIS_GATE=True,
                       ENABLE_SHADOW_TRACKS=False, ENABLE_SIZE_SCORING=False,
                       ENABLE_OCCLUSION_HOLD=False, SCORE_FORM="distance",
                       KALMAN_GATE_ALSO_RADIUS=False,
                       KALMAN_GATE_MIN_UPDATES=5)
        st = tl.TrackState(cfg, 0.0, 0.0, SIZE, min_window=0.0,
                           max_window=MAX_WINDOW,
                           view_half_w=5.0, view_half_h=5.0)
        for _ in range(8):
            st.step(DT, [_рамка(0.0, 0.0, SIZE)])
        side = st.current_window_side()
        d = st.cfg.TARGET_SELECT_MAX_DIST_FRAC * side * 1.4
        r = st.step(DT, [_рамка(d, 0.0, SIZE)])
        assert r.chosen is not None, \
            "после прогрева гейт обязан снова быть шире радиуса (с выключенным сужением)"


def _рамка(cx, cy, size, conf=0.9):
    """Детекция в формате петли: (x0, y0, x1, y1, conf)."""
    return (cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2, conf)
