import backtrader as bt
from datetime import datetime, timedelta
from BacktraderQuikJunior.QJStore import QKStore


class MinimalStrategy(bt.Strategy):
    def __init__(self):
        # индикатор SMA (простая скользящая средняя) по ценам закрытия
        self.sma = bt.indicators.SMA(period=5)


cerebro = bt.Cerebro(stdstats=False)  # отключаем стандартных observers, чтобы чище было
store = QKStore()

dataname = 'QJSIM.SBER'
fromdate = datetime.today() - timedelta(hours=12)
data = store.getdata(dataname=dataname,
                     timeframe=bt.TimeFrame.Minutes,
                     compression=1,
                     fromdate=fromdate,
                     live_bars=False)

cerebro.adddata(data)
cerebro.addstrategy(MinimalStrategy)
cerebro.run()
cerebro.plot(
    style='line',
    volume=False,
    legend=True,
)