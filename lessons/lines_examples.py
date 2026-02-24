import backtrader as bt
from datetime import datetime, timedelta
from BacktraderQuikJunior.QJStore import QKStore


class DemoStrategy(bt.Strategy):
    def __init__(self):
        # индикатор SMA (простая скользящая средняя) по ценам закрытия
        self.sma = bt.indicators.SMA(period=10)
        print(f"Time\t Close\t SMA")

    def next(self):
        # print(f"{bt.num2time(self.data.datetime[0])} "
        #       f"{self.data.close[0]} "
        #       f"{self.sma[0]}\t")
        pass

    def stop(self):
        for i in range(len(self.data)):
            print(f"{bt.num2time(self.data.datetime[-i])} "
                  f"{self.data.close[-i]} "
                  f"{self.sma[-i]}\t")


cerebro = bt.Cerebro(stdstats=False)  # отключаем стандартных observers, чтобы чище было
store = QKStore()

dataname = 'QJSIM.SBER'
fromdate = datetime.now() - timedelta(hours=1)
data = store.getdata(dataname=dataname,
                     timeframe=bt.TimeFrame.Minutes,
                     compression=1,
                     fromdate=fromdate,
                     live_bars=False)

cerebro.adddata(data)
cerebro.addstrategy(DemoStrategy)
cerebro.run()
cerebro.plot(
    # style='bar',
    style='line',
    volume=False,
    legend=True,
)