package com.surftracker.camfps;

import android.app.Activity;
import android.os.Bundle;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.File;

/**
 * Один прогон целиком: итог, настройки запуска и что лежит в папке.
 *
 * Настройки показываются НАРЯДУ с результатом и на том же экране. Прогон, у
 * которого не видно, с какими порогами и коэффициентом он шёл, сравнивать не
 * с чем: два прогона с разной долей на цели могут отличаться настройкой, а не
 * поведением, и по одной доле этого не увидеть.
 */
public class RunDetailActivity extends Activity {

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        File dir = new File(getIntent().getStringExtra("dir"));
        String json = RunsActivity.read(new File(dir, "прогон.json"));

        ScrollView sv = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(28, 28, 28, 28);
        sv.addView(root);

        add(root, 28, dir.getName());

        StringBuilder s = new StringBuilder();
        if (json == null) {
            s.append("прогон.json не читается");
        } else {
            s.append("— ИТОГ —\n");
            row(s, "тактов", num(json, "тактов"));
            row(s, "доля на цели", pct(json, "доля_на_цели"));
            row(s, "потерь", num(json, "потерь"));
            row(s, "повторных захватов", num(json, "повторных_захватов"));
            row(s, "промахов подряд макс", num(json, "промахов_подряд_макс"));
            row(s, "длительность", RunsActivity.mmss(RunsActivity.jsonNum(json, "длительность_с")));
            row(s, "окно, медиана px", num(json, "окно_медиана"));
            row(s, "инференс, мс", num(json, "инференс_мс_медиана"));
            row(s, "такт, мс", num(json, "такт_мс_медиана"));
            row(s, "батарея, °C", RunsActivity.fmt1(RunsActivity.jsonNum(json, "батарея_нагрев")));

            s.append("\n— СВЯЗЬ С МОТОРОМ —\n");
            row(s, "телеметрии", num(json, "телеметрии"));
            row(s, "протухших нулей", num(json, "протухших_нулей"));
            double p50 = RunsActivity.jsonNum(json, "p50");
            if (p50 > 0) row(s, "rtt p50/p95, мс",
                    RunsActivity.fmt1(p50) + " / " + RunsActivity.fmt1(RunsActivity.jsonNum(json, "p95")));

            s.append("\n— НАСТРОЙКИ ЗАПУСКА —\n");
            for (String k : new String[]{"модель", "сенсор", "запись", "поле_зрения_град",
                                          "K", "знак", "окно", "секунд", "выбег",
                                          "потоков", "xnnpack", "режим"}) {
                String v = RunsActivity.jsonStr(json, k);
                if (v == null) {
                    double n = RunsActivity.jsonNum(json, k);
                    if (n != 0) row(s, k, RunsActivity.fmt1(n));
                } else row(s, k, v);
            }

            String err = RunsActivity.jsonStr(json, "ошибка");
            if (err != null) s.append("\n— ОШИБКА —\n").append(err).append('\n');
        }

        s.append("\n— ФАЙЛЫ —\n");
        File[] fs = dir.listFiles();
        if (fs != null) {
            java.util.Arrays.sort(fs, (a, c) -> a.getName().compareTo(c.getName()));
            for (File f : fs)
                row(s, f.getName(), f.isDirectory()
                        ? (count(f) + " шт")
                        : (f.length() / 1024 >= 1024
                            ? (f.length() / 1024 / 1024) + " МБ"
                            : Math.max(1, f.length() / 1024) + " КБ"));
        }
        s.append("\nпуть: ").append(dir.getAbsolutePath());

        add(root, 17, s.toString());
        setContentView(sv);
    }

    private static int count(File d) {
        File[] f = d.listFiles();
        return f == null ? 0 : f.length;
    }

    private String num(String json, String key) {
        double v = RunsActivity.jsonNum(json, key);
        return (v == Math.floor(v)) ? String.valueOf((long) v) : RunsActivity.fmt1(v);
    }

    private String pct(String json, String key) {
        return Math.round(RunsActivity.jsonNum(json, key) * 100) + "%";
    }

    private static void row(StringBuilder s, String k, String v) {
        s.append(k).append(": ").append(v).append('\n');
    }

    private void add(LinearLayout root, int size, String text) {
        TextView t = new TextView(this);
        t.setTextSize(size);
        t.setPadding(0, 12, 0, 0);
        t.setTypeface(android.graphics.Typeface.MONOSPACE);
        t.setText(text);
        root.addView(t);
    }
}
