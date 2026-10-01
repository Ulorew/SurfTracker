"""tile_infer_video.py — тайлинг кадра под инференс + склейка через NMS.
Код был без тестов; логика (сетка перекрывающихся окон,
объединение дублей) напрямую пригодится трекеру — стоит закрепить сейчас."""
import pytest

from tile_infer_video import make_tile_grid, nms


class TestMakeTileGrid:
    def test_frame_smaller_than_tile_single_tile_at_origin(self):
        tiles = make_tile_grid(400, 300, tile=640, overlap=140)
        assert tiles == [(0, 0)]

    def test_frame_exactly_tile_size_single_tile(self):
        tiles = make_tile_grid(640, 640, tile=640, overlap=140)
        assert tiles == [(0, 0)]

    def test_last_tile_always_flush_with_right_and_bottom_edge(self):
        tiles = make_tile_grid(1920, 1080, tile=640, overlap=140)
        xs = sorted({x for x, y in tiles})
        ys = sorted({y for x, y in tiles})
        assert xs[-1] + 640 == 1920
        assert ys[-1] + 640 == 1080
        assert xs[0] == 0 and ys[0] == 0

    def test_full_frame_coverage_no_gaps(self):
        """Каждая точка кадра должна попадать хотя бы в одну плитку — иначе
        цель у "непокрытой" координаты никогда не будет замечена ни одной
        плиткой."""
        w, h, tile, overlap = 1920, 1080, 640, 140
        tiles = make_tile_grid(w, h, tile, overlap)
        # проверяем по решётке точек, не по каждому пикселю (быстрее, тот же смысл)
        for px in range(0, w, 37):
            for py in range(0, h, 37):
                covered = any(tx <= px < tx + tile and ty <= py < ty + tile for tx, ty in tiles)
                assert covered, f"точка ({px},{py}) не покрыта ни одной плиткой"

    def test_overlap_between_adjacent_tiles_at_least_requested(self):
        tiles = make_tile_grid(1920, 1080, tile=640, overlap=140)
        xs = sorted({x for x, y in tiles})
        for a, b in zip(xs, xs[1:]):
            actual_overlap = (a + 640) - b
            assert actual_overlap >= 140 or b == xs[-1], (
                "перекрытие между соседними плитками по x меньше запрошенного "
                "(последняя плитка исключение — она прижата к краю)"
            )

    def test_no_duplicate_tile_positions(self):
        tiles = make_tile_grid(1920, 1080, tile=640, overlap=140)
        assert len(tiles) == len(set(tiles))


class TestNms:
    def test_empty_input(self):
        assert nms([], [], iou_thr=0.5) == []

    def test_single_box_kept(self):
        assert nms([(0, 0, 10, 10)], [0.9], iou_thr=0.5) == [0]

    def test_high_iou_duplicate_keeps_higher_score(self):
        boxes = [(0, 0, 10, 10), (1, 1, 11, 11)]  # почти полностью пересекаются
        scores = [0.9, 0.5]
        keep = nms(boxes, scores, iou_thr=0.5)
        assert keep == [0]

    def test_low_iou_boxes_both_kept(self):
        boxes = [(0, 0, 10, 10), (100, 100, 110, 110)]
        scores = [0.9, 0.5]
        keep = nms(boxes, scores, iou_thr=0.5)
        assert set(keep) == {0, 1}

    def test_order_independent_of_input_order(self):
        """Тот же результат, если та же пара боксов подана в обратном
        порядке скоров — раз ЛУЧШИЙ скор побеждает, а не первый по списку."""
        boxes = [(1, 1, 11, 11), (0, 0, 10, 10)]
        scores = [0.5, 0.9]
        keep = nms(boxes, scores, iou_thr=0.5)
        assert boxes[keep[0]] == (0, 0, 10, 10)

    def test_iou_threshold_boundary_respected(self):
        # два бокса 10x10 со сдвигом 8px по x -> IoU = 2*10/(100+100-2*10)=0.111
        boxes = [(0, 0, 10, 10), (8, 0, 18, 10)]
        scores = [0.9, 0.8]
        assert nms(boxes, scores, iou_thr=0.2) == [0, 1]  # ниже порога -> оба остаются
        assert nms(boxes, scores, iou_thr=0.05) == [0]    # выше порога -> дубль убран
