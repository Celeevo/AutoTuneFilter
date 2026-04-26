from atf import AutoTuneFilter
from datetime import datetime
import backtrader as bt
from moex_store import MoexStore
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING


FUTURE_TYPE = dict(     # Базовая ставка комиссии Биржи
    currency=0.00462,   # Валютные контракты
    percent=0.01650,    # Процентные контракты
    stock=0.01980,      # Фондовые контракты
    xindex=0.00660,     # Индексные контракты
    commodity=0.01320   # Товарные контракты
)

class FuturesCommission(bt.CommInfoBase):
    params = dict(moexcomm=0.0, cost_of_price_step=0)  # Базовая ставка комиссии Биржи

    def _getcommission(self, size, price, pseudoexec):
        brokers_pocket = abs(size) * self.p.commission
        moexs_pocket = abs(size) * price * self.p.mult * self.p.moexcomm / 100
        return brokers_pocket + moexs_pocket


# Класс для расчета комиссии при работе с Акциями
class StockCommission(bt.CommInfoBase):
    params = dict(
        moexcomm=0,  # Комиссии Биржи в % от покупки/продажи (0.03%)
        brokercomm=0  # Комиссии Брокера в % от покупки/продажи (0.03%)
    )

    def _getcommission(self, size, price, pseudoexec):
        return abs(size) * price * (self.p.moexcomm + self.p.brokercomm)


futures_comm = dict( # Комиссии для фьючерсов
    RTS=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=29000, # 27/04/25, 24700,  # ГО 05.12.2024
                          mult=16.53098/10,  # мультипликатор Стоимость шага цены/Шаг цены
                          moexcomm=FUTURE_TYPE['xindex']),
    RTSM=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=2900,  # 27/04/25, 2470,  # ГО 05.12.2024
                          mult=8.26549/0.5,  # мультипликатор Стоимость шага цены/Шаг цены
                          moexcomm=FUTURE_TYPE['xindex']),
    NASD=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=2607,  # ГО 05.12.2024
                          mult=0.97966/1,  # мультипликатор Стоимость шага цены/Шаг цены
                          moexcomm=FUTURE_TYPE['xindex']),
    CNY=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=1050,  # ГО 27/04/25
                          mult=1/0.001,  # мультипликатор
                          moexcomm=FUTURE_TYPE['currency']),
    Si=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=15758,  # ГО  05.12.2024
                          mult=1,  # мультипликатор
                          moexcomm=FUTURE_TYPE['currency']),
    Eu=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=16000,  # ГО
                          mult=1,  # мультипликатор
                          moexcomm=FUTURE_TYPE['currency']),
    NG=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=6300,  # ГО
                          mult=9.8/0.001,  # мультипликатор
                          moexcomm=FUTURE_TYPE['commodity']),
    GOLD=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=16600,  # ГО
                          mult=9.8/0.1,  # мультипликатор
                          moexcomm=FUTURE_TYPE['commodity']),
    SBRF=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=5200,  # ГО
                          mult=1,  # мультипликатор
                          moexcomm=FUTURE_TYPE['stock']),
    BR=FuturesCommission(commission=2.0,  # 2 руб за контракт
                         margin=10374,  # ГО 30-12-24
                         mult=10.167/ 0.01,  # мультипликатор
                         moexcomm=FUTURE_TYPE['commodity']),
    MIX=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=33000,  # - ГО 26-04-26
                          mult=25 / 25,  # мультипликатор Стоимость шага цены/Шаг цены
                          moexcomm=FUTURE_TYPE['xindex'],
                          cost_of_price_step=25),
    MXI=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=3500,  # - ГО 26-04-25, 3400 - ГО 07.02.2025
                          mult=0.5 / 0.05,  # мультипликатор Стоимость шага цены/Шаг цены
                          moexcomm=FUTURE_TYPE['xindex'], cost_of_price_step=0.05),
    SPYF=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=5600,  # ГО 27-04-25
                          mult=0.82655 / 0.01,  # мультипликатор Стоимость шага цены/Шаг цены
                          moexcomm=FUTURE_TYPE['xindex'], cost_of_price_step=0.05))

def round_to_nearest_price_step(step, value, isbuy):
    """
    Универсальное округление до ближайшего кратного шага цены.
    Работает с любыми положительными step, в т.ч. < 1.

    :param step: Шаг цены инструмента (>0)
    :param value: Значение, которое нужно округлить
    :param isbuy: True  -> округлить вниз  (покупка, long)
                   False -> округлить вверх (продажа, short)
    :return: Округлённое значение (float)
    """
    if step <= 0:
        raise ValueError('step должен быть > 0')

    # Переводим во "внутреннюю" точку Decimal, чтобы избежать погрешностей float
    step_d  = Decimal(str(step))
    value_d = Decimal(str(value))

    # Сколько шагов содержится в price?
    steps_cnt = value_d / step_d

    # Округляем количество шагов
    rounding_mode = ROUND_FLOOR if isbuy else ROUND_CEILING
    steps_cnt = steps_cnt.to_integral_value(rounding=rounding_mode)

    # Возвращаемся к цене и приводим к float
    return float(steps_cnt * step_d)


class AllInSizer(bt.Sizer):
    def _getsizing(self, comminfo, cash, data, isbuy):
        if comminfo.p.margin:  # работаем с фьючерсами?
            max_size = cash / comminfo.p.margin  # Кэш / ГО
        else:
            max_size = cash / self.strategy.entry_price  # Кэш / вход

        size = int(max_size) - 1
        if size <= 0:
            return 0
        direction = 2 * isbuy - 1  # 1 при входе в лонг, -1 - в шорт
        stop_loss_price = self.strategy.entry_price - direction * cash * (self.strategy.p.risk / 100) / (size * comminfo.p.mult)
        if comminfo.p.cost_of_price_step != 0:
            self.strategy.stop_loss_price = round_to_nearest_price_step(comminfo.p.cost_of_price_step, stop_loss_price, isbuy)
        else:
            self.strategy.stop_loss_price = stop_loss_price
        # print(f'SIZER: {data.p.name = }, {cash = }, {comminfo.p.margin = }, {comminfo.p.mult = }, {size = }, {self.strategy.entry_price = }, {self.strategy.stop_loss_price = }, {isbuy = }')
        return size


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
        tp_mult=2.0,   # тейк-профит в R
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

        # self.ema = bt.indicators.EMA(self.data, period=40)
        # self.rsi = bt.indicators.RSI(self.data, period=14)

        self.stop_loss_price, self.entry_price = 0.0, 0.0
        self.take_profit_price = 0.0

        # ROC из статьи: BP - BP[2]
        self.roc = self.atf.bp - self.atf.bp(-2)

        # Сигналы пересечения нуля
        self.cross_up = bt.indicators.CrossUp(self.roc, 0.0)
        self.cross_down = bt.indicators.CrossDown(self.roc, 0.0)

        self.order = None
        self.stop_order = None
        self.take_profit_order = None

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status == order.Completed:
            if order.info.name in ('long', 'short'):
                exit_size = abs(order.executed.size)
                executed_entry = order.executed.price
                risk_points = abs(executed_entry - self.stop_loss_price)

                comminfo = self.broker.getcommissioninfo(self.data)

                if order.isbuy():
                    self.log(f'BUY EXECUTED at {executed_entry:.2f}')

                    raw_take_profit = executed_entry + self.p.tp_mult * risk_points
                    if comminfo.p.cost_of_price_step != 0:
                        # тейк для long округляем К ВХОДУ, т.е. вниз
                        self.take_profit_price = round_to_nearest_price_step(
                            comminfo.p.cost_of_price_step,
                            raw_take_profit,
                            True
                        )
                    else:
                        self.take_profit_price = raw_take_profit

                    self.stop_order = self.sell(
                        exectype=bt.Order.Stop,
                        size=exit_size,
                        price=self.stop_loss_price,
                        name='stop_loss'
                    )
                    self.take_profit_order = self.sell(
                        exectype=bt.Order.Limit,
                        size=exit_size,
                        price=self.take_profit_price,
                        name='take_profit',
                        oco=self.stop_order
                    )

                else:
                    self.log(f'SELL EXECUTED at {executed_entry:.2f}')

                    raw_take_profit = executed_entry - self.p.tp_mult * risk_points
                    if comminfo.p.cost_of_price_step != 0:
                        # тейк для short округляем К ВХОДУ, т.е. вверх
                        self.take_profit_price = round_to_nearest_price_step(
                            comminfo.p.cost_of_price_step,
                            raw_take_profit,
                            False
                        )
                    else:
                        self.take_profit_price = raw_take_profit

                    self.stop_order = self.buy(
                        exectype=bt.Order.Stop,
                        size=exit_size,
                        price=self.stop_loss_price,
                        name='stop_loss'
                    )
                    self.take_profit_order = self.buy(
                        exectype=bt.Order.Limit,
                        size=exit_size,
                        price=self.take_profit_price,
                        name='take_profit',
                        oco=self.stop_order
                    )

                self.log(
                    f'STOP={self.stop_loss_price:.2f} | '
                    f'TP(2R)={self.take_profit_price:.2f}'
                )

            # ---------- исполнился stop-loss ----------
            elif order.info.name == 'stop_loss':
                self.log(f'STOP LOSS EXECUTED at {order.executed.price:.2f}')
                self.stop_order = None
                self.take_profit_order = None
                self.stop_loss_price = 0.0
                self.take_profit_price = 0.0
                self.entry_price = 0.0

            # ---------- исполнился take-profit ----------
            elif order.info.name == 'take_profit':
                self.log(f'TAKE PROFIT EXECUTED at {order.executed.price:.2f}')
                self.take_profit_order = None
                self.stop_order = None
                self.stop_loss_price = 0.0
                self.take_profit_price = 0.0
                self.entry_price = 0.0

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log(f'ORDER FAILED: {order.getstatusname()}')
            if order == self.stop_order:
                self.stop_order = None
            elif order == self.take_profit_order:
                self.take_profit_order = None

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
                self.order = self.buy(name='long')

            elif self.p.allow_short and short_signal:
                self.log('SHORT SIGNAL -> sell()')
                self.entry_price = self.data.close[0]
                self.order = self.sell(name='short')

            return

        # Если позиция уже есть, новых действий не делаем.
        # Выход только по stop-loss или take-profit.
        if self.position:
            return

        # Если выходные ордера ещё висят, тоже ничего не делаем
        if self.stop_order or self.take_profit_order:
            return


if __name__ == '__main__':
    params = dict(
        write_history=True,
        depo=300000.0,  # Начальный депозит
        risk=5,
        window=50,  #range(16,57),  #30,
        bandwidth=0.34, #[0.08, 0.16, 0.24, 0.32, 0.4], # [i / 100 for i in range(1, 31)]
        thresh=-0.48,  #[-i / 12.5 for i in range(3, 8)], # 0.22, #
        allow_short=True,
        printlog=True,
        tp_mult=1.5,   # тейк-профит в R
    )
    cerebro = bt.Cerebro(stdstats=True)
    cerebro.broker.setcash(params['depo'])
    store = MoexStore(write_to_file=True, read_from_file=True)

    data = store.getdata(
        sec_id='MXM6',
        fromdate='2026-3-14',
        todate='2026-4-26',
        tf='1h',
        name='MXM6'
    )

    # 26-04-26 50-0.32--0.48-1.5-MIX

    cerebro.adddata(data)
    # cerebro.addsizer(bt.sizers.FixedSize, stake=1)
    cerebro.addsizer(AllInSizer)
    cerebro.broker.addcommissioninfo(futures_comm['MIX'], name=data.p.name)
    cerebro.addstrategy(AutoTuneFilterStrategy, **params)
    results = cerebro.run()
    cerebro.plot(style='candle')