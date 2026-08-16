package com.surftracker.camfps;

/**
 * Параметры прогона: описание, умолчания и правило разрешения значения.
 *
 * ЗАЧЕМ ТАБЛИЦЕЙ. Умолчания были рассыпаны литералами по вызовам
 * getIntent().getIntExtra("seconds", 60) — двадцать восемь штук в разных
 * местах файла. Узнать, с чем прогон пойдёт, если параметр не передать, можно
 * было только вычитав весь TrackActivity; поменять умолчание — только найдя
 * все места. Здесь оно одно и видно целиком.
 *
 * ПРАВИЛО РАЗРЕШЕНИЯ — главное в этом файле:
 *
 *   1. параметр передан в интенте  -> берём его;
 *   2. иначе сохранён на экране    -> берём сохранённый;
 *   3. иначе                       -> умолчание отсюда.
 *
 * Порядок именно такой, чтобы скрипты с ноутбука работали БУКВАЛЬНО как
 * раньше: они передают параметры явно, значит попадают в пункт 1, и никакие
 * настройки, натыканные на экране, их не переопределяют. Обратный порядок
 * означал бы, что прогон, запущенный командой, молча идёт с чужими числами —
 * и объяснить расхождение результатов было бы нечем.
 *
 * Умолчания в SPEC обязаны совпадать с прежними литералами. Это не
 * пожелание: несовпадение изменило бы поведение всех прошлых команд разом.
 * На это есть проверка в стенде run_settings_check.
 */
public final class RunSettings {

    private RunSettings() {}

    /** Откуда берутся значения. Абстракция ради проверяемости: ни Intent, ни
     *  SharedPreferences нельзя создать на ноутбуке. */
    public interface Source {
        boolean has(String key);
        String get(String key);
    }

    public static final char INT = 'i', FLOAT = 'f', BOOL = 'b', TEXT = 's', CHOICE = 'c';

    public static final class Item {
        public final String key, label, group, def, hint;
        public final char type;
        public final String[] choices;

        Item(String group, String key, char type, String label, String def,
             String hint, String[] choices) {
            this.group = group; this.key = key; this.type = type;
            this.label = label; this.def = def; this.hint = hint; this.choices = choices;
        }
    }

    static Item it(String g, String k, char t, String l, String d, String h) {
        return new Item(g, k, t, l, d, h, null);
    }

    static Item ch(String g, String k, String l, String d, String h, String... c) {
        return new Item(g, k, CHOICE, l, d, h, c);
    }

    /**
     * Что показывать на экране. Здесь НЕ все параметры TrackActivity: отладочные
     * (flow, nocam, cam_w, spin) остаются только для интента. Экран, на котором
     * лежит всё подряд, перестаёт отвечать на вопрос «что тут обычно меняют».
     */
    public static final Item[] SPEC = {
        it("Прогон", "tag",      TEXT,  "Метка", "", "к имени папки добавится время"),
        it("Прогон", "seconds",  INT,   "Длительность, с", "60", null),
        it("Прогон", "video",    BOOL,  "Писать видео", "false", null),
        ch("Прогон", "quality",  "Качество записи", "2160",
           "ниже — меньше нагрев и пропуски кадров", "720", "1080", "1440", "2160"),
        ch("Прогон", "scen",     "Реплики суфлёра", "",
           "голосовой сценарий для наблюдателя",
           "", "проводка", "выбег", "окно", "двое", "знак"),
        it("Прогон", "rec",      BOOL,  "Писать кадры модели", "false",
           "то, что видела сеть; много файлов"),

        it("Слежение", "k",       FLOAT, "Коэффициент петли K", "1.2",
           "больше — резче доводка, но ближе к раскачке"),
        it("Слежение", "sign",    INT,   "Знак", "-1", "-1 или 1; проверяется прогоном"),
        it("Слежение", "side",    INT,   "Стартовое окно, px", "1280",
           "дальше окно ведёт трекер"),
        it("Слежение", "relost",  FLOAT, "Перезахват после потери, с", "1.5",
           "сколько ждать, прежде чем брать новую цель"),
        it("Слежение", "dwell",   FLOAT, "Удержание, с", "0.18", null),
        it("Слежение", "coast",   FLOAT, "Выбег", "0.4", null),
        it("Слежение", "sync",    BOOL,  "Синхронизация с энкодером", "true",
           "вычитать собственный поворот стенда; выключать только для сравнения"),

        it("Мотор", "dry",   BOOL, "Без мотора", "false",
           "слежение считается, команды не шлются"),
        it("Мотор", "home",  BOOL, "Возврат в исходное", "true", null),
        it("Мотор", "mac",   TEXT, "MAC модуля", "", "пусто — искать сохранённый"),

        it("Модель", "model",   TEXT, "Файл модели", "person_w8a32.tflite", null),
        it("Модель", "threads", INT,  "Потоков", "1", null),
        it("Модель", "xnn",     BOOL, "XNNPACK", "false", null),
    };

    public static Item find(String key) {
        for (Item i : SPEC) if (i.key.equals(key)) return i;
        return null;
    }

    /** Значение по правилу «интент -> сохранённое -> умолчание». */
    public static String resolve(Source intent, Source saved, String key) {
        if (intent != null && intent.has(key)) {
            String v = intent.get(key);
            if (v != null) return v;
        }
        if (saved != null && saved.has(key)) {
            String v = saved.get(key);
            if (v != null && !v.isEmpty()) return v;
        }
        Item i = find(key);
        return i == null ? null : i.def;
    }

    public static int asInt(String v, int def) {
        try { return (int) Double.parseDouble(v.trim()); } catch (Throwable t) { return def; }
    }

    public static float asFloat(String v, float def) {
        try { return Float.parseFloat(v.trim().replace(',', '.')); }
        catch (Throwable t) { return def; }
    }

    public static boolean asBool(String v) {
        return "true".equalsIgnoreCase(v) || "1".equals(v) || "да".equalsIgnoreCase(v);
    }
}
