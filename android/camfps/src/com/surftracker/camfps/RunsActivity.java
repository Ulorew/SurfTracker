package com.surftracker.camfps;

import android.app.Activity;
import android.os.Bundle;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.File;
import java.io.FileInputStream;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Список прогонов и итог по каждому.
 *
 * ЗАЧЕМ. До этого экрана приложение не имело интерфейса вовсе: всё
 * запускалось интентами с ноутбука, а результат лежал россыпью файлов в общем
 * каталоге. На вопрос «где смотреть видео» приходилось отвечать путём, а на
 * вопрос «что там произошло» — «стяни лог и прогоним скрипт». Прогон, который
 * нельзя посмотреть с самого телефона, фактически не закончен.
 *
 * ЧТО ПОКАЗЫВАЕТ. Доля тактов на цели, число потерь, длительность, окно,
 * скорость такта, нагрев. Числа НЕ считаются здесь: их пишет TrackActivity в
 * прогон.json на том же материале, из которого строит лог. Второй счётчик,
 * живущий отдельно, рано или поздно разошёлся бы с первым, и различить их
 * было бы нечем.
 *
 * ЧЕГО ЗДЕСЬ НЕТ. Оценки «хорошо/плохо» по доле на цели. Доля 0.4 на прогоне,
 * где человек ушёл из кадра, и доля 0.4 при подмене цели — разные вещи, а
 * отличить их по одному числу нельзя. Экран красит только явный отказ (прогон
 * не завершился) и пустой прогон.
 */
public class RunsActivity extends Activity {

    static final String TAG = "runs";

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        render();
    }

    @Override
    protected void onResume() {
        super.onResume();
        render();   // вернулись из просмотра — список мог измениться
    }

    private void render() {
        ScrollView sv = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(28, 28, 28, 28);
        sv.addView(root);

        File dir = new File(getExternalFilesDir(null), "track");
        List<File> runs = listRuns(dir);

        // Начать прогон и поменять настройки можно С ТЕЛЕФОНА. Прежде и то и
        // другое требовало ноутбука: запуск — командой adb, настройка — правкой
        // константы и пересборкой apk. В поле нет ни того, ни другого.
        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        top.addView(bigButton("НОВЫЙ ПРОГОН", v -> startActivity(
                new android.content.Intent(this, TrackActivity.class))));
        top.addView(bigButton("Настройки", v -> startActivity(
                new android.content.Intent(this, SettingsActivity.class))));
        root.addView(top);

        // КАЛИБРОВКА — с телефона, без ноутбука.
        //
        // Поле зрения, которое сообщает камера, занижено на 18% (измерено), и
        // величина уже дважды менялась со сменой режима потока — значит её
        // надо перемерять, а не считать константой. Пока этот прогон
        // запускался только командой с ноутбука, в поле он был недоступен, а
        // трижды подряд вместо него по ошибке запускалось обычное слежение.
        LinearLayout top2 = new LinearLayout(this);
        top2.setOrientation(LinearLayout.HORIZONTAL);
        top2.addView(bigButton("КАЛИБРОВКА ПОЛЯ ЗРЕНИЯ", v -> startActivity(
                new android.content.Intent(this, TrackActivity.class)
                        .putExtra("flow", true)
                        .putExtra("spin", 0.12f)
                        .putExtra("seconds", 40)
                        .putExtra("video", false)
                        .putExtra("home", true)
                        .putExtra("tag", "калибровка"))));
        root.addView(top2);

        TextView calHint = new TextView(this);
        calHint.setTextSize(15);
        calHint.setPadding(0, 6, 0, 0);
        calHint.setText("Калибровка: наведите на неподвижную сцену с фактурой, "
                + "нажмите СТАРТ и уйдите из кадра. Человек не нужен и мешает.");
        root.addView(calHint);

        TextView head = new TextView(this);
        head.setTextSize(30);
        head.setPadding(0, 30, 0, 0);
        head.setText(runs.isEmpty() ? "Прогонов пока нет"
                                    : "Прогоны (" + runs.size() + ")");
        root.addView(head);

        if (runs.isEmpty()) {
            TextView hint = new TextView(this);
            hint.setTextSize(17);
            hint.setPadding(0, 20, 0, 0);
            hint.setText("Каталог: " + dir.getAbsolutePath()
                    + "\n\nПрогон появится здесь сразу после завершения.");
            root.addView(hint);
        }

        for (File r : runs) root.addView(card(r));
        setContentView(sv);
    }

    /**
     * Папки прогонов, новые сверху.
     *
     * Сортировка по имени, а не по времени файла: метка прогона начинается с
     * даты и времени, а mtime папки меняется от любого касания — в том числе
     * от стягивания файлов на ноут, после которого порядок молча перемешался
     * бы. Прогоны со старым, плоским именованием сюда не попадают: у них нет
     * своей папки, и склеивать их с новыми по префиксу значило бы гадать.
     */
    static List<File> listRuns(File dir) {
        List<File> out = new ArrayList<>();
        File[] all = dir.listFiles();
        if (all == null) return out;
        for (File f : all)
            if (f.isDirectory() && (new File(f, "run.json").exists() || new File(f, "прогон.json").exists())) out.add(f);
        out.sort((a, c) -> c.getName().compareTo(a.getName()));
        return out;
    }

    /** Карточка одного прогона. */
    private View card(File runDir) {
        LinearLayout c = new LinearLayout(this);
        c.setOrientation(LinearLayout.VERTICAL);
        c.setPadding(24, 24, 24, 24);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = 24;
        c.setLayoutParams(lp);

        String json = read(RunJson.pick(runDir, "run.json", "прогон.json"));
        boolean ok = jsonBool(json, "ok");
        boolean started = jsonNum(json, "тактов") > 0;
        c.setBackgroundColor(!started ? 0xFF3A2A2A : (ok ? 0xFF1E2A1E : 0xFF3A3320));

        TextView name = new TextView(this);
        name.setTextSize(22);
        name.setText(runDir.getName());
        c.addView(name);

        TextView main = new TextView(this);
        main.setTextSize(19);
        main.setPadding(0, 10, 0, 0);
        main.setText(summary(json, runDir));
        c.addView(main);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setPadding(0, 14, 0, 0);
        File video = RunJson.pick(runDir, "video.mp4", "видео.mp4");
        if (video.exists()) row.addView(button("Видео", v -> openVideo(video)));
        // Разбор доступен всегда, когда есть лог: без видео он покажет числа,
        // и это честнее, чем прятать кнопку и оставлять вопрос без ответа.
        if (RunJson.pick(runDir, "log.csv", "лог.csv", "прогон.csv").exists())
            row.addView(button("Разбор", v -> startActivity(
                    new android.content.Intent(this, ReviewActivity.class)
                            .putExtra("dir", runDir.getAbsolutePath()))));
        row.addView(button("Подробно", v -> openDetails(runDir)));
        c.addView(row);
        return c;
    }

    private android.widget.Button bigButton(String text, View.OnClickListener l) {
        android.widget.Button b = new android.widget.Button(this);
        b.setTextSize(20);
        b.setText(text);
        b.setOnClickListener(l);
        b.setLayoutParams(new LinearLayout.LayoutParams(0, 170, 1f));
        return b;
    }

    private android.widget.Button button(String text, View.OnClickListener l) {
        android.widget.Button b = new android.widget.Button(this);
        b.setTextSize(18);
        b.setText(text);
        b.setOnClickListener(l);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        b.setLayoutParams(lp);
        return b;
    }

    /** Короткая сводка для карточки: то, что решают с одного взгляда. */
    private String summary(String json, File runDir) {
        return RunJson.summary(json, RunJson.pick(runDir, "video.mp4", "видео.mp4").exists());
    }


    /**
     * Открыть запись — ЧЕРЕЗ ГАЛЕРЕЮ, а не по пути к файлу.
     *
     * Запись лежит в Android/data/<пакет>/files: этот каталог не индексируется
     * галереей вовсе, а file:// чужому проигрывателю отдавать нельзя — он его
     * не примет. Поэтому при первом нажатии видео публикуется в общий раздел
     * (Movies/SurfTracker), и дальше открывается уже оттуда.
     *
     * Копия делается ОДИН РАЗ и её адрес запоминается рядом с записью: файл на
     * четыреста мегабайт незачем копировать при каждом просмотре.
     */
    private void publishAndOpen(File f) {
        File mark = new File(f.getParentFile(), "video.uri");
        String saved = RunJson.read(mark);
        if (saved != null && saved.trim().length() > 0) {
            if (openUri(android.net.Uri.parse(saved.trim()))) return;
            mark.delete();   // адрес протух (видео удалили из галереи) — опубликуем заново
        }
        toast("Публикую в галерею, " + (f.length() / 1024 / 1024) + " МБ…");
        new Thread(() -> {
            android.net.Uri uri = null;
            try {
                android.content.ContentValues cv = new android.content.ContentValues();
                cv.put(android.provider.MediaStore.Video.Media.DISPLAY_NAME,
                        f.getParentFile().getName() + ".mp4");
                cv.put(android.provider.MediaStore.Video.Media.MIME_TYPE, "video/mp4");
                cv.put(android.provider.MediaStore.Video.Media.RELATIVE_PATH,
                        android.os.Environment.DIRECTORY_MOVIES + "/SurfTracker");
                cv.put(android.provider.MediaStore.Video.Media.IS_PENDING, 1);
                uri = getContentResolver().insert(
                        android.provider.MediaStore.Video.Media.EXTERNAL_CONTENT_URI, cv);
                if (uri == null) throw new java.io.IOException("галерея не приняла запись");
                try (java.io.InputStream in = new java.io.FileInputStream(f);
                     java.io.OutputStream out = getContentResolver().openOutputStream(uri)) {
                    byte[] buf = new byte[1 << 20];
                    int n;
                    while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                }
                cv.clear();
                cv.put(android.provider.MediaStore.Video.Media.IS_PENDING, 0);
                getContentResolver().update(uri, cv, null, null);
                try (java.io.OutputStream m = new java.io.FileOutputStream(mark)) {
                    m.write(uri.toString().getBytes("UTF-8"));
                }
            } catch (Throwable t) {
                Log.e(TAG, "публикация: " + t);
                final String msg = LiveStatus.shortError(String.valueOf(t));
                runOnUiThread(() -> toast("Не опубликовалось: " + msg));
                return;
            }
            final android.net.Uri u = uri;
            runOnUiThread(() -> { if (!openUri(u)) toast("Видео в галерее: Movies/SurfTracker"); });
        }).start();
    }

    private boolean openUri(android.net.Uri u) {
        try {
            android.content.Intent i = new android.content.Intent(android.content.Intent.ACTION_VIEW);
            i.setDataAndType(u, "video/mp4");
            i.addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION);
            startActivity(i);
            return true;
        } catch (Throwable t) { Log.e(TAG, "открытие: " + t); return false; }
    }

    private void openVideo(File f) {
        try {
            publishAndOpen(f);
        } catch (Throwable t) {
            // Внешний проигрыватель может не принять file:// на новых Android.
            // Сообщаем путь, а не молчим: путь позволяет открыть файл вручную.
            toast("Не открылось. Файл: " + f.getAbsolutePath());
            Log.e(TAG, "видео: " + t);
        }
    }

    private void openDetails(File runDir) {
        startActivity(new android.content.Intent(this, RunDetailActivity.class)
                .putExtra("dir", runDir.getAbsolutePath()));
    }

    private void toast(String s) {
        android.widget.Toast.makeText(this, s, android.widget.Toast.LENGTH_LONG).show();
    }

    // Разбор json и сводка живут в RunJson — там их достаёт стенд
    // tools/windowing/run_json_check. Внутри активности их нельзя было бы ни
    // запустить, ни проверить.

    static String read(File f) { return RunJson.read(f); }
    static double jsonNum(String json, String key) { return RunJson.num(json, key); }
    static boolean jsonBool(String json, String key) { return RunJson.bool(json, key); }
    static String jsonStr(String json, String key) { return RunJson.str(json, key); }
    static String mmss(double sec) { return RunJson.mmss(sec); }
    static String fmt1(double v) { return RunJson.fmt1(v); }
}
