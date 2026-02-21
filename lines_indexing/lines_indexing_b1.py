from datetime import datetime, timedelta
import backtrader as bt
from BacktraderQuikJunior.QJStore import QKStore

# Стратегия - выводим на каждом Баре сообщение о его приходе
class TestStrategy(bt.Strategy):
    def __init__(self):
        print(f"STRATEGY.__init__ - Ожидаем получение нового БАРА из QUIK!")

    def next(self):
        # Просто выводим цену закрытия каждого дня
        print(f"{50*'*'}\n"
              f"ПОЛУЧЕН НОВЫЙ БАР (Time: {datetime.now().strftime('%H:%M:%S')})!\n"
              f"Data Feed LEN: {len(self.data)}\n"
              f"Bar Start at {bt.num2date(self.data.datetime[0])}\n"
              # f"Bar Start Time {bt.num2date(self.data.datetime[0]).time()}\n"
              # f"Bar Start Time {self.data.datetime.datetime(0).time().isoformat()}\n"
              f"Close[0]: {self.data.close[0]}")
        if len(self.data) > 1:
            for i in range(1, len(self.data)):
                print(f"Close[-{i}]: {self.data.close[-i]}")

def main():
    # Создаем экземпляры cerebro и хранилища
    cerebro = bt.Cerebro(stdstats=False, quicknotify=True)
    store = QKStore()

    dataname = 'QJSIM.SBER'

    broker = store.getbroker()  # экземпляр брокера берем из хранилища
    cerebro.setbroker(broker)  # привязываем его к cerebro
    # Проверяем запрошенный источник данных на его наличие в QUIK Junior
    # broker.check_data_names(dataname)

    fromdate = datetime.today() - timedelta(minutes=2)
    print(datetime.today(), fromdate)
    # Будем работать на тайм-фрейме 1 минута
    data = store.getdata(dataname=dataname, timeframe=bt.TimeFrame.Minutes,
                         compression=1,
                         fromdate=fromdate,
                         live_bars=True)

    cerebro.adddata(data)
    cerebro.addstrategy(TestStrategy)
    cerebro.run()

if __name__ == '__main__':
    main()