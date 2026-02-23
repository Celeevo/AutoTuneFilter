from datetime import datetime, timedelta
import backtrader as bt
from BacktraderQuikJunior.QJStore import QKStore

# Стратегия - выводим на каждом Баре сообщение о его приходе
class TestStrategy(bt.Strategy):
    def __init__(self):
        print(f"STRATEGY.__init__ - {datetime.now().strftime('%H:%M:%S')}. "
              f"Ожидаем получение нового БАРА из QUIK!")

    def next(self):
        print(f"\n{50*'*'}\n"
              f"ПОЛУЧЕН НОВЫЙ БАР! "
              f"(Time: {datetime.now().strftime('%H:%M:%S')}, "
              f"Data Feed LEN: {len(self.data)})\n"
              f"Bar Start Time: {bt.num2time(self.data.datetime[0])}, "
              f"Close[0]: {self.data.close[0]}")
        if len(self.data) > 1:
            print(f"ПРЕДЫДУЩИЕ БАРЫ:")
            for i in range(1, len(self.data)):
                print(f"Bar Start Time: {bt.num2time(self.data.datetime[-i])}, "
                      f"Close[-{i}]: {self.data.close[-i]}")

def main():
    # Создаем экземпляры cerebro и хранилища store
    cerebro = bt.Cerebro()
    store = QKStore()

    # Создаем Источник данных и добавляем его в cerebro
    dataname = 'QJSIM.SBER'
    fromdate = datetime.today() - timedelta(minutes=1)
    data = store.getdata(dataname=dataname,
                         timeframe=bt.TimeFrame.Minutes,
                         compression=1,
                         fromdate=fromdate,
                         live_bars=True)
    cerebro.adddata(data)

    cerebro.addstrategy(TestStrategy)
    cerebro.run()

if __name__ == '__main__':
    main()