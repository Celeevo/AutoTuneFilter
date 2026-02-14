from datetime import datetime, timedelta
import backtrader as bt
from BacktraderQuikJunior.QJStore import QKStore

# Создаем Стратегию
class TestStrategy(bt.Strategy):
    def __init__(self):
        print(f"STRATEGY.__init__ | Start CASH = {self.broker.getcash()}")

    def next(self):
        # Просто выводим цену закрытия каждого дня
        print(f"Bar Start DateTime {self.data.datetime.datetime(0).isoformat()}, "
              f"Real World DateTime {datetime.now().strftime('%H:%M:%S.%f')[:-3]}, "
              f"Close: {self.data.close[0]}, "
              f"Previous Close {self.data.close[-1]}")

def main():
    # Создаем экземпляры cerebro и хранилища
    cerebro = bt.Cerebro(stdstats=False, quicknotify=True)
    # cerebro = bt.Cerebro(stdstats=False)
    store = QKStore()

    dataname = 'QJSIM.SBER'


    broker = store.getbroker()  # экземпляр брокера берем из хранилища
    cerebro.setbroker(broker)  # привязываем его к cerebro
    # Проверяем запрошенный источник данных на его наличие в QUIK Junior
    broker.check_data_names(dataname)

    fromdate = datetime.today().date()  # с какой даты берем данные
    fromdate = datetime.today() - timedelta(minutes=1)
    # fromdate = datetime.now()  # с какой даты берем данные
    # Будем работать на тайм-фрейме 1 минута
    data = store.getdata(dataname=dataname, timeframe=bt.TimeFrame.Minutes,
                         compression=1, fromdate=fromdate, live_bars=True)

    # Добавляем в cerebro источник данных, сайзер, стратегию и
    # запускаем движок
    cerebro.adddata(data)
    cerebro.addstrategy(TestStrategy)
    # cerebro.addstrategy(TraceStrat)
    cerebro.run()

if __name__ == '__main__':
    main()