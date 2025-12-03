import backtrader as bt
import datetime

# Получаем исторические данные из csv файла.
# Стратегия:
#       только покупка, если цена растет 2 свечи подряд
#       продажа на 5-м баре после покупки.
# Установка стартового капитала = 200_000 руб.
# Учитываем комиссию 0,1%.
# Размер сделки = 10 акций.

# Создаем Стратегию
class TestStrategy(bt.Strategy):
    params = (('exitbars', 5),)

    def log(self, txt, dt=None):
        # Функция логирования событий Стратегии
        dt = dt or self.data.datetime.date(0)
        print(f'{dt.isoformat()}, {txt}')

    def __init__(self):
        # Для отслеживания размещенных Ордеров
        self.order = None

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            # Ордер отправлен Брокеру или подтвержден Брокером - ничего не делаем
            return

        # Проверяем, был ли Ордер исполнен
        # Брокер может отвергнуть Ордер, если для его исполнения недостаточно денег
        if order.status in [order.Completed]:
            side = 'ПОКУПКА' if order.isbuy() else 'ПРОДАЖА'

            self.log(f'{side} ИСПОЛНЕНА, цена: {order.executed.price}, '
                     f'комиссия {order.executed.comm:.5f}')

            # Запоминаем номер бара, на котором произошла Покупка
            self.bar_executed = len(self)

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log('Ордер отвергнут Брокером')

        # Нет отслеживаемых Ордеров
        self.order = None

    def notify_trade(self, trade):
        # Если сделка еще не закрыта - выходим
        if not trade.isclosed:
            return

        self.log(f'Операционная ПРИБЫЛЬ, до начисления комиссии: '
                 f'{trade.pnl:.2f}, после комиссии: {trade.pnlcomm:.2f}')

    def next(self):
        # Просто выводим цену закрытия каждого дня
        self.log(f'Close: {self.data.close[0]}')

        # Проверяем, есть ли размещенные Ордера, если есть -
        # мы не можем разместить еще
        if self.order:
            return

        # Проверяем, есть ли у нас позиция на рынке
        if not self.position:

            # Еще нет, тогда если
            # текущее значение close меньше предыдущего close
            if self.data.close[0] < self.data.close[-1]:
                # предыдущее close меньше пред-предыдущего close
                if self.data.close[-1] < self.data.close[-2]:
                    self.log(f'Покупаем! {self.data.close[0]}')
                    # Будем отслеживать созданный Ордер,
                    # чтобы не создавать еще
                    self.order = self.buy()
        else:

            # Уже в позиции, можем продавать, если
            if len(self) >= (self.bar_executed + self.p.exitbars):
                self.log(f'Продаем! {self.data.close[0]}')

                # Будем отслеживать созданный Ордер,
                # чтобы не создавать еще
                self.order = self.sell()

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
        datetime=0, time=1, open=2, high=3, low=4, close=5, volume=6,
        # -1 означает отсутствие данных в файле
        openinterest=-1
    )

    # добавляем источник данных в движок
    cerebro.adddata(data)
    cerebro.addstrategy(TestStrategy, exitbars=5)
    # разделите 0,1% на 100, чтобы убрать %
    cerebro.broker.setcommission(commission=0.001)
    cerebro.addsizer(bt.sizers.FixedSize, stake=10)

    print(f'Стартовый капитал: {cerebro.broker.getvalue()}')
    cerebro.run()
    cerebro.plot()
    print(f'Финальный капитал: {cerebro.broker.getvalue():.2f}')