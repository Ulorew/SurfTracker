package com.surftracker.camfps;

import android.media.Image;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.nio.ByteBuffer;

/**
 * Камерный путь YUV -> RGB -> тензор, ОДНОЙ реализацией на всех.
 *
 * Зачем вынесено. Тикет «камерное зрение» требует доказать, что стенд гоняет
 * ровно тот конвейер, что боевой прогон. Скопированный «такой же» цикл этого
 * не доказывает: он разъедется на первой же правке и никто не заметит. Здесь
 * код один, и совпадение — свойство конструкции, а не обещание.
 *
 * ВНИМАНИЕ, главный подозреваемый по нулевым детекциям. Коэффициенты ниже —
 * ПОЛНОДИАПАЗОННЫЕ BT.601 (Y 0..255). Если камера отдаёт видеодиапазон
 * (Y 16..235, как в видеопотоке), картинка выйдет с задранным контрастом и
 * смещённой яркостью, а модель обучалась на полнодиапазонных JPEG. Сверка
 * уровня 1 из тикета проверяет ровно это: сырой YUV сохраняется ДО
 * конвертации, ноутбук конвертирует независимо и сравнивает.
 */
public final class Yuv {

    private Yuv() {}

    /**
     * Кроп S x S из YUV-кадра в тензор NCHW float32 /255.
     *
     * @param stage профилирование: 0 = полный путь, 1 = только чтение плоскостей,
     *              2 = чтение + конвертация без записи тензора. Аккумулятор
     *              возвращается наружу и обязан быть использован, иначе JIT
     *              выбросит цикл и «чтение» окажется бесплатным.
     * @return аккумулятор-приёмник (складывать в volatile-поле)
     */
    public static long convert(Image im, ByteBuffer out, int cropX, int cropY,
                                int S, int stage) {
        Image.Plane[] pl = im.getPlanes();
        ByteBuffer yb = pl[0].getBuffer(), ub = pl[1].getBuffer(), vb = pl[2].getBuffer();
        int yRow = pl[0].getRowStride();
        int uRow = pl[1].getRowStride(), uPix = pl[1].getPixelStride();
        int vRow = pl[2].getRowStride(), vPix = pl[2].getPixelStride();
        final int PLANE = S * S;
        long acc = 0;
        for (int j = 0; j < S; j++) {
            int sy = cropY + j;
            int yBase = sy * yRow + cropX;
            int uvBase = (sy >> 1) * uRow;
            int vvBase = (sy >> 1) * vRow;
            for (int i = 0; i < S; i++) {
                int Y = yb.get(yBase + i) & 0xFF;
                int uvx = (cropX + i) >> 1;
                int U = (ub.get(uvBase + uvx * uPix) & 0xFF) - 128;
                int V = (vb.get(vvBase + uvx * vPix) & 0xFF) - 128;
                if (stage == 1) { acc += Y + U + V; continue; }
                int R = Y + ((91881 * V) >> 16);
                int G = Y - ((22554 * U + 46802 * V) >> 16);
                int B = Y + ((116130 * U) >> 16);
                if (stage == 2) { acc += R + G + B; continue; }
                int idx = j * S + i;
                out.putFloat(idx * 4, (R < 0 ? 0 : R > 255 ? 255 : R) / 255.0f);
                out.putFloat((PLANE + idx) * 4, (G < 0 ? 0 : G > 255 ? 255 : G) / 255.0f);
                out.putFloat((2 * PLANE + idx) * 4, (B < 0 ? 0 : B > 255 ? 255 : B) / 255.0f);
            }
        }
        return acc;
    }

    /**
     * Кроп S x S С УМЕНЬШЕНИЕМ до OUT x OUT — то, чего в приложении не было.
     *
     * Зачем отдельно от convert(). Вход сети фиксирован 640, а петля просит
     * окно медианой 823 px и p95 1152 (замер по матрице). Нынешний путь берёт
     * ровно 640 натурой и потому показывает модели НЕ то поле зрения, которое
     * петля запросила. Честное уменьшение — единственный способ отдать сети
     * запрошенное окно.
     *
     * Билинейно, а не усреднением по площади: замер на 44 тактах ведения дал
     * IoU топ-бокса p50 0.998 и min 0.959 против INTER_AREA при цене в разы
     * меньше (6.8-10.0 мс против 38-84 на тех же S). Ближайший сосед не
     * годится — тот же замер даёт IoU p05 = 0.000.
     *
     * Стоимость по построению ~ OUT^2, а не S^2: читаются только те точки,
     * которые нужны выходу. Именно это и проверяет кривая кроп(S).
     *
     * @param stage 1 = только выборка Y/U/V, 2 = + конвертация, 0 = полный путь
     */
    public static long convertScaled(Image im, ByteBuffer out, int cropX, int cropY,
                                      int S, int OUT, int stage) {
        Image.Plane[] pl = im.getPlanes();
        ByteBuffer yb = pl[0].getBuffer(), ub = pl[1].getBuffer(), vb = pl[2].getBuffer();
        int yRow = pl[0].getRowStride();
        int uRow = pl[1].getRowStride(), uPix = pl[1].getPixelStride();
        int vRow = pl[2].getRowStride(), vPix = pl[2].getPixelStride();
        final int PLANE = OUT * OUT;
        // Отображение центров пикселей: out i -> src cropX + (i+0.5)*S/OUT - 0.5.
        // Через 16.16 с фиксированной точкой — деления в внутреннем цикле нет.
        final long scale = ((long) S << 16) / OUT;
        final long half = (scale >> 1) - (1L << 15);
        long acc = 0;
        for (int j = 0; j < OUT; j++) {
            long sy16 = (long) j * scale + half;
            int sy = (int) (sy16 >> 16);
            int fy = (int) (sy16 & 0xFFFF);
            if (sy < 0) { sy = 0; fy = 0; }
            int sy1 = sy + 1;
            if (sy1 > S - 1 + cropY) sy1 = sy;
            int yBase0 = (cropY + sy) * yRow;
            int yBase1 = (cropY + sy1) * yRow;
            int uvBase = ((cropY + sy) >> 1) * uRow;
            int vvBase = ((cropY + sy) >> 1) * vRow;
            for (int i = 0; i < OUT; i++) {
                long sx16 = (long) i * scale + half;
                int sx = (int) (sx16 >> 16);
                int fx = (int) (sx16 & 0xFFFF);
                if (sx < 0) { sx = 0; fx = 0; }
                int sx1 = sx + 1;
                if (sx1 > S - 1) sx1 = sx;
                int p00 = yb.get(yBase0 + cropX + sx) & 0xFF;
                int p01 = yb.get(yBase0 + cropX + sx1) & 0xFF;
                int p10 = yb.get(yBase1 + cropX + sx) & 0xFF;
                int p11 = yb.get(yBase1 + cropX + sx1) & 0xFF;
                int top = p00 + (((p01 - p00) * fx) >> 16);
                int bot = p10 + (((p11 - p10) * fx) >> 16);
                int Y = top + (((bot - top) * fy) >> 16);
                // Цветность вдвое реже пространственно — берём ближайшую:
                // билинейная по U/V не окупается, парус различается яркостью.
                int uvx = (cropX + sx) >> 1;
                int U = (ub.get(uvBase + uvx * uPix) & 0xFF) - 128;
                int V = (vb.get(vvBase + uvx * vPix) & 0xFF) - 128;
                if (stage == 1) { acc += Y + U + V; continue; }
                int R = Y + ((91881 * V) >> 16);
                int G = Y - ((22554 * U + 46802 * V) >> 16);
                int B = Y + ((116130 * U) >> 16);
                if (stage == 2) { acc += R + G + B; continue; }
                int idx = j * OUT + i;
                out.putFloat(idx * 4, (R < 0 ? 0 : R > 255 ? 255 : R) / 255.0f);
                out.putFloat((PLANE + idx) * 4, (G < 0 ? 0 : G > 255 ? 255 : G) / 255.0f);
                out.putFloat((2 * PLANE + idx) * 4, (B < 0 ? 0 : B > 255 ? 255 : B) / 255.0f);
            }
        }
        return acc;
    }

    /**
     * Сырые плоскости КАК ЕСТЬ + метаданные, ДО любой конвертации.
     *
     * Ровно то, чего не хватало прежнему «контролю честности»: он сохранял
     * пост-конвертационный буфер, то есть сравнивал результат конвертации сам
     * с собой и не мог отличить «сцена такая» от «конвертация испортила».
     *
     * Формат намеренно тупой: три файла плоскостей побайтово как в
     * ByteBuffer, плюс json со stride-ами. Ноутбук соберёт сам — и если
     * соберёт иначе, чем телефон, это и будет ответом.
     */
    public static void dumpRaw(Image im, File base) throws Exception {
        Image.Plane[] pl = im.getPlanes();
        String[] suffix = {"y", "u", "v"};
        StringBuilder meta = new StringBuilder();
        meta.append("{\"width\":").append(im.getWidth())
            .append(",\"height\":").append(im.getHeight())
            .append(",\"format\":\"YUV_420_888\",\"planes\":[");
        for (int p = 0; p < 3; p++) {
            ByteBuffer b = pl[p].getBuffer().duplicate();
            b.rewind();
            byte[] arr = new byte[b.remaining()];
            b.get(arr);
            try (FileOutputStream fo = new FileOutputStream(base.getPath() + "." + suffix[p])) {
                fo.write(arr);
            }
            if (p > 0) meta.append(",");
            meta.append("{\"plane\":\"").append(suffix[p])
                .append("\",\"bytes\":").append(arr.length)
                .append(",\"row_stride\":").append(pl[p].getRowStride())
                .append(",\"pixel_stride\":").append(pl[p].getPixelStride()).append("}");
        }
        meta.append("]}");
        try (OutputStreamWriter w = new OutputStreamWriter(
                new FileOutputStream(base.getPath() + ".yuvmeta.json"))) {
            w.write(meta.toString());
        }
    }

    /**
     * Гистограмма Y по ВСЕМУ кадру — прямой ответ на вопрос про диапазон.
     * Доля Y&lt;16 и Y&gt;235 равная нулю означает видеодиапазон; у
     * полнодиапазонной картинки хвосты заняты.
     *
     * @return массив из 256 счётчиков
     */
    public static long[] histY(Image im) {
        Image.Plane p = im.getPlanes()[0];
        ByteBuffer b = p.getBuffer();
        int row = p.getRowStride(), w = im.getWidth(), h = im.getHeight();
        long[] hist = new long[256];
        for (int y = 0; y < h; y++) {
            int base = y * row;
            for (int x = 0; x < w; x++) hist[b.get(base + x) & 0xFF]++;
        }
        return hist;
    }
}
