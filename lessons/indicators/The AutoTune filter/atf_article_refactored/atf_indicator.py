import math
import backtrader as bt


class AutoTuneFilter(bt.Indicator):
    """
    John Ehlers - AutoTune Filter.

    Линии индикатора:
    - bp      : tuned band-pass filter
    - filt    : high-pass filtered series
    - mincorr : минимальная rolling autocorrelation
    - dc      : dominant cycle

    В конце файла есть AutoTuneDemoStrategy. Она не торгует, а только выводит
    линии индикатора на график через CerebroView, чтобы сверить значения с
    TradingView или другой "эталонной" реализацией.
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

    def __init__(self):
        # HP-коэффициенты зависят только от window и не меняются от бара к бару.
        window = int(self.p.window)
        w = 1.414 * math.pi / window
        q = math.exp(-w)
        self._hp_c1 = 2.0 * q * math.cos(w)
        self._hp_c2 = q * q
        self._hp_a0 = 0.25 * (1.0 + self._hp_c1 + self._hp_c2)
        # Кэшируем int(window) для горячего пути.
        self._window = window
        self._autocorr_warmup = 2 * window + 1

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
        window = self._window
        bandwidth = float(self.p.bandwidth)

        filt_line = self.l.filt
        bp_line = self.l.bp
        data_line = self.data

        # =============================================================
        # 1) HIGH-PASS FILTER
        # =============================================================

        c1 = self._hp_c1
        c2 = self._hp_c2
        a0 = self._hp_a0

        if len(self) < 5:
            filt = 0.0
        else:
            src0 = data_line[0]
            src1 = data_line[-1]
            src2 = data_line[-2]

            filt = (
                a0 * (src0 - 2.0 * src1 + src2)
                + c1 * self._prev(filt_line, -1, 0.0)
                - c2 * self._prev(filt_line, -2, 0.0)
            )

        filt_line[0] = filt

        # =============================================================
        # 2) ROLLING AUTOCORRELATION
        # =============================================================

        mincorr = 1.0
        best_lag = 1
        warm = len(self) >= self._autocorr_warmup

        for lag in range(1, window + 1):
            sx = sy = sxx = sxy = syy = 0.0

            if warm:
                for j in range(window):
                    x = filt_line[-j]
                    y = filt_line[-(lag + j)]
                    sx += x
                    sy += y
                    sxx += x * x
                    sxy += x * y
                    syy += y * y
            else:
                for j in range(window):
                    x = self._prev(filt_line, -j, 0.0)
                    y = self._prev(filt_line, -(lag + j), 0.0)
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
            bp_line[0] = 0.0
            return

        w0 = 2.0 * math.pi / dc
        l1 = math.cos(w0)
        g1 = math.cos(w0 * bandwidth)

        # Разумная численная защита: если g1 почти ноль,
        # сохраняем предыдущее значение, чтобы не словить деление на 0.
        if abs(g1) < 1e-12:
            bp_line[0] = self._prev(bp_line, -1, 0.0)
            return

        inner = 1.0 / (g1 * g1) - 1.0

        # Ещё одна float-защита:
        # если inner стал чуть меньше нуля только из-за погрешности,
        # считаем его нулём.
        if inner < 0.0 and abs(inner) < 1e-12:
            inner = 0.0
        elif inner < 0.0:
            bp_line[0] = self._prev(bp_line, -1, 0.0)
            return

        s1 = 1.0 / g1 - math.sqrt(inner)

        bp = (
            0.5 * (1.0 - s1) * (data_line[0] - data_line[-2])
            + l1 * (1.0 + s1) * self._prev(bp_line, -1, 0.0)
            - s1 * self._prev(bp_line, -2, 0.0)
        )
        bp_line[0] = bp


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

    # def next(self):
    #     dt = self.data.datetime.datetime(0)
    #     print(
    #         f'{dt} | close={self.data.close[0]:.2f} | '
    #         f'bp={self.atf.bp[0]:.6f} | '
    #         f'filt={self.atf.filt[0]:.6f} | '
    #         f'mincorr={self.atf.mincorr[0]:.6f} | '
    #         f'dc={self.atf.dc[0]:.2f}'
    #     )


if __name__ == '__main__':
    from moex_store import MoexStore
    from cerebroview import plot

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
    cerebro.addstrategy(AutoTuneDemoStrategy)
    cerebro.run()
    plot(cerebro)
