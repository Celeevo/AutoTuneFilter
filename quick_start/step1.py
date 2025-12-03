import backtrader as bt
import datetime

# Получаем исторические данные из csv файла.
# Простейшая стратегия: только покупка, если цена растет 2 свечи подряд.
# Установка стартового капитала = 200_000 руб.


# Создаем Стратегию
class TestStrategy(bt.Strategy):

    def log(self, txt, dt=None):
        # Функция логирования событий Стратегии
        dt = dt or self.data.datetime.date(0)
        print(f'{dt.isoformat()}, {txt}')

    def next(self):
        # Просто выводим цену закрытия каждого дня
        self.log(f'Close: {self.data.close[0]}')
        # текущее значение close меньше предыдущего close
        if self.data.close[0] < self.data.close[-1]:
            # предыдущее close меньше пред-предыдущего close
            if self.data.close[-1] < self.data.close[-2]:
                self.log(f'Покупаем! {self.data.close[0]}')
                self.buy()

if __name__ == '__main__':
    cerebro = bt.Cerebro()
    # установим свой стартовый капитал
    cerebro.broker.setcash(200000.0)

    data = bt.feeds.GenericCSVData(
        # путь и имя файла с историческими котировками
        dataname='SBER_010123_311224.csv',
        # не берем данные раньше этой даты:
        fromdate=datetime.datetime(2023, 1, 1),
        # не берем данные позже этой даты:
        todate=datetime.datetime(2024, 12, 31),
        # укажем формат даты (по умолчанию %Y-%m-%d)
        dtformat='%d/%m/%y',
        # укажем формат времени (по умолчанию %H:%M:%S)
        tmformat='%H:%M',
        # укажем, какие данные в каких колонках файла
        datetime=0,
        time=1,
        open=2,
        high=3,
        low=4,
        close=5,
        volume=6,
        # -1 означает отсутствие данных в файле
        openinterest=-1
    )

    # добавляем источник данных в движок
    cerebro.adddata(data)
    cerebro.addstrategy(TestStrategy)

    print(f'Стартовый капитал: {cerebro.broker.getvalue()}')
    cerebro.run()
    cerebro.plot()
    print(f'Финальный капитал: {cerebro.broker.getvalue()}')