"""Теневые треки (тикет "счёт кандидата, теневые треки, синтетический стенд",
п.2).

Назначение одно: чужие детекции ЗАНЯТЫ и не притягивают цель. Без этого
сосед, оказавшийся ближе к предсказанию, забирает захват — и по логу это
выглядит как обычный выбор ближайшего, потому что формально он и есть
ближайший.

Теневой трек — это тот же alpha-beta в углах, но без окна, без потери и без
реакквизиции: он существует, только пока его кормят детекции. Ничего, кроме
"здесь уже кто-то есть", он не утверждает.

Правило исключения кандидата взято из тикета дословно: кандидат выбывает,
если евклидово расстояние до предсказания ТЕНЕВОГО меньше
SHADOW_TAKEN_RATIO от расстояния до предсказания ЦЕЛИ. Метрика с обеих
сторон одна и та же намеренно: махаланобисово расстояние до цели с евклидовым
до теневого сравнивать нельзя — это разные единицы, и отношение между ними
ничего не значит.

При потере цели теневые НЕ очищаются: на повторном захвате они продолжают
занимать соседей, иначе реакквизиция сядет на первого попавшегося.
"""

import math

from track_filters import AlphaBetaFilter, dist


def _det_center(det):
    return ((det[0] + det[2]) / 2.0, (det[1] + det[3]) / 2.0)


def _det_size(det):
    return max(det[2] - det[0], det[3] - det[1])


def _det_conf(det):
    return det[4] if len(det) > 4 else 1.0


class ShadowTrack:
    """Один занятый сосед. Сам решений не принимает."""

    __slots__ = ("filter", "size", "misses", "born_at")

    def __init__(self, cfg, cx, cy, size, born_at, vel=None):
        self.filter = AlphaBetaFilter(cfg.ALPHA_BETA_ALPHA, cfg.ALPHA_BETA_BETA, cfg)
        self.filter.seed(cx, cy)
        if vel is not None:
            # Новорождённый теневой стартует со скоростью ЦЕЛИ, а не с нуля.
            # Нулевая скорость — не знание, а его отсутствие, и на первом же
            # такте детекция соседа уходит дальше радиуса сопоставления: сосед
            # начинает порождать цепочку однотактных теней вместо одной
            # прослеживаемой. Скорость цели — лучший доступный априор:
            # соседи по проезду идут примерно тем же курсом.
            self.filter.vx, self.filter.vy = vel
        self.size = size
        self.misses = 0
        self.born_at = born_at

    def predict(self, dt):
        return self.filter.predict(dt)

    def feed(self, cx, cy, size, dt):
        self.filter.update(cx, cy, dt)
        self.size = size
        self.misses = 0

    def coast(self, dt):
        """Такт без своей детекции: экстраполируем (с тем же затуханием
        скорости, что у цели) и считаем пропуск."""
        self.filter.advance(dt)
        self.misses += 1


class ShadowSet:
    """Все теневые треки такта. Порядок операций внутри такта задан жёстко:
    предсказание -> исключение кандидатов -> выбор цели (снаружи) -> питание
    и рождение. Питание ДО выбора сделало бы теневой сопоставленным с той
    самой детекцией, которую цель ещё только собирается взять.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.tracks = []
        self.tick = 0

    def __len__(self):
        return len(self.tracks)

    def predictions(self, dt):
        """Куда теневые попадут на этом такте. Состояние не трогает."""
        return [t.predict(dt) for t in self.tracks]

    def is_taken(self, det, pred_cx, pred_cy, dt):
        """Занят ли этот кандидат чужим треком (правило тикета).

        pred_cx/cy — предсказание ЦЕЛИ на тот же такт. Обе стороны сравнения
        в евклидовых углах.
        """
        if not self.tracks:
            return False
        dcx, dcy = _det_center(det)
        d_target = dist(dcx, dcy, pred_cx, pred_cy)
        thr = self.cfg.SHADOW_TAKEN_RATIO * d_target
        for scx, scy in self.predictions(dt):
            if dist(dcx, dcy, scx, scy) < thr:
                return True
        return False

    def match_limit(self, track, det_size, dt):
        """Радиус сопоставления детекции существующему теневому.

        Размерная часть — шум измерения; скоростная — то, что теневой мог
        пройти за такт сверх собственного предсказания. Без второй части
        механизм рассыпается ровно на быстрых соседях, то есть там, где он
        и нужен.
        """
        base = self.cfg.SHADOW_MATCH_SIZE_FRAC * max(track.size, det_size)
        return base + math.hypot(track.filter.vx, track.filter.vy) * dt

    def step(self, dt, detections, chosen, target_vel=None):
        """Питание существующих, рождение новых, смерть старых, кап.

        detections — ВСЁ, что нашлось в окне (а не только прошедшее отбор
        цели): сосед, не попавший в радиус выбора, — ровно тот, кого и надо
        занять заранее, до того как он окажется ближе цели.
        """
        self.tick += 1
        chosen_key = None if chosen is None else tuple(chosen[:4])

        # предсказания считаем ОДИН раз до всех обновлений: иначе первый же
        # накормленный теневой сдвинется, и следующий сопоставится уже к
        # обновлённому состоянию — результат стал бы зависеть от порядка
        preds = self.predictions(dt)
        free = [d for d in detections
                if tuple(d[:4]) != chosen_key and _det_conf(d) >= self.cfg.SHADOW_FEED_CONF]

        used = set()
        for i, t in enumerate(self.tracks):
            scx, scy = preds[i]
            best, best_d = None, None
            for j, d in enumerate(free):
                if j in used:
                    continue
                dcx, dcy = _det_center(d)
                dd = dist(dcx, dcy, scx, scy)
                limit = self.match_limit(t, _det_size(d), dt)
                if dd <= limit and (best_d is None or dd < best_d):
                    best, best_d = j, dd
            if best is None:
                t.coast(dt)
            else:
                used.add(best)
                d = free[best]
                dcx, dcy = _det_center(d)
                t.feed(dcx, dcy, _det_size(d), dt)

        self.tracks = [t for t in self.tracks if t.misses < self.cfg.SHADOW_MAX_MISSES]

        if chosen is None and getattr(self.cfg, "SHADOW_BIRTH_REQUIRES_PICK", False):
            # Такт без принятого кандидата — тот, где мы не знаем, кто цель.
            # Рождение здесь и есть первый шаг ловушки закрепления: истинная
            # детекция заводит свой теневой и оказывается заперта правилом
            # 0.7 навсегда. Существующие теневые при этом ЖИВУТ дальше —
            # снимается только рождение.
            return

        for j, d in enumerate(free):
            if j in used or _det_conf(d) < self.cfg.SHADOW_BIRTH_CONF:
                continue
            dcx, dcy = _det_center(d)
            self.tracks.append(ShadowTrack(self.cfg, dcx, dcy, _det_size(d), self.tick,
                                            vel=target_vel))
            if len(self.tracks) > self.cfg.SHADOW_MAX_COUNT:
                # при переполнении умирает самый давно необновлявшийся;
                # при равенстве — самый старый по рождению
                victim = max(self.tracks, key=lambda t: (t.misses, -t.born_at))
                self.tracks.remove(victim)

    def debug_state(self):
        return [{"cx": t.filter.cx, "cy": t.filter.cy, "size": t.size,
                 "misses": t.misses} for t in self.tracks]
