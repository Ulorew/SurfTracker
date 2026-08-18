"""Радиус приёма меряется от ЦЕЛИ, а не от того, что влезло в кадр.

ЧТО ЧИНИТСЯ. Радиус приёма считался от стороны окна, а сторона окна прижата
потолком кадра: вырезка больше короткой стороны приходит в модель анизотропно
сплющенной, поэтому потолок нужен — но он свойство КАДРА. К вопросу «мог ли
этот кандидат быть моей целью» размер кадра отношения не имеет.

Числа прогона 260818_1059: цель 960 px, окно 3.5*960 = 3360 упирается в
потолок 1440, радиус приёма выходит 0.30*1440 = 432 px — это 0.45 размера
цели вместо положенных 1.05. На тактах 409 и 419 уверенные детекции (0.90 и
0.92) были отвергнуты именно этим схлопнувшимся радиусом.

Размер берётся ПРЕДСКАЗАННЫЙ, а не размер кандидата. Урок выучен дважды: в
счёте кандидата (00baa94) и в гейте (5c8aba4) — допуск, растущий с размером
проверяемого объекта, поощряет подмену крупным мусором.
"""
import math
import types

import pytest

import tracking_config as base_cfg
import track_logic as tl


def make_cfg(**overrides):
    d = {k: getattr(base_cfg, k) for k in dir(base_cfg) if k.isupper()}
    d.update(overrides)
    return types.SimpleNamespace(**d)


SIZE = math.radians(2.0)
DT = 0.25
# Потолок связывает так же, как на телефоне: 3.5*размер против min(кадр).
# 1440/3360 = 0.43, здесь 1.5/3.5 = 0.43 — то же отношение.
ПОТОЛОК = SIZE * 1.5


def состояние(**overrides):
    cfg = make_cfg(FILTER_LEVEL=1, ENABLE_MAHALANOBIS_GATE=False,
                   ENABLE_SHADOW_TRACKS=False, ENABLE_SIZE_SCORING=False,
                   ENABLE_OCCLUSION_HOLD=False, SCORE_FORM="distance",
                   **overrides)
    return tl.TrackState(cfg, 0.0, 0.0, SIZE, min_window=SIZE * 0.1,
                         max_window=ПОТОЛОК,
                         view_half_w=SIZE * 20, view_half_h=SIZE * 20)


def рамка(cx, cy, size, conf=0.9):
    return (cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2, conf)


class TestПотолокНеСжимаетПриём:

    def test_окно_прижато_а_приём_нет(self):
        st = состояние()
        assert st.current_window_side() == pytest.approx(ПОТОЛОК), \
            "предпосылка: окно обязано упираться в потолок"
        assert st.acceptance_side() == pytest.approx(
            base_cfg.TRACK_WINDOW_K * SIZE), \
            "сторона приёма считается от размера цели и потолком не режется"

    def test_радиус_вырос_ровно_во_столько_же(self):
        st = состояние()
        было = st.cfg.TARGET_SELECT_MAX_DIST_FRAC * st.current_window_side()
        стало = st.cfg.TARGET_SELECT_MAX_DIST_FRAC * st.acceptance_side()
        # 3.5/1.5 = 2.33 — то же отношение, что 3360/1440 в прогоне
        assert стало / было == pytest.approx(base_cfg.TRACK_WINDOW_K / 1.5)

    def test_детекция_за_прежним_радиусом_принимается(self):
        """Такты 409 и 419 в миниатюре: кандидат дальше схлопнутого радиуса,
        но заведомо ближе честного."""
        st = состояние()
        d = st.cfg.TARGET_SELECT_MAX_DIST_FRAC * ПОТОЛОК * 1.5
        assert d < st.cfg.TARGET_SELECT_MAX_DIST_FRAC * st.acceptance_side()
        r = st.step(DT, [рамка(d, 0.0, SIZE)])
        assert r.chosen is not None

    def test_с_выключенной_правкой_та_же_детекция_отвергается(self):
        st = состояние(TARGET_RADIUS_IGNORES_VIEW_CAP=False)
        d = st.cfg.TARGET_SELECT_MAX_DIST_FRAC * ПОТОЛОК * 1.5
        r = st.step(DT, [рамка(d, 0.0, SIZE)])
        assert r.chosen is None

    def test_без_потолка_ничего_не_меняется(self):
        """Правка обязана быть невидимой там, где потолок не связывает —
        иначе она меняет поведение на всех клипах, а не чинит один режим."""
        for flag in (True, False):
            cfg = make_cfg(FILTER_LEVEL=1, ENABLE_MAHALANOBIS_GATE=False,
                           ENABLE_SHADOW_TRACKS=False, ENABLE_SIZE_SCORING=False,
                           ENABLE_OCCLUSION_HOLD=False, SCORE_FORM="distance",
                           TARGET_RADIUS_IGNORES_VIEW_CAP=flag)
            st = tl.TrackState(cfg, 0.0, 0.0, SIZE, min_window=SIZE * 0.1,
                               max_window=SIZE * 10,
                               view_half_w=SIZE * 20, view_half_h=SIZE * 20)
            assert st.acceptance_side() == pytest.approx(st.current_window_side())


class TestРазмерПредсказанныйАНеКандидата:
    """Третье повторение того же урока — пусть будет прибито и здесь."""

    def test_крупный_кандидат_не_расширяет_себе_приём(self):
        st = состояние()
        радиус = st.cfg.TARGET_SELECT_MAX_DIST_FRAC * st.acceptance_side()
        d = радиус * 1.2
        # кандидат втрое крупнее — но приём считается от ведомого размера
        r = st.step(DT, [рамка(d, 0.0, SIZE * 3)])
        assert r.chosen is None

    def test_приём_растёт_только_когда_растёт_ЦЕЛЬ(self):
        st = состояние()
        было = st.acceptance_side()
        for _ in range(10):
            st.step(DT, [рамка(0.0, 0.0, SIZE * 2)])
        assert st.acceptance_side() > было
