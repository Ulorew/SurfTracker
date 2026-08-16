package com.surftracker.camfps;

import android.app.Activity;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Настройки прогона на экране телефона.
 *
 * ЗАЧЕМ. Любая проба — другой коэффициент петли, другое качество записи,
 * прогон без мотора — требовала правки константы в исходнике и полной
 * пересборки apk. То есть попробовать что-то в поле было нельзя в принципе:
 * с собой нет ни ноутбука, ни сборки.
 *
 * ЧТО ЗДЕСЬ НЕ ХРАНИТСЯ. Значения не пишутся в настройки прогона, пока не
 * нажато «Сохранить». Поле, применяющееся по мере набора, означает, что
 * недонабранное число («1» на пути к «1.2») успевает стать настройкой.
 *
 * ПОРЯДОК СТАРШИНСТВА (RunSettings): переданное в интенте всегда бьёт
 * сохранённое здесь. Иначе прогон, запущенный командой с ноутбука, молча
 * шёл бы с числами, натыканными на экране, и объяснить расхождение
 * результатов было бы нечем.
 */
public class SettingsActivity extends Activity {

    private final Map<String, android.view.View> fields = new LinkedHashMap<>();
    private SharedPreferences prefs;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        prefs = getSharedPreferences("прогон", MODE_PRIVATE);

        ScrollView sv = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(28, 28, 28, 40);
        sv.addView(root);

        String group = null;
        for (RunSettings.Item it : RunSettings.SPEC) {
            if (!it.group.equals(group)) {
                group = it.group;
                TextView g = new TextView(this);
                g.setTextSize(24);
                g.setPadding(0, 34, 0, 8);
                g.setText(group);
                root.addView(g);
            }
            root.addView(field(it));
        }

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setPadding(0, 40, 0, 0);
        row.addView(btn("Сохранить", v -> { save(); finish(); }));
        row.addView(btn("Сбросить всё", v -> { prefs.edit().clear().apply(); recreate(); }));
        root.addView(row);

        TextView note = new TextView(this);
        note.setTextSize(15);
        note.setPadding(0, 24, 0, 0);
        note.setText("Параметр, переданный при запуске с ноутбука, всегда важнее "
                + "сохранённого здесь. Пустое поле означает «не задано» — "
                + "берётся умолчание.");
        root.addView(note);

        setContentView(sv);
    }

    /** Одна настройка: подпись, поле нужного вида, подсказка. */
    private android.view.View field(RunSettings.Item it) {
        LinearLayout c = new LinearLayout(this);
        c.setOrientation(LinearLayout.VERTICAL);
        c.setPadding(0, 12, 0, 12);

        String cur = prefs.getString(it.key, it.def);

        if (it.type == RunSettings.BOOL) {
            android.widget.CheckBox cb = new android.widget.CheckBox(this);
            cb.setTextSize(19);
            cb.setText(it.label);
            cb.setChecked(RunSettings.asBool(cur));
            fields.put(it.key, cb);
            c.addView(cb);
        } else if (it.type == RunSettings.CHOICE) {
            c.addView(label(it.label));
            android.widget.Spinner sp = new android.widget.Spinner(this);
            String[] shown = new String[it.choices.length];
            for (int i = 0; i < shown.length; i++)
                shown[i] = it.choices[i].isEmpty() ? "(нет)" : it.choices[i];
            android.widget.ArrayAdapter<String> ad = new android.widget.ArrayAdapter<>(
                    this, android.R.layout.simple_spinner_dropdown_item, shown);
            sp.setAdapter(ad);
            for (int i = 0; i < it.choices.length; i++)
                if (it.choices[i].equals(cur)) sp.setSelection(i);
            fields.put(it.key, sp);
            c.addView(sp);
        } else {
            c.addView(label(it.label));
            android.widget.EditText e = new android.widget.EditText(this);
            e.setTextSize(19);
            e.setSingleLine(true);
            if (it.type == RunSettings.INT)
                e.setInputType(android.text.InputType.TYPE_CLASS_NUMBER
                        | android.text.InputType.TYPE_NUMBER_FLAG_SIGNED);
            else if (it.type == RunSettings.FLOAT)
                e.setInputType(android.text.InputType.TYPE_CLASS_NUMBER
                        | android.text.InputType.TYPE_NUMBER_FLAG_DECIMAL
                        | android.text.InputType.TYPE_NUMBER_FLAG_SIGNED);
            e.setText(cur);
            e.setHint(it.def.isEmpty() ? "не задано" : ("умолчание " + it.def));
            fields.put(it.key, e);
            c.addView(e);
        }

        if (it.hint != null) {
            TextView h = new TextView(this);
            h.setTextSize(14);
            h.setText(it.hint);
            c.addView(h);
        }
        return c;
    }

    private TextView label(String s) {
        TextView t = new TextView(this);
        t.setTextSize(17);
        t.setText(s);
        return t;
    }

    private android.widget.Button btn(String text, android.view.View.OnClickListener l) {
        android.widget.Button b = new android.widget.Button(this);
        b.setTextSize(18);
        b.setText(text);
        b.setOnClickListener(l);
        b.setLayoutParams(new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        return b;
    }

    /**
     * Сохранение.
     *
     * Значение, равное умолчанию, НЕ записывается, а удаляется. Иначе экран
     * молча закрепил бы сегодняшнее умолчание навсегда: поменяв его потом в
     * коде, мы не увидели бы изменения ни на одном телефоне, где экран хоть
     * раз открывали и сохраняли.
     */
    private void save() {
        SharedPreferences.Editor ed = prefs.edit();
        for (RunSettings.Item it : RunSettings.SPEC) {
            android.view.View v = fields.get(it.key);
            String val;
            if (v instanceof android.widget.CheckBox)
                val = String.valueOf(((android.widget.CheckBox) v).isChecked());
            else if (v instanceof android.widget.Spinner) {
                int i = ((android.widget.Spinner) v).getSelectedItemPosition();
                val = (i >= 0 && i < it.choices.length) ? it.choices[i] : it.def;
            } else
                val = ((android.widget.EditText) v).getText().toString().trim();

            if (val.equals(it.def)) ed.remove(it.key);
            else ed.putString(it.key, val);
        }
        ed.apply();
        android.widget.Toast.makeText(this, "Настройки сохранены",
                android.widget.Toast.LENGTH_SHORT).show();
    }
}
