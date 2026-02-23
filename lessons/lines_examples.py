import backtrader as bt
from datetime import datetime, timedelta
from BacktraderQuikJunior.QJStore import QKStore

class NoTradeStrategy(bt.Strategy):
    def next(self):
        pass  # Ничего не делаем — просто показываем data lines


cerebro = bt.Cerebro(stdstats=False)  # отключаем стандартных observers, чтобы чище было
store = QKStore()
broker = store.getbroker()  # экземпляр брокера берем из хранилища

dataname = 'QJSIM.SBER'
fromdate = datetime.today() - timedelta(minutes=1)
data = store.getdata(dataname=dataname,
                     timeframe=bt.TimeFrame.Minutes,
                     compression=1,
                     fromdate=fromdate,
                     live_bars=True)
cerebro.adddata(data)

cerebro.addstrategy(NoTradeStrategy)

cerebro.run()
cerebro.plot(style='candlestick', volume=True, iplot=False)