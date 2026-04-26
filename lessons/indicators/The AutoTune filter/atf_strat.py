from atf import AutoTuneFilter
from datetime import datetime
import backtrader as bt
from moex_store import MoexStore


class AutoTuneFilterStrategy(bt.Strategy):
    """
    Strategy based on Financial Hacker article:
    - ROC = BP - BP[2]
    - Long  when ROC crosses above 0 and MinCorr < Thresh
    - Short when ROC crosses below 0 and MinCorr < Thresh and Filt > 0
    """

    params = dict(
        write_history=None,  # Записываем или нет детальную инфу о каждой сделке
        depo=0,  # Начальный депозит
        tf=None,
        risk=None,
        start_date=None,
        end_date=None,
        window=26,
        bandwidth=0.22,
        thresh=-0.22,
        allow_short=True,
        printlog=False,
    )

    def log(self, txt):
        if self.p.printlog:
            dt = self.data.datetime.datetime(0)
            print(f'{dt} | {txt}')

    def __init__(self):
        self.atf = AutoTuneFilter(
            self.data.close,
            window=self.p.window,
            bandwidth=self.p.bandwidth
        )
        self.stop_loss_price, self.entry_price = 0.0, 0.0

        # ROC из статьи: BP - BP[2]
        self.roc = self.atf.bp - self.atf.bp(-2)

        # Сигналы пересечения нуля
        self.cross_up = bt.indicators.CrossUp(self.roc, 0.0)
        self.cross_down = bt.indicators.CrossDown(self.roc, 0.0)

        self.order = None

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status == order.Completed:
            if order.isbuy():
                self.log(f'BUY EXECUTED at {order.executed.price:.2f}')
            else:
                self.log(f'SELL EXECUTED at {order.executed.price:.2f}')

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log(f'ORDER FAILED: {order.getstatusname()}')

        self.order = None

    def next(self):
        if self.order:
            return

        roc_now = self.roc[0]
        mincorr_now = self.atf.mincorr[0]
        filt_now = self.atf.filt[0]
        dc_now = self.atf.dc[0]
        bp_now = self.atf.bp[0]

        long_signal = bool(self.cross_up[0] and mincorr_now < self.p.thresh)
        short_signal = bool(
            self.cross_down[0]
            and mincorr_now < self.p.thresh
            and filt_now > 0
        )

        self.log(
            f'close={self.data.close[0]:.2f} | '
            f'bp={bp_now:.6f} | roc={roc_now:.6f} | '
            f'mincorr={mincorr_now:.6f} | filt={filt_now:.6f} | dc={dc_now:.2f}'
        )

        # Нет позиции
        if not self.position:
            if long_signal:
                self.log('LONG SIGNAL -> buy()')
                self.entry_price = self.data.close[0]
                self.order = self.buy()

            elif self.p.allow_short and short_signal:
                self.log('SHORT SIGNAL -> sell()')
                self.entry_price = self.data.close[0]
                self.order = self.sell()

            return

        # Уже long
        if self.position.size > 0:
            if short_signal:
                if self.p.allow_short:
                    # self.log('REVERSE LONG -> SHORT')
                    self.log('EXIT')
                    self.close()
                    # self.order = self.sell()
                else:
                    self.log('EXIT LONG')
                    # self.order = self.close()
            return

        # Уже short
        if self.position.size < 0 and long_signal:
            # self.log('REVERSE SHORT -> LONG')
            self.log('EXIT')
            self.close()
            # self.order = self.buy()


if __name__ == '__main__':
    cerebro = bt.Cerebro(stdstats=True)
    cerebro.broker.setcash(1000000)

    store = MoexStore(write_to_file=True, read_from_file=True)

    data = store.getdata(
        sec_id='MXM6',
        fromdate='2026-03-15',
        todate=datetime.today(),
        tf='1h',
        name='MXM6'
    )

    cerebro.adddata(data)

    # Можно поставить size через sizer
    cerebro.addsizer(bt.sizers.FixedSize, stake=1)

    cerebro.addstrategy(
        AutoTuneFilterStrategy,
        window=26,
        bandwidth=0.22,
        thresh=-0.42,
        allow_short=True,
        printlog=True
    )

    results = cerebro.run()
    cerebro.plot(style='candle')