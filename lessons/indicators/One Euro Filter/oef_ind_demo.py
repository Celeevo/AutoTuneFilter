from datetime import datetime
from moex_store import MoexStore
import math
import backtrader as bt


class OneEuroFilter(bt.Indicator):
    """
    One Euro Filter по версии John F. Ehlers.

    Формула из статьи:

    AlphaDX = 2*pi / (4*pi + PeriodDX)

    SmoothedDX = AlphaDX * (Price - Price[1])
                 + (1 - AlphaDX) * SmoothedDX[1]

    Cutoff = PeriodMin + Beta * AbsValue(SmoothedDX)

    Alpha3 = 2*pi / (4*pi + Cutoff)

    Smoothed = Alpha3 * Price
               + (1 - Alpha3) * Smoothed[1]
    """

    lines = (
        'filtered',
        'smoothed_dx',
        'cutoff',
        'alpha',
    )

    params = (
        ('period_min', 10),
        ('beta', 0.2),
        ('period_dx', 10),
    )

    plotinfo = dict(subplot=False)

    plotlines = dict(
        filtered=dict(_name='OneEuroFilter'),
        smoothed_dx=dict(_plotskip=True),
        cutoff=dict(_plotskip=True),
        alpha=dict(_plotskip=True),
    )

    def __init__(self):
        self.alpha_dx = 2.0 * math.pi / (4.0 * math.pi + self.p.period_dx)

    @staticmethod
    def alpha_from_period(period):
        return 2.0 * math.pi / (4.0 * math.pi + period)

    def next(self):
        price = self.data[0]

        # Аналог:
        # If CurrentBar = 1 Then Begin
        #     SmoothedDX = 0;
        #     Smoothed = Price;
        # End;
        if len(self) == 1:
            self.lines.smoothed_dx[0] = 0.0
            self.lines.cutoff[0] = self.p.period_min
            self.lines.alpha[0] = self.alpha_from_period(self.p.period_min)
            self.lines.filtered[0] = price
            return

        dx = price - self.data[-1]

        smoothed_dx = (
            self.alpha_dx * dx
            + (1.0 - self.alpha_dx) * self.lines.smoothed_dx[-1]
        )

        cutoff = self.p.period_min + self.p.beta * abs(smoothed_dx)

        alpha = self.alpha_from_period(cutoff)

        filtered = (
            alpha * price
            + (1.0 - alpha) * self.lines.filtered[-1]
        )

        self.lines.smoothed_dx[0] = smoothed_dx
        self.lines.cutoff[0] = cutoff
        self.lines.alpha[0] = alpha
        self.lines.filtered[0] = filtered


class OneEuroDemoStrategy(bt.Strategy):
    params = (
        ('period_min', 10),
        ('beta', 0.2),
        ('period_dx', 10),
        ('print_extra', False),
    )

    def __init__(self):
        self.one_euro = OneEuroFilter(
            self.data.close,
            period_min=self.p.period_min,
            beta=self.p.beta,
            period_dx=self.p.period_dx,
        )

    def next(self):
        dt = self.data.datetime.datetime(0)

        if self.p.print_extra:
            print(
                f'{dt} | '
                f'close={self.data.close[0]:.6f} | '
                f'one_euro={self.one_euro.filtered[0]:.6f} | '
                f'smoothed_dx={self.one_euro.smoothed_dx[0]:.6f} | '
                f'cutoff={self.one_euro.cutoff[0]:.6f} | '
                f'alpha={self.one_euro.alpha[0]:.6f}'
            )
        else:
            print(
                f'{dt} | '
                f'close={self.data.close[0]:.6f} | '
                f'one_euro={self.one_euro.filtered[0]:.6f}'
            )

if __name__ == '__main__':
    cerebro = bt.Cerebro()

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

    cerebro.addstrategy(
        OneEuroDemoStrategy,
        period_min=10,
        beta=0.2,
        period_dx=10,
        print_extra=False,
    )


    results = cerebro.run(runonce=False, preload=True)
    cerebro.plot(style='candle')