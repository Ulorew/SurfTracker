package com.surftracker.camfps;

/**
 * Калман по углам — перенос уровня 2 из офлайнового прода (FILTER_LEVEL = 2).
 * БЕЗ Android: класс обязан быть проверяемым стендом.
 *
 * Состояние пятимерное: (θ, θ̇, φ, φ̇, log h). Размер живёт В СОСТОЯНИИ, а не
 * в отдельной медленной EMA: в этой ветке его роль играет log h со своим шумом
 * процесса, и заводить второй источник той же величины нельзя.
 *
 * ЗАЧЕМ он здесь вообще. Ковариация нужна не сама по себе, а махаланобисову
 * гейту: в отличие от фиксированного радиуса приёма, гейт САМ расширяется,
 * когда фильтр не уверен, и сам сужается на плотном треке. На прогоне
 * 18 августа фиксированный радиус отверг уверенные детекции (0.90 и 0.92) на
 * тактах 409 и 419 — человек стоял в кадре, а убеждение к тому моменту уехало.
 *
 * ЕДИНИЦЫ. Офлайн живёт в радианах и получает шум из метрических допущений
 * (2 м/с² на опорных 50 м; 20 м/с на 20 м). Перенос живёт в ПИКСЕЛЯХ СЕНСОРА,
 * поэтому обе величины пересчитываются через PX_PER_RAD. Это единственное
 * место переноса, где масштаб важен: уровень 1 масштабно-инвариантен, а
 * Калман — нет, и стенд сличения обязан задавать обеим сторонам одно и то же
 * число, иначе он будет сравнивать разные фильтры и радостно сходиться.
 *
 * Анизотропная Q (KALMAN_ANISOTROPIC_Q) в прод-конфигурации выключена и сюда
 * НЕ переносилась: переносить неиспользуемую ветку значит завести код, который
 * никто не проверяет.
 */
public final class KalmanTracker {

    // Порядок состояния — как в офлайне: IDX_TH, IDX_DTH, IDX_PH, IDX_DPH, IDX_LOGH
    private static final int TH = 0, DTH = 1, PH = 2, DPH = 3, LOGH = 4;

    /** Пикселей на радиан. Задаётся снаружи: fPx камеры или значение стенда. */
    public static double PX_PER_RAD = 1300.0;

    // Константы прод-конфигурации tracking-v2, безразмерные — как есть.
    public static final double R_POS_SIZE_FRAC = 0.30;   // KALMAN_R_POS_SIZE_FRAC
    public static final double R_LOGH = 0.25;            // KALMAN_R_LOGH
    public static final double GATE_CHI2 = 9.21;         // KALMAN_GATE_CHI2, 2 dof, p=0.01
    public static final double LOGH_RADIAL_FRAC = 0.30;  // KALMAN_LOGH_RADIAL_FRAC
    // Метрические допущения офлайна, приведённые к угловым:
    //   2.0 м/с² на опорных 50 м -> 0.04 рад/с²
    //   20 м/с на минимальных 20 м -> 1.0 рад/с
    public static final double SIGMA_ACCEL_RAD = 2.0 / 50.0;
    public static final double V_MAX_RAD = 20.0 / 20.0;
    public static final double SEED_SIZE_FALLBACK_RAD = 0.02;

    public boolean initialized;
    private final double[] x = new double[5];
    private final double[][] P = new double[5][5];

    private static double sigmaAccel() { return SIGMA_ACCEL_RAD * PX_PER_RAD; }
    private static double vMax()       { return V_MAX_RAD * PX_PER_RAD; }
    private static double sigmaLoghRate() { return LOGH_RADIAL_FRAC * vMax() / PX_PER_RAD; }

    public double cx() { return x[TH]; }
    public double cy() { return x[PH]; }
    public double vx() { return x[DTH]; }
    public double vy() { return x[DPH]; }
    public double size() { return Math.exp(x[LOGH]); }

    /**
     * Прижать ПОЛОЖЕНИЕ к видимой области. Скорость и ковариация не трогаются.
     *
     * Пишет прямо в состояние, а не в чью-то копию: офлайн делает ровно это
     * (clamp_belief_to_view присваивает filter.cx/cy, а у Калмана это сеттеры
     * на x[TH]/x[PH]). Пока прижимались только зеркальные поля трекера,
     * внутреннее состояние фильтра жило своей жизнью, и предсказание
     * расходилось с офлайном уже на третьем такте.
     */
    public void setPos(double px, double py) { x[TH] = px; x[PH] = py; }

    public void seed(double mx, double my, double mSize) {
        double h = (mSize > 0) ? mSize : SEED_SIZE_FALLBACK_RAD * PX_PER_RAD;
        double r = (R_POS_SIZE_FRAC * h) * (R_POS_SIZE_FRAC * h);
        x[TH] = mx; x[DTH] = 0; x[PH] = my; x[DPH] = 0; x[LOGH] = Math.log(h);
        for (int i = 0; i < 5; i++) java.util.Arrays.fill(P[i], 0);
        // Начальная скорость НОЛЬ — это не знание, а его отсутствие, поэтому
        // её дисперсия берётся широкой.
        P[TH][TH] = r; P[PH][PH] = r;
        P[DTH][DTH] = vMax() * vMax(); P[DPH][DPH] = vMax() * vMax();
        P[LOGH][LOGH] = R_LOGH * R_LOGH;
        initialized = true;
    }

    // --- матрицы модели ---------------------------------------------------

    private static double[][] F(double dt) {
        double[][] f = eye(5);
        f[TH][DTH] = dt; f[PH][DPH] = dt;
        return f;
    }

    /** Дискретный белый шум по ускорению (CWNA) плюс блуждание по log h. */
    private static double[][] Q(double dt) {
        double s2 = sigmaAccel() * sigmaAccel();
        double[][] q = new double[5][5];
        int[] pos = { TH, PH }, vel = { DTH, DPH };
        for (int i = 0; i < 2; i++) {
            // изотропная Q: недиагональные члены между осями равны нулю
            q[pos[i]][pos[i]] = dt * dt * dt * dt / 4.0 * s2;
            q[pos[i]][vel[i]] = dt * dt * dt / 2.0 * s2;
            q[vel[i]][pos[i]] = dt * dt * dt / 2.0 * s2;
            q[vel[i]][vel[i]] = dt * dt * s2;
        }
        double sl = sigmaLoghRate();
        q[LOGH][LOGH] = sl * sl * dt;
        return q;
    }

    /** Сигма положения — доля размера ЦЕЛИ: крупная цель мерится хуже. */
    private static double rPos(double mSize) {
        double s = R_POS_SIZE_FRAC * Math.max(mSize, 1e-9);
        return s * s;
    }

    /** Моменты предсказания: (F·x, F·P·Fᵀ + Q). */
    private void predictMoments(double dt, double[] xp, double[][] Pp) {
        double[][] f = F(dt);
        double[] t = mul(f, x);
        System.arraycopy(t, 0, xp, 0, 5);
        double[][] fp = mul(f, P);
        double[][] fpf = mulT(fp, f);
        double[][] q = Q(dt);
        for (int i = 0; i < 5; i++)
            for (int j = 0; j < 5; j++) Pp[i][j] = fpf[i][j] + q[i][j];
    }

    /** Предсказание позиции БЕЗ изменения состояния. */
    public double predX(double dt) { return x[TH] + x[DTH] * dt; }
    public double predY(double dt) { return x[PH] + x[DPH] * dt; }

    /**
     * Такт без измерения: предсказание становится состоянием, неопределённость
     * растёт на Q, модуль скорости затухает.
     *
     * Затухание домножает СОСТОЯНИЕ после предсказания, матрицу F не трогает:
     * иначе оно попало бы и в ковариацию, а нужно обратное — неопределённость
     * обязана расти, сжимается только скорость.
     */
    public void advance(double dt, double tauSec) {
        double[] xp = new double[5]; double[][] Pp = new double[5][5];
        predictMoments(dt, xp, Pp);
        System.arraycopy(xp, 0, x, 0, 5);
        for (int i = 0; i < 5; i++) System.arraycopy(Pp[i], 0, P[i], 0, 5);
        if (tauSec > 0 && dt > 0) {
            double k = Math.exp(-dt / tauSec);
            x[DTH] *= k; x[DPH] *= k;
        }
    }

    /**
     * Квадрат махаланобисова расстояния кандидата до предсказания, 2 степени
     * свободы. Сравнивается с GATE_CHI2.
     */
    public double gateDistance2(double mx, double my, double mSize, double dt) {
        double[] xp = new double[5]; double[][] Pp = new double[5][5];
        predictMoments(dt, xp, Pp);
        double r = rPos(mSize);
        double a = Pp[TH][TH] + r, b = Pp[TH][PH], c = Pp[PH][TH], d = Pp[PH][PH] + r;
        double det = a * d - b * c;
        if (Math.abs(det) < 1e-18) return Double.MAX_VALUE;
        double yx = mx - xp[TH], yy = my - xp[PH];
        // решение 2x2 системы S·u = y и скалярное произведение yᵀu
        double ux = (d * yx - b * yy) / det;
        double uy = (-c * yx + a * yy) / det;
        return yx * ux + yy * uy;
    }

    /** Обновление по измерению (положение и размер), форма Джозефа. */
    public void update(double mx, double my, double dt, double mSize) {
        if (!initialized) { seed(mx, my, mSize); return; }
        double[] xp = new double[5]; double[][] Pp = new double[5][5];
        predictMoments(dt, xp, Pp);

        boolean withSize = mSize > 0;
        int m = withSize ? 3 : 2;
        double[][] H = new double[m][5];
        H[0][TH] = 1.0; H[1][PH] = 1.0;
        double[] z = new double[m];
        z[0] = mx; z[1] = my;
        double[][] R = new double[m][m];
        if (withSize) {
            H[2][LOGH] = 1.0;
            z[2] = Math.log(mSize);
            R[0][0] = rPos(mSize); R[1][1] = rPos(mSize); R[2][2] = R_LOGH * R_LOGH;
        } else {
            double sz = Math.exp(xp[LOGH]);
            R[0][0] = rPos(sz); R[1][1] = rPos(sz);
        }

        double[][] PpHt = new double[5][m];
        for (int i = 0; i < 5; i++)
            for (int j = 0; j < m; j++) {
                double v = 0;
                for (int k = 0; k < 5; k++) v += Pp[i][k] * H[j][k];
                PpHt[i][j] = v;
            }
        double[][] S = new double[m][m];
        for (int i = 0; i < m; i++)
            for (int j = 0; j < m; j++) {
                double v = R[i][j];
                for (int k = 0; k < 5; k++) v += H[i][k] * PpHt[k][j];
                S[i][j] = v;
            }
        double[][] Si = inv(S, m);
        double[][] K = new double[5][m];
        for (int i = 0; i < 5; i++)
            for (int j = 0; j < m; j++) {
                double v = 0;
                for (int k = 0; k < m; k++) v += PpHt[i][k] * Si[k][j];
                K[i][j] = v;
            }
        double[] y = new double[m];
        for (int i = 0; i < m; i++) {
            double v = z[i];
            for (int k = 0; k < 5; k++) v -= H[i][k] * xp[k];
            y[i] = v;
        }
        for (int i = 0; i < 5; i++) {
            double v = xp[i];
            for (int j = 0; j < m; j++) v += K[i][j] * y[j];
            x[i] = v;
        }
        // Форма Джозефа: симметричность и положительная определённость P не
        // теряются от накопления округлений за сотни тактов.
        double[][] IKH = eye(5);
        for (int i = 0; i < 5; i++)
            for (int j = 0; j < 5; j++) {
                double v = 0;
                for (int k = 0; k < m; k++) v += K[i][k] * H[k][j];
                IKH[i][j] -= v;
            }
        double[][] left = mulT(mul(IKH, Pp), IKH);
        for (int i = 0; i < 5; i++)
            for (int j = 0; j < 5; j++) {
                double v = 0;
                for (int a2 = 0; a2 < m; a2++)
                    for (int b2 = 0; b2 < m; b2++) v += K[i][a2] * R[a2][b2] * K[j][b2];
                P[i][j] = left[i][j] + v;
            }
    }

    // --- мелкая матричная арифметика --------------------------------------

    private static double[][] eye(int n) {
        double[][] m = new double[n][n];
        for (int i = 0; i < n; i++) m[i][i] = 1.0;
        return m;
    }

    private static double[] mul(double[][] a, double[] v) {
        double[] r = new double[a.length];
        for (int i = 0; i < a.length; i++) {
            double s = 0;
            for (int k = 0; k < v.length; k++) s += a[i][k] * v[k];
            r[i] = s;
        }
        return r;
    }

    private static double[][] mul(double[][] a, double[][] b) {
        int n = a.length, m = b[0].length, p = b.length;
        double[][] r = new double[n][m];
        for (int i = 0; i < n; i++)
            for (int j = 0; j < m; j++) {
                double s = 0;
                for (int k = 0; k < p; k++) s += a[i][k] * b[k][j];
                r[i][j] = s;
            }
        return r;
    }

    /** a · bᵀ */
    private static double[][] mulT(double[][] a, double[][] b) {
        int n = a.length, m = b.length, p = a[0].length;
        double[][] r = new double[n][m];
        for (int i = 0; i < n; i++)
            for (int j = 0; j < m; j++) {
                double s = 0;
                for (int k = 0; k < p; k++) s += a[i][k] * b[j][k];
                r[i][j] = s;
            }
        return r;
    }

    /** Обращение 2x2 или 3x3 — больше здесь не встречается. */
    private static double[][] inv(double[][] a, int n) {
        double[][] r = new double[n][n];
        if (n == 2) {
            double det = a[0][0] * a[1][1] - a[0][1] * a[1][0];
            if (Math.abs(det) < 1e-18) det = 1e-18;
            r[0][0] = a[1][1] / det;  r[0][1] = -a[0][1] / det;
            r[1][0] = -a[1][0] / det; r[1][1] = a[0][0] / det;
            return r;
        }
        // Гаусс с частичным выбором ведущего: 3x3, поэтому без изысков
        double[][] m = new double[n][2 * n];
        for (int i = 0; i < n; i++) {
            System.arraycopy(a[i], 0, m[i], 0, n);
            m[i][n + i] = 1.0;
        }
        for (int c = 0; c < n; c++) {
            int piv = c;
            for (int i = c + 1; i < n; i++) if (Math.abs(m[i][c]) > Math.abs(m[piv][c])) piv = i;
            double[] t = m[c]; m[c] = m[piv]; m[piv] = t;
            double d = m[c][c];
            if (Math.abs(d) < 1e-18) d = 1e-18;
            for (int j = 0; j < 2 * n; j++) m[c][j] /= d;
            for (int i = 0; i < n; i++) {
                if (i == c) continue;
                double f = m[i][c];
                for (int j = 0; j < 2 * n; j++) m[i][j] -= f * m[c][j];
            }
        }
        for (int i = 0; i < n; i++) System.arraycopy(m[i], n, r[i], 0, n);
        return r;
    }
}
