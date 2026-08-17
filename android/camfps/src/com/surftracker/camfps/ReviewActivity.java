package com.surftracker.camfps;

import android.app.Activity;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.media.MediaMetadataRetriever;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.SeekBar;
import android.widget.TextView;

import java.io.File;

/**
 * Разбор записанного прогона прямо на телефоне: кадр, рамки, окно, потери.
 *
 * ЗАЧЕМ НА ТЕЛЕФОНЕ. Разбор был возможен только после стягивания файлов на
 * ноутбук и запуска скрипта. Значит, вопрос «почему потеряло вон там» ждал до
 * возвращения домой, а в поле, где ещё можно переставить камеру или
 * переснять, ответа не было вовсе.
 *
 * ЧТО РИСУЕТСЯ. Окно, которое видела модель (белое), выбранная цель (зелёная
 * или, на такте потери, красная) и подпись такта. Всё — из лога прогона, а не
 * из повторного прогона модели: повторный инференс по записи ответил бы на
 * другой вопрос, «что модель находит сейчас», а не «что происходило тогда».
 *
 * ЦЕНА КАДРА. MediaMetadataRetriever на 4K достаёт кадр за сотни миллисекунд,
 * поэтому вытаскивание идёт в отдельном потоке, а быстрая перемотка не ставит
 * запросы в очередь — берётся последний запрошенный такт (см. pending).
 */
public class ReviewActivity extends Activity {

    private ReviewModel model;
    private MediaMetadataRetriever mmr;
    private ImageView image;
    private TextView caption;
    private SeekBar bar;
    private HandlerThread worker;
    private Handler bg;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private volatile int pending = -1;   // последний запрошенный такт
    private volatile int shown = -1;
    private android.widget.Button play;
    private boolean playing;
    private final Handler player = new Handler(Looper.getMainLooper());
    private android.widget.Button speed;
    private int speedIdx = 1;
    /** Пауза между тактами, мс: чем быстрее, тем меньше. */
    private static final String[] SPEEDS = { "0.5", "1", "2", "4" };
    private static final int[] DELAYS   = { 500, 220, 60, 0 };

    /**
     * Проигрывание по ТАКТАМ, а не по кадрам видео.
     *
     * Разбирают не запись, а решения петли: интересен каждый такт, на котором
     * трекер что-то выбрал. Кадры между тактами ничего не добавляют, а на 4K
     * их извлечение стоит сотни миллисекунд — плавного видео из этого всё
     * равно не выйдет, и попытка сделать вид, что выйдет, только обманет.
     *
     * Следующий такт запрашивается ПОСЛЕ отрисовки предыдущего (см. onShown),
     * иначе очередь запросов растёт быстрее, чем разбирается.
     */
    private void togglePlay() {
        playing = !playing;
        play.setText(playing ? "❚❚" : "▶");
        if (playing) step();
    }

    private void step() {
        if (!playing) return;
        if (shown >= model.ticks.size() - 1) { playing = false; play.setText("▶"); return; }
        request(shown + 1);
    }
    private File dir, framesDir;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        dir = new File(getIntent().getStringExtra("dir"));

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(16, 16, 16, 16);

        caption = new TextView(this);
        caption.setTextSize(16);
        caption.setTypeface(android.graphics.Typeface.MONOSPACE);

        image = new ImageView(this);
        image.setAdjustViewBounds(true);
        image.setScaleType(ImageView.ScaleType.FIT_CENTER);

        bar = new SeekBar(this);
        bar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            public void onProgressChanged(SeekBar s, int p, boolean user) { if (user) request(p); }
            public void onStartTrackingTouch(SeekBar s) {}
            public void onStopTrackingTouch(SeekBar s) {}
        });

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.addView(btn("‹", v -> request(shown - 1)));
        row.addView(btn("›", v -> request(shown + 1)));
        row.addView(btn("пропуск ›", v -> nextGap()));
        play = btn("▶", v -> togglePlay());
        row.addView(play);
        // Скорость проигрывания. Такт разбирается за сотни миллисекунд
        // (кадр модели быстрее, кадр 4K из видео медленнее), поэтому «быстро»
        // это не столько ускорение, сколько отказ от паузы между тактами.
        speed = btn("1x", v -> {
            speedIdx = (speedIdx + 1) % SPEEDS.length;
            speed.setText(SPEEDS[speedIdx] + "x");
        });
        row.addView(speed);

        root.addView(image, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        root.addView(bar);
        root.addView(caption);
        root.addView(row);
        setContentView(root);

        model = ReviewModel.parse(
                RunJson.read(RunJson.pick(dir, "log.csv", "лог.csv", "прогон.csv")),
                RunJson.read(RunJson.pick(dir, "run.json", "прогон.json")));
        File video = RunJson.pick(dir, "video.mp4", "видео.mp4");

        if (!model.usable()) {
            caption.setText("Разбирать нечего: " + (model.error != null ? model.error : "лог пуст"));
            return;
        }
        // КАДРЫ МОДЕЛИ — самостоятельный источник разбора.
        //
        // Прежде экран выходил здесь, если нет видео, и прогон, снятый ради
        // кадров модели, показывал «видео не писалось» вместо разбора. А
        // именно кадры модели и есть точный источник: рамка ложится на них по
        // построению, без привязки ко времени видео.
        framesDir = new File(dir, "frames");
        if (!framesDir.isDirectory()) framesDir = new File(dir, "кадры");
        boolean haveFrames = framesDir.isDirectory()
                && framesDir.list() != null && framesDir.list().length > 0;

        if (video.exists()) {
            try {
                mmr = new MediaMetadataRetriever();
                mmr.setDataSource(video.getAbsolutePath());
            } catch (Throwable t) {
                mmr = null;
                if (!haveFrames) {
                    caption.setText("Видео не открылось: "
                            + LiveStatus.shortError(String.valueOf(t)));
                    return;
                }
            }
        } else if (!haveFrames) {
            caption.setText("Ни видео, ни кадров модели — разбирать можно только числа.\n"
                    + "Тактов в логе: " + model.ticks.size()
                    + ", потерь: " + model.losses.size()
                    + "\n\nЧтобы разбирать картинку, включите «Писать кадры модели»"
                    + " или «Писать видео» в настройках.");
            return;
        }

        worker = new HandlerThread("review");
        worker.start();
        bg = new Handler(worker.getLooper());
        bar.setMax(Math.max(0, model.ticks.size() - 1));
        // Открываем на такте С ЦЕЛЬЮ, а не на первой потере.
        //
        // Прежде экран открывался ровно там, где рамки цели нет по
        // определению — на потере, — и первое, что видел человек, было
        // «разбор не рисует рамку». Прыгать к потерям есть кнопка.
        int first = 0;
        for (int i = 0; i < model.ticks.size(); i++)
            if (model.ticks.get(i).hit) { first = i; break; }
        request(first);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (worker != null) worker.quitSafely();
        try { if (mmr != null) mmr.release(); } catch (Throwable ignored) {}
    }

    private android.widget.Button btn(String text, android.view.View.OnClickListener l) {
        android.widget.Button b = new android.widget.Button(this);
        b.setTextSize(20);
        b.setText(text);
        b.setOnClickListener(l);
        b.setLayoutParams(new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        return b;
    }

    /**
     * К началу СЛЕДУЮЩЕГО отрезка без детекции.
     *
     * По отрезкам, а не по тактам: подряд идущие пропуски — одно событие, и
     * шагать по каждому такту внутри него бессмысленно. И строго вперёд: кольцо
     * выглядело как «кнопка всегда бросает в одно и то же место», потому что
     * прежний список строился по состоянию «потеря» (пять промахов подряд) и
     * часто содержал единственную запись.
     */
    private void nextGap() {
        if (model.gaps.isEmpty()) {
            caption.setText("Тактов без детекции в этом прогоне нет");
            return;
        }
        for (int idx : model.gaps) if (idx > shown) { request(idx); return; }
        caption.setText("Дальше пропусков нет — всего их "
                + model.gaps.size() + ", последний на такте "
                + model.ticks.get(model.gaps.get(model.gaps.size() - 1)).i
                + ". Нажмите ‹ или потяните ползунок, чтобы вернуться назад.");
    }

    /**
     * Запрос кадра. Быстрые нажатия НЕ копятся в очередь: берётся последний
     * запрошенный. Иначе десять нажатий подряд на 4K означают десять
     * последовательных извлечений по полсекунды, и экран живёт своей жизнью
     * ещё пять секунд после того, как палец убран.
     */
    private void request(int idx) {
        if (model == null || !model.usable()) return;
        if (mmr == null && framesDir == null) return;
        final int i = Math.max(0, Math.min(model.ticks.size() - 1, idx));
        pending = i;
        bar.setProgress(i);
        if (bg == null) return;
        bg.post(() -> {
            int want = pending;
            if (want != i) return;            // уже запросили другой — этот не нужен
            ReviewModel.Tick t = model.ticks.get(want);
            Bitmap frame = null;   // переприсваивается при уменьшении
            // КАДР МОДЕЛИ, если он записан.
            //
            // Это тот самый кадр, по которому получена детекция: рамка ложится
            // на него точно по построению, без привязки ко времени видео.
            // Проверено 17 августа на четырёх тактах с самым резким движением
            // (сдвиг цели до 189 px за такт) — рамка ровно на человеке, тогда
            // как поверх видеозаписи она в тех же тактах отставала. Значит
            // отставание жило в привязке к видео, а не в зрении.
            File mf = (framesDir == null) ? null
                    : new File(framesDir, String.format(java.util.Locale.US, "%05d.jpg", t.i));
            if (mf != null && mf.exists()) {
                try { frame = android.graphics.BitmapFactory.decodeFile(mf.getAbsolutePath()); }
                catch (Throwable ignored) {}
                if (frame != null) {
                    final Bitmap drawnM = overlayModelFrame(frame, t, want);
                    final String capM = ReviewModel.caption(t, want, model.ticks.size())
                            + "\nкадр модели (то, что видела сеть)";
                    ui.post(() -> {
                        if (pending != want) return;
                        shown = want;
                        image.setImageBitmap(drawnM);
                        caption.setText(capM);
                        if (playing) player.postDelayed(this::step, DELAYS[speedIdx]);
                    });
                    return;
                }
            }
            try {
                // OPTION_CLOSEST, а НЕ OPTION_CLOSEST_SYNC.
                //
                // SYNC берёт ближайший ОПОРНЫЙ кадр, а они стоят раз в
                // секунду и реже. На стенде с известным законом движения это
                // дало кадр 10.4 с там, где такт приходится на 6.0 с — рамка
                // легла на пустое место, и выглядело это как «трекер вёл не
                // туда». На настоящей записи промах был бы меньше, до
                // полусекунды, то есть незаметен и оттого хуже.
                //
                // Цена — декодирование от предыдущего опорного кадра, поэтому
                // извлечение и живёт в отдельном потоке.
                frame = mmr.getFrameAtTime(model.videoMs(t) * 1000L,
                        MediaMetadataRetriever.OPTION_CLOSEST);
            } catch (Throwable ignored) {}
            if (mmr == null) {
                // Кадра модели на этот такт нет, а видео не писалось: сказать
                // прямо, а не показывать прошлый кадр как нынешний.
                final String capN = ReviewModel.caption(t, want, model.ticks.size())
                        + "\nкадра модели на этот такт нет";
                ui.post(() -> {
                    if (pending != want) return;
                    shown = want;
                    caption.setText(capN);
                    if (playing) player.postDelayed(this::step, DELAYS[speedIdx]);
                });
                return;
            }
            // УМЕНЬШАЕМ ДО РИСОВАНИЯ. Кадр 4K в ARGB — 33 МБ, а copy() для
            // наложения удваивает: 66 МБ на такт в приложении, которое рядом
            // держит камерный конвейер и модель. Экран телефона всё равно
            // меньше 1920, поэтому уменьшение ничего не отнимает у разбора, а
            // расход памяти режет вчетверо. Наложение считает координаты от
            // размеров переданной картинки, так что пересчёт не нужен.
            if (frame != null && frame.getWidth() > 1920) {
                try {
                    Bitmap small = Bitmap.createScaledBitmap(frame, 1920,
                            Math.max(1, frame.getHeight() * 1920 / frame.getWidth()), true);
                    if (small != null && small != frame) { frame.recycle(); frame = small; }
                } catch (Throwable ignored) {}
            }
            final Bitmap drawn = (frame == null) ? null : overlay(frame, t);
            final String cap = ReviewModel.caption(t, want, model.ticks.size())
                    + (model.noWinY ? "\n(старый лог: вертикаль окна неизвестна)" : "")
                    + (frame == null ? "\nкадр не извлёкся" : "");
            ui.post(() -> {
                if (pending != want) return;
                shown = want;
                if (drawn != null) image.setImageBitmap(drawn);
                caption.setText(cap);
                if (playing) player.postDelayed(this::step, DELAYS[speedIdx]);
            });
        });
    }

    /**
     * Наложение на КАДР МОДЕЛИ. Координаты переводятся в тензорные по той же
     * вырезке, из которой кадр и получен, — пересчёта времени здесь нет вовсе.
     */
    private Bitmap overlayModelFrame(Bitmap src, ReviewModel.Tick t, int idx) {
        Bitmap bm = src.copy(Bitmap.Config.ARGB_8888, true);
        Canvas c = new Canvas(bm);
        double[] cr = model.cropOf(idx, model.sensorW, model.sensorH);
        if (cr == null) return bm;
        double n = bm.getWidth();
        Paint p = new Paint();
        p.setStyle(Paint.Style.STROKE);
        p.setAntiAlias(true);
        p.setStrokeWidth((float) Math.max(2, n / 160));
        if (!Double.isNaN(t.cx)) {
            double tx = (t.cx - cr[0]) / cr[2], ty = (t.cy - cr[1]) / cr[2];
            double hw = (Double.isNaN(t.bw) ? t.size : t.bw) / cr[2] / 2;
            double hh = (Double.isNaN(t.bh) ? t.size : t.bh) / cr[2] / 2;
            p.setColor(!Double.isNaN(t.conf) && t.conf < 0.35 ? Color.YELLOW
                       : (t.tracking ? Color.GREEN : Color.RED));
            c.drawRect((float) (tx - hw), (float) (ty - hh),
                       (float) (tx + hw), (float) (ty + hh), p);
        }
        // На кадре модели предсказание НЕ рисуется. Вырезка построена вокруг
        // него же, поэтому метка стояла бы ровно в центре всегда и не несла
        // никаких сведений — а выглядела как измерение. Что детекции нет,
        // сказано в подписи.
        return bm;
    }

    /**
     * Наложение на кадр.
     *
     * Рисуется по КООРДИНАТАМ СЕНСОРА из лога, пересчитанным в пиксели кадра
     * видео. Пересчёт нужен всегда: видео пишется своим профилем, и при записи
     * 1080p поверх сенсора 3840 рамка «как есть» уехала бы вчетверо.
     */
    private Bitmap overlay(Bitmap src, ReviewModel.Tick t) {
        Bitmap bm = src.copy(Bitmap.Config.ARGB_8888, true);
        Canvas c = new Canvas(bm);
        double vw = bm.getWidth(), vh = bm.getHeight();
        Paint p = new Paint();
        p.setStyle(Paint.Style.STROKE);
        p.setAntiAlias(true);

        // Окно модели — белым. Его вертикаль может быть неизвестна в старых
        // логах; тогда ставим по середине кадра и говорим об этом в подписи.
        double wcx = model.toViewX(t.winCx, vw);
        double wcy = (t.winCy >= 0) ? model.toViewY(t.winCy, vh) : vh / 2;
        double half = model.toViewX(t.win / 2.0, vw);
        p.setColor(Color.WHITE);
        p.setStrokeWidth((float) Math.max(2, vw / 400));
        c.drawRect((float) (wcx - half), (float) (wcy - half),
                   (float) (wcx + half), (float) (wcy + half), p);

        // Цель. Красная на такте потери — глаз находит её раньше, чем читает
        // подпись, а разбор начинается именно с «где потеряли».
        if (!Double.isNaN(t.cx) && !Double.isNaN(t.size)) {
            double bx = model.toViewX(t.cx, vw), by = model.toViewY(t.cy, vh);
            // Полувысота — из СВОЕЙ колонки, если она есть. По одному «размеру»
            // (наибольшей стороне) рисовался КВАДРАТ со стороной в рост
            // человека: 980 пикселей вместо 350, половина кадра с обрезанным
            // верхом. Рамкой цели это не выглядело вовсе.
            double halfX = Double.isNaN(t.bw) ? model.toViewX(t.size / 2.0, vw)
                                              : model.toViewX(t.bw / 2.0, vw);
            double halfH = Double.isNaN(t.bh) ? model.toViewX(t.size / 2.0, vw)
                                              : model.toViewX(t.bh / 2.0, vw);
            // Слабая детекция — ЖЁЛТЫМ. При уверенности около 0.3 модель
            // регулярно находит «цель» на смятом покрывале, когда человек ушёл
            // из кадра: рамка при этом выглядит как обычная, и разбор
            // принимает её за цель. Порог 0.35 — тот же, по которому работает
            // затравка крайней меры.
            p.setColor(!Double.isNaN(t.conf) && t.conf < 0.35 ? Color.YELLOW
                       : (t.tracking ? Color.GREEN : Color.RED));
            p.setStrokeWidth((float) Math.max(3, vw / 250));
            c.drawRect((float) (bx - halfX), (float) (by - halfH),
                       (float) (bx + halfX), (float) (by + halfH), p);
        }

        // ТАКТ БЕЗ ДЕТЕКЦИИ: показываем ПРЕДСКАЗАНИЕ, и показываем иначе.
        //
        // Прежде на таком такте не рисовалось ничего, и человек видел только
        // окно — а оно движется по инерции экстраполяции. Читалось это как
        // «рамка цели отстала», хотя рамки цели в этот момент нет вовсе.
        // Пунктир и синий цвет: домысел не должен выглядеть как измерение.
        if (Double.isNaN(t.cx) && t.win > 0) {
            double px = model.toViewX(t.winCx, vw);
            double py = (t.winCy >= 0) ? model.toViewY(t.winCy, vh) : vh / 2;
            double ph = Double.isNaN(t.filtSize) ? model.toViewX(t.win / 8.0, vw)
                                                 : model.toViewX(t.filtSize / 2.0, vw);
            p.setColor(0xFF4FC3F7);
            p.setStrokeWidth((float) Math.max(3, vw / 300));
            p.setPathEffect(new android.graphics.DashPathEffect(
                    new float[]{ (float) (vw / 60), (float) (vw / 90) }, 0));
            c.drawRect((float) (px - ph), (float) (py - ph),
                       (float) (px + ph), (float) (py + ph), p);
            // Перекрестье в центре: пунктирный прямоугольник на пёстром фоне
            // теряется, а суть в том, ГДЕ петля считает цель.
            c.drawLine((float) (px - ph), (float) py, (float) (px + ph), (float) py, p);
            c.drawLine((float) px, (float) (py - ph), (float) px, (float) (py + ph), p);
            p.setPathEffect(null);
        }

        // Центр кадра — по нему считается ошибка наведения, и без метки
        // невозможно глазами оценить, куда петля ведёт.
        p.setColor(0x88FFFF00);
        p.setStrokeWidth((float) Math.max(1, vw / 800));
        c.drawLine((float) (vw / 2), 0, (float) (vw / 2), (float) vh, p);
        return bm;
    }
}
