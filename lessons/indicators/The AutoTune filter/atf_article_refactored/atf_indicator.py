import math
import backtrader as bt


class AutoTuneFilter(bt.Indicator):
    """
    John Ehlers - AutoTune Filter.

    Что выдаёт индикатор:
    - bp      : tuned band-pass filter
    - filt    : high-pass filtered series
    - mincorr : минимальная rolling autocorrelation
    - dc      : dominant cycle

    В конце файла есть AutoTuneDemoStrategy. Она не торгует, а только выводит
    линии индикатора на график через CerebroView, чтобы сверить значения с
    TradingView или другой эталонной реализацией.
    """

    lines = ('bp', 'filt', 'mincorr', 'dc')

    params = (
        ('window', 20),
        ('bandwidth', 0.25)
    )

    plotinfo = dict(subplot=True)

    plotlines = dict(
        bp=dict(_name='AutoTune BP'),
        filt=dict(_name='HighPass'),
        mincorr=dict(_name='MinCorr'),
        dc=dict(_name='DominantCycle'),  # , _plotskip=True
    )

    @staticmethod
    def _safe(value, default=0.0):
        """
        Аналог Pine-функции nz().
        """
        if value is None:
            return default

        try:
            if math.isnan(value):
                return default
        except TypeError:
            pass

        return value

    def _prev(self, line, ago=-1, default=0.0):
        """
        Безопасный доступ к прошлым значениям собственной линии индикатора.

        Нужен только для рекурсивных формул:
        - filt использует filt[1] и filt[2]
        - bp использует bp[1] и bp[2]

        Если прошлой истории линии ещё нет, возвращаем default.
        Это соответствует Pine-логике через nz(res[1]) / nz(res[2]).
        """
        bars_back = -ago
        if len(self) > bars_back:
            return self._safe(line[ago], default)

        return default

    def prenext(self):
        self._step()

    def nextstart(self):
        self._step()

    def next(self):
        self._step()

    def _step(self):
        window = int(self.p.window)
        bandwidth = float(self.p.bandwidth)

        # =============================================================
        # 1) HIGH-PASS FILTER
        # =============================================================

        w = 1.414 * math.pi / window
        q = math.exp(-w)
        c1 = 2.0 * q * math.cos(w)
        c2 = q * q
        a0 = 0.25 * (1.0 + c1 + c2)

        if len(self) < 5:
            filt = 0.0
        else:
            src0 = self.data[0]
            src1 = self.data[-1]
            src2 = self.data[-2]

            filt = (
                a0 * (src0 - 2.0 * src1 + src2)
                + c1 * self._prev(self.l.filt, -1, 0.0)
                - c2 * self._prev(self.l.filt, -2, 0.0)
            )

        self.l.filt[0] = filt

        # =============================================================
        # 2) ROLLING AUTOCORRELATION
        # =============================================================

        mincorr = 1.0
        best_lag = 1

        for lag in range(1, window + 1):
            sx = sy = sxx = sxy = syy = 0.0

            for j in range(window):
                x = self._prev(self.l.filt, -j, 0.0)
                y = self._prev(self.l.filt, -(lag + j), 0.0)

                sx += x
                sy += y
                sxx += x * x
                sxy += x * y
                syy += y * y

            cov = window * sxy - sx * sy
            vx = window * sxx - sx * sx
            vy = window * syy - sy * sy

            if vx <= 0.0 or vy <= 0.0:
                corr = 1.0
            else:
                corr = cov / math.sqrt(vx * vy)
                corr = self._safe(corr, 1.0)

            if corr < mincorr:
                mincorr = corr
                best_lag = lag

        self.l.mincorr[0] = mincorr

        # =============================================================
        # 3) DOMINANT CYCLE
        # =============================================================

        dc = 2.0 * best_lag
        prev_dc = self._prev(self.l.dc, -1, dc)
        dc = min(max(dc, prev_dc - 2.0), prev_dc + 2.0)
        self.l.dc[0] = dc

        # =============================================================
        # 4) TUNED BAND-PASS FILTER
        # =============================================================
        if len(self) < 4:
            self.l.bp[0] = 0.0
            return

        w0 = 2.0 * math.pi / dc
        l1 = math.cos(w0)
        g1 = math.cos(w0 * bandwidth)

        # Разумная численная защита: если g1 почти ноль,
        # сохраняем предыдущее значение, чтобы не словить деление на 0.
        if abs(g1) < 1e-12:
            self.l.bp[0] = self._prev(self.l.bp, -1, 0.0)
            return

        inner = 1.0 / (g1 * g1) - 1.0

        # Ещё одна float-защита:
        # если inner стал чуть меньше нуля только из-за погрешности,
        # считаем его нулём.
        if inner < 0.0 and abs(inner) < 1e-12:
            inner = 0.0
        elif inner < 0.0:
            self.l.bp[0] = self._prev(self.l.bp, -1, 0.0)
            return

        s1 = 1.0 / g1 - math.sqrt(inner)

        bp = (
            0.5 * (1.0 - s1) * (self.data[0] - self.data[-2])
            + l1 * (1.0 + s1) * self._prev(self.l.bp, -1, 0.0)
            - s1 * self._prev(self.l.bp, -2, 0.0)
        )
        self.l.bp[0] = bp


class AutoTuneDemoStrategy(bt.Strategy):
    params = (
        ('window', 20),
        ('bandwidth', 0.25),
    )

    def __init__(self):
        self.atf = AutoTuneFilter(
            self.data.close,
            window=self.p.window,
            bandwidth=self.p.bandwidth,
        )


if __name__ == '__main__':
    from moex_store import MoexStore

    cerebro = bt.Cerebro(stdstats=False)

    store = MoexStore()
    data = store.getdata(
        sec_id='RIM6',
        fromdate='2026-01-15',
        todate='2026-05-23',
        tf='1h',
        name='RIM6'
    )

    cerebro.adddata(data)
    cerebro.addstrategy(
        AutoTuneDemoStrategy,
        window=20,
        bandwidth=0.25,
    )
    results = cerebro.run()

    from cerebroview import plot
    plot(cerebro)
