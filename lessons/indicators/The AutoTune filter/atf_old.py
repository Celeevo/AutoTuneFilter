from datetime import datetime
from moex_store import MoexStore
import math
import backtrader as bt

import math
import backtrader as bt

class AutoTuneFilter(bt.Indicator):
    """
    John Ehlers - AutoTune Filter
    Исправленная реализация для Backtrader.

    Главный выход: bp
    Дополнительные линии:
    - filt    : high-pass filtered series
    - mincorr : minimum rolling correlation
    - dc      : dominant cycle
    """

    lines = ('bp', 'filt', 'mincorr', 'dc',)

    params = (
        ('window', 20),
        ('bandwidth', 0.25),
        ('output', 'bp'),
    )

    plotinfo = dict(subplot=True)

    plotlines = dict(
        bp=dict(_name='AutoTune BP', _plotskip=True),
        filt=dict(_name='HighPass'),
        mincorr=dict(_name='MinCorr', _plotskip=True),
        dc=dict(_name='DominantCycle', _plotskip=True),
    )

    def _nz(self, value, default=0.0):
        """Заменяет nan/None на default."""
        if value is None:
            return default
        try:
            if math.isnan(value):
                return default
        except TypeError:
            pass
        return value

    def _data(self, ago):
        """
        Безопасный доступ к цене.
        Если истории не хватает, возвращаем ближайшее доступное значение.
        """
        if ago == 0:
            return self.data[0]

        if ago == -1:
            return self.data[-1] if len(self) > 1 else self.data[0]

        if ago == -2:
            if len(self) > 2:
                return self.data[-2]
            elif len(self) > 1:
                return self.data[-1]
            return self.data[0]

        # Для более глубоких обращений в корреляции
        idx_needed = abs(ago)
        if len(self) > idx_needed:
            return self.data[ago]

        # Если глубокой истории нет — возвращаем самое старое доступное
        return self.data[-(len(self) - 1)] if len(self) > 1 else self.data[0]

    def _line(self, line, ago, default=0.0):
        """
        Безопасный доступ к предыдущим значениям своей линии.
        """
        idx_needed = abs(ago)
        if ago == 0:
            return self._nz(line[0], default)

        if len(self) > idx_needed:
            return self._nz(line[ago], default)

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

        # ------------------------------------------
        # 1) High-pass filter
        # ------------------------------------------
        a1 = math.exp(-1.414 * math.pi / window)
        b1 = 2.0 * a1 * math.cos(1.414 * math.pi / window)
        c2 = b1
        c3 = -(a1 * a1)
        c1 = (1.0 + c2 - c3) / 4.0

        price0 = self._data(0)
        price1 = self._data(-1)
        price2 = self._data(-2)

        filt1 = self._line(self.l.filt, -1, 0.0)
        filt2 = self._line(self.l.filt, -2, 0.0)

        filt = (
            c1 * (price0 - 2.0 * price1 + price2) +
            c2 * filt1 +
            c3 * filt2
        )
        self.l.filt[0] = filt

        # ------------------------------------------
        # 2) Rolling autocorrelation
        # ------------------------------------------
        mincorr = 1.0
        best_lag = 1

        max_lag = min(window, max(1, len(self) - 1))

        for lag in range(1, max_lag + 1):
            sx = sy = sxx = sxy = syy = 0.0
            n = 0

            max_j = min(window - 1, len(self) - lag - 1)
            if max_j < 0:
                continue

            for j in range(max_j + 1):
                x = self._line(self.l.filt, -j, 0.0)
                y = self._line(self.l.filt, -(lag + j), 0.0)

                sx += x
                sy += y
                sxx += x * x
                sxy += x * y
                syy += y * y
                n += 1

            if n < 2:
                continue

            denx = n * sxx - sx * sx
            deny = n * syy - sy * sy

            if denx > 0.0 and deny > 0.0:
                corr = (n * sxy - sx * sy) / math.sqrt(denx * deny)
            else:
                corr = 0.0

            if corr < mincorr:
                mincorr = corr
                best_lag = lag

        self.l.mincorr[0] = mincorr

        # ------------------------------------------
        # 3) Dominant cycle
        # ------------------------------------------
        dc = 2.0 * best_lag
        prev_dc = self._line(self.l.dc, -1, dc)

        if dc > prev_dc + 2.0:
            dc = prev_dc + 2.0
        elif dc < prev_dc - 2.0:
            dc = prev_dc - 2.0

        if dc < 2.0:
            dc = 2.0

        self.l.dc[0] = dc

        # ------------------------------------------
        # 4) Tuned band-pass filter
        # ------------------------------------------
        l1 = math.cos(2.0 * math.pi / dc)
        g1 = math.cos(bandwidth * 2.0 * math.pi / dc)

        if abs(g1) < 1e-12:
            self.l.bp[0] = self._line(self.l.bp, -1, 0.0)
            return

        inner = 1.0 / (g1 * g1) - 1.0
        if inner < 0.0 and abs(inner) < 1e-12:
            inner = 0.0
        elif inner < 0.0:
            self.l.bp[0] = self._line(self.l.bp, -1, 0.0)
            return

        s1 = 1.0 / g1 - math.sqrt(inner)

        bp1 = self._line(self.l.bp, -1, 0.0)
        bp2 = self._line(self.l.bp, -2, 0.0)

        bp = (
            0.5 * (1.0 - s1) * (price0 - price2) +
            l1 * (1.0 + s1) * bp1 -
            s1 * bp2
        )
        self.l.bp[0] = bp

class AutoTuneDemoStrategy(bt.Strategy):
    params = (
        ('window', 20),
    )

    def __init__(self):
        self.atf = AutoTuneFilter(
        # self.atf = AutoTuneFilterTV(
            self.data.close,
            window=self.p.window,
        )

    def next(self):
        dt = self.data.datetime.datetime(0)
        print(
            f'{dt} | close={self.data.close[0]:.2f} | '
            f'bp={self.atf.bp[0]:.6f} | '
            f'filt={self.atf.filt[0]:.6f} | '
            f'mincorr={self.atf.mincorr[0]:.6f} | '
            f'dc={self.atf.dc[0]:.2f}'
        )


if __name__ == '__main__':
    cerebro = bt.Cerebro(stdstats=False)

    store = MoexStore(write_to_file=True, read_from_file=True)

    data = store.getdata(
        sec_id='MXM6',
        fromdate='2026-03-15',
        todate=datetime.today(),
        # tf='5m',
        tf='1h',
        name='MXM6'
    )

    cerebro.adddata(data)
    cerebro.addstrategy(AutoTuneDemoStrategy, window=20)

    results = cerebro.run()
    cerebro.plot(style='candle')