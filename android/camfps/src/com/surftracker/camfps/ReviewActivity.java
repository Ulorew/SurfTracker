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
    private File dir;

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
        row.addView(btn("к потере", v -> nextLoss()));
        play = btn("▶", v -> togglePlay());
        row.addView(play);

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
        if (!video.exists()) {
            // Прогон без записи — обычное дело, и это не ошибка. Но и рисовать
            // поверх нечего, поэтому говорим прямо, а не показываем чёрный экран.
            caption.setText("Видео не писалось — разбирать можно только числа.\n"
                    + "Тактов в логе: " + model.ticks.size()
                    + ", потерь: " + model.losses.size());
            return;
        }

        try {
            mmr = new MediaMetadataRetriever();
            mmr.setDataSource(video.getAbsolutePath());
        } catch (Throwable t) {
            caption.setText("Видео не открылось: " + LiveStatus.shortError(String.valueOf(t)));
            mmr = null;
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

    /** Ближайшая потеря после показанного такта; по кругу. */
    private void nextLoss() {
        if (model.losses.isEmpty()) {
            caption.setText("Потерь в этом прогоне не было");
            return;
        }
        for (int idx : model.losses) if (idx > shown) { request(idx); return; }
        request(model.losses.get(0));
    }

    /**
     * Запрос кадра. Быстрые нажатия НЕ копятся в очередь: берётся последний
     * запрошенный. Иначе десять нажатий подряд на 4K означают десять
     * последовательных извлечений по полсекунды, и экран живёт своей жизнью
     * ещё пять секунд после того, как палец убран.
     */
    private void request(int idx) {
        if (model == null || !model.usable() || mmr == null) return;
        final int i = Math.max(0, Math.min(model.ticks.size() - 1, idx));
        pending = i;
        bar.setProgress(i);
        if (bg == null) return;
        bg.post(() -> {
            int want = pending;
            if (want != i) return;            // уже запросили другой — этот не нужен
            ReviewModel.Tick t = model.ticks.get(want);
            Bitmap frame = null;
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
            final Bitmap drawn = (frame == null) ? null : overlay(frame, t);
            final String cap = ReviewModel.caption(t, want, model.ticks.size())
                    + (model.noWinY ? "\n(старый лог: вертикаль окна неизвестна)" : "")
                    + (frame == null ? "\nкадр не извлёкся" : "");
            ui.post(() -> {
                if (pending != want) return;
                shown = want;
                if (drawn != null) image.setImageBitmap(drawn);
                caption.setText(cap);
                if (playing) player.postDelayed(this::step, 60);
            });
        });
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
            double bh = model.toViewX(t.size / 2.0, vw);
            p.setColor(t.tracking ? Color.GREEN : Color.RED);
            p.setStrokeWidth((float) Math.max(3, vw / 250));
            c.drawRect((float) (bx - bh), (float) (by - bh),
                       (float) (bx + bh), (float) (by + bh), p);
        }

        // Центр кадра — по нему считается ошибка наведения, и без метки
        // невозможно глазами оценить, куда петля ведёт.
        p.setColor(0x88FFFF00);
        p.setStrokeWidth((float) Math.max(1, vw / 800));
        c.drawLine((float) (vw / 2), 0, (float) (vw / 2), (float) vh, p);
        return bm;
    }
}
