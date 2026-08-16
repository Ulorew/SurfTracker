import com.surftracker.camfps.RunSettings;
import java.util.HashMap;
import java.util.Map;

/**
 * Проверка правила «интент -> сохранённое -> умолчание» и самих умолчаний.
 *
 * Проверять тут есть что ровно по одной причине: экран настроек появился
 * ПОСЛЕ того, как сложились скрипты запуска с ноутбука. Если сохранённое
 * значение начнёт перебивать переданное явно, все прежние команды молча
 * пойдут с чужими числами — и разницу результатов будет не на что списать.
 *
 * Умолчания сверяются со списком, переписанным из прежних литералов
 * TrackActivity. Не «примерно теми же»: несовпадение любого изменило бы
 * поведение всех команд, где параметр не передаётся.
 */
public class RunSettingsCheck {

    static int failed = 0;

    static void eq(String what, Object got, Object want) {
        boolean ok = String.valueOf(got).equals(String.valueOf(want));
        if (!ok) failed++;
        System.out.println((ok ? "  ok   " : "  ПЛОХО ") + what + ": " + got
                + (ok ? "" : "   ожидалось " + want));
    }

    static class Map1 implements RunSettings.Source {
        final Map<String, String> m = new HashMap<>();
        Map1 put(String k, String v) { m.put(k, v); return this; }
        public boolean has(String k) { return m.containsKey(k); }
        public String get(String k) { return m.get(k); }
    }

    /** Литералы, стоявшие в getIntent().getXExtra(...) до появления экрана. */
    static final String[][] WAS = {
        {"seconds", "60"}, {"side", "1280"}, {"k", "1.2"}, {"sign", "-1"},
        {"dry", "false"}, {"coast", "0.4"}, {"relost", "1.5"}, {"dwell", "0.18"},
        {"home", "true"}, {"rec", "false"}, {"video", "false"},
        {"model", "person_w8a32.tflite"}, {"threads", "1"}, {"xnn", "false"},
        {"tag", ""}, {"mac", ""}, {"scen", ""},
    };

    public static void main(String[] args) {
        System.out.println("== умолчания совпадают с прежними литералами ==");
        for (String[] w : WAS) {
            RunSettings.Item i = RunSettings.find(w[0]);
            if (i == null) { failed++; System.out.println("  ПЛОХО нет параметра " + w[0]); continue; }
            eq(w[0], i.def, w[1]);
        }

        System.out.println("== правило разрешения ==");
        Map1 intent = new Map1().put("seconds", "180").put("k", "2.0");
        Map1 saved  = new Map1().put("seconds", "30").put("video", "true").put("sign", "1");

        // 1. интент бьёт сохранённое — иначе команды с ноутбука пойдут не с тем
        eq("интент бьёт сохранённое", RunSettings.resolve(intent, saved, "seconds"), "180");
        eq("интент бьёт умолчание", RunSettings.resolve(intent, saved, "k"), "2.0");
        // 2. сохранённое бьёт умолчание
        eq("сохранённое бьёт умолчание", RunSettings.resolve(intent, saved, "video"), "true");
        eq("сохранённое, знак", RunSettings.resolve(intent, saved, "sign"), "1");
        // 3. ни там, ни там — умолчание
        eq("умолчание", RunSettings.resolve(intent, saved, "relost"), "1.5");
        // ничего не сохранено вовсе — поведение ровно прежнее
        eq("без сохранённых", RunSettings.resolve(intent, null, "sign"), "-1");
        eq("без источников", RunSettings.resolve(null, null, "seconds"), "60");
        // пустая строка в сохранённом — это «не задано», а не «пусто»
        eq("пустое сохранённое не перебивает",
                RunSettings.resolve(null, new Map1().put("model", ""), "model"),
                "person_w8a32.tflite");
        // но пустая МЕТКА — законное значение и приходит из умолчания
        eq("метка по умолчанию пуста", RunSettings.resolve(null, null, "tag"), "");
        eq("неизвестный ключ", RunSettings.resolve(intent, saved, "выдуманный"), "null");

        System.out.println("== разбор значений ==");
        eq("int", RunSettings.asInt("180", -1), 180);
        eq("int из дробного", RunSettings.asInt("180.0", -1), 180);
        eq("int из мусора", RunSettings.asInt("абв", -1), -1);
        eq("float", RunSettings.asFloat("1.2", -1f), 1.2f);
        eq("float с запятой", RunSettings.asFloat("1,2", -1f), 1.2f);
        eq("float с пробелами", RunSettings.asFloat("  2.5 ", -1f), 2.5f);
        eq("bool true", RunSettings.asBool("true"), true);
        eq("bool 1", RunSettings.asBool("1"), true);
        eq("bool false", RunSettings.asBool("false"), false);
        eq("bool мусор", RunSettings.asBool("абв"), false);

        System.out.println("== целостность таблицы ==");
        java.util.Set<String> seen = new java.util.HashSet<>();
        for (RunSettings.Item i : RunSettings.SPEC) {
            if (!seen.add(i.key)) { failed++; System.out.println("  ПЛОХО дубль ключа " + i.key); }
            if (i.type == RunSettings.CHOICE) {
                boolean has = false;
                for (String c : i.choices) if (c.equals(i.def)) has = true;
                if (!has) { failed++; System.out.println("  ПЛОХО умолчание " + i.key
                        + " не входит в список выбора"); }
            }
            // Умолчание обязано разбираться своим же типом: строка "полтора" в
            // числовом поле молча превратилась бы в ноль на устройстве.
            if (i.type == RunSettings.INT && !i.def.isEmpty()
                    && RunSettings.asInt(i.def, Integer.MIN_VALUE) == Integer.MIN_VALUE) {
                failed++; System.out.println("  ПЛОХО умолчание " + i.key + " не целое");
            }
            if (i.type == RunSettings.FLOAT && !i.def.isEmpty()
                    && RunSettings.asFloat(i.def, Float.NaN) != RunSettings.asFloat(i.def, Float.NaN)) {
                failed++; System.out.println("  ПЛОХО умолчание " + i.key + " не число");
            }
        }
        eq("параметров на экране", RunSettings.SPEC.length, 18);

        System.out.println();
        if (failed == 0) System.out.println("ИТОГ: настройки прогона исправны");
        else { System.out.println("ИТОГ: " + failed + " ПРОВЕРОК ПРОВАЛЕНО"); System.exit(1); }
    }
}
