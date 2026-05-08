from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import time as _time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

import backtrader as bt
import pandas as pd

# from atf import AutoTuneFilter
# from atf_new import AutoTuneFilter
from atf_new_from_Antropic import AutoTuneFilter
from moex_store import MoexStore
from cerebroview import plot

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
                          margin=1050,  # ГО 27/04/25 27/04/26(!)
                          mult=1/0.001,  # мультипликатор
                          moexcomm=FUTURE_TYPE['currency'],
                          cost_of_price_step=0.001),
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


class AutoTuneFilterStrategy(bt.Strategy):
    """
    Strategy based on Financial Hacker article:
    - ROC = BP - BP[2]
    - Long  when ROC crosses above 0 and MinCorr < Thresh
    - Short when ROC crosses below 0 and MinCorr < Thresh and Filt > 0
    """

    params = dict(
        write_history=None,  # Записываем или нет детальную инфу о каждой сделке
        risk=None,
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

        self.stop_loss_price = 0.0
        self.entry_price = 0.0
        self.take_profit_price = 0.0

        self.roc = self.atf.bp - self.atf.bp(-2)
        self.cross_up = bt.indicators.CrossUp(self.roc, 0.0)
        self.cross_down = bt.indicators.CrossDown(self.roc, 0.0)

        self.long_signal = bt.And(
            self.cross_up,
            self.atf.mincorr < self.p.thresh
        )

        self.short_signal = bt.And(
            self.cross_down,
            self.atf.mincorr < self.p.thresh,
            # self.atf.filt > 0
        )

        self.order = None
        self.stop_order = None
        self.take_profit_order = None

    def _reset_bracket_state(self):
        self.order = None
        self.stop_order = None
        self.take_profit_order = None
        self.stop_loss_price = 0.0
        self.take_profit_price = 0.0
        self.entry_price = 0.0

    def _has_active_orders(self):
        return any(
            order is not None and order.alive()
            for order in (self.order, self.stop_order, self.take_profit_order)
        )

    def _round_exit_price(self, comminfo, price, isbuy):
        if comminfo.p.cost_of_price_step != 0:
            return round_to_nearest_price_step(comminfo.p.cost_of_price_step, price, isbuy)
        return price

    def _calc_bracket_params(self, isbuy):
        comminfo = self.broker.getcommissioninfo(self.data)
        cash = self.broker.getcash()
        self.entry_price = self.data.close[0]

        if self.entry_price <= 0:
            return None

        if comminfo.p.margin:
            max_size = cash / comminfo.p.margin
        else:
            max_size = cash / self.entry_price

        size = int(max_size) - 1
        if size <= 0:
            return None

        direction = 1 if isbuy else -1
        raw_stop_loss = (
            self.entry_price
            - direction * cash * (self.p.risk / 100) / (size * comminfo.p.mult)
        )
        self.stop_loss_price = self._round_exit_price(comminfo, raw_stop_loss, isbuy)

        risk_points = abs(self.entry_price - self.stop_loss_price)
        if risk_points <= 0:
            return None

        raw_take_profit = self.entry_price + direction * self.p.tp_mult * risk_points
        self.take_profit_price = self._round_exit_price(comminfo, raw_take_profit, isbuy)

        return size, self.stop_loss_price, self.take_profit_price

    def _submit_bracket(self, isbuy):
        bracket_params = self._calc_bracket_params(isbuy)
        if bracket_params is None:
            return

        size, stop_price, limit_price = bracket_params
        side = 'long' if isbuy else 'short'
        bracket_name = 'buy_bracket' if isbuy else 'sell_bracket'
        self.log(f'{side.upper()} SIGNAL -> {bracket_name}()')

        bracket_method = self.buy_bracket if isbuy else self.sell_bracket
        self.order, self.stop_order, self.take_profit_order = bracket_method(
            size=size,
            exectype=bt.Order.Market,
            stopprice=stop_price,
            stopexec=bt.Order.Stop,
            limitprice=limit_price,
            limitexec=bt.Order.Limit,
            oargs={'name': side},
            # BackBroker pre-checks bracket children as sequential independent
            # orders. Disable child checks to avoid false Margin on OCO exits.
            stopargs={'name': 'stop_loss', '_checksubmit': False},
            limitargs={'name': 'take_profit', '_checksubmit': False},
            # stopargs={'name': 'stop_loss'},
            # limitargs={'name': 'take_profit'},
        )

        self.log(
            f'STOP={self.stop_loss_price:.2f} | '
            f'TP({self.p.tp_mult}R)={self.take_profit_price:.2f}'
        )

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        order_name = getattr(order.info, 'name', None)

        if order.status == order.Completed:
            if order_name == 'long':
                self.log(f'BUY EXECUTED at {order.executed.price:.2f}')
                self.order = None
            elif order_name == 'short':
                self.log(f'SELL EXECUTED at {order.executed.price:.2f}')
                self.order = None
            elif order_name == 'stop_loss':
                self.log(f'STOP LOSS EXECUTED at {order.executed.price:.2f}')
                self._reset_bracket_state()
            elif order_name == 'take_profit':
                self.log(f'TAKE PROFIT EXECUTED at {order.executed.price:.2f}')
                self._reset_bracket_state()

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log(f'ORDER {order_name} FAILED: {order.getstatusname()}')

            if order_name in ('long', 'short'):
                self._reset_bracket_state()
            elif order_name == 'stop_loss':
                self.stop_order = None
            elif order_name == 'take_profit':
                self.take_profit_order = None

    def next(self):
        if self.position or self._has_active_orders():
            return

        if self.long_signal[0]:
            self._submit_bracket(isbuy=True)
        elif self.p.allow_short and self.short_signal[0]:
            self._submit_bracket(isbuy=False)

def ensure_single_params(params):
    iterable_params = [
        name for name, value in params.items()
        if isinstance(value, (list, tuple, set, range))
    ]
    if iterable_params:
        raise ValueError(
            'Для одиночного запуска замените оптимизационные параметры на одиночные значения: '
            + ', '.join(iterable_params)
        )


def print_trade_analysis(analysis):
    total = getattr(getattr(analysis, 'total', None), 'closed', 0) or 0
    won = getattr(getattr(analysis, 'won', None), 'total', 0) or 0
    lost = getattr(getattr(analysis, 'lost', None), 'total', 0) or 0
    pnl = getattr(getattr(analysis, 'pnl', None), 'net', None)
    pnl_total = getattr(pnl, 'total', 0) or 0
    pnl_average = getattr(pnl, 'average', 0) or 0

    print(f'Сделок закрыто: {total}')
    print(f'Прибыльных: {won}, убыточных: {lost}')
    print(f'PNL net: {pnl_total:.2f}, средний PNL: {pnl_average:.2f}')


def make_contract_data(store, sec, contract, contracts, start_date, end_date, tf):
    prevexpdate = pd.to_datetime(store.futures.prevexpdate(contract))

    if contract == contracts[0]:
        fromdate = pd.to_datetime(start_date) - timedelta(days=5)
    else:
        fromdate = prevexpdate - timedelta(days=5)

    if contract == contracts[-1]:
        todate = end_date
    else:
        todate = store.futures.expdate(contract)

    data = store.getdata(
        sec_id=contract,
        fromdate=fromdate,
        todate=todate,
        tf=tf,
        name=contract,
    )
    data.sec = sec
    return data


def run_contract(data, params, start_cash):
    cerebro = bt.Cerebro()
    cerebro.broker = bt.brokers.BackBroker()
    cerebro.broker.setcash(start_cash)
    cerebro.broker.addcommissioninfo(futures_comm[data.sec], name=data.p.name)
    cerebro.adddata(data)
    cerebro.addstrategy(AutoTuneFilterStrategy, **params)
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')

    start_value = cerebro.broker.getvalue()
    runs = cerebro.run(tradehistory=params['write_history'])
    # cerebro.plot(style='candle')
    plot(cerebro,
         indicators=[
        {
            "name": "ROC",
            "path": "roc",
            "panel": "Сигналы",
            "series": "histogram",
            # "data_index": 0,
        }
    ],)
    strategy = runs[0]
    finish_value = cerebro.broker.getvalue()

    trades = strategy.analyzers.trades.get_analysis()
    drawdown = strategy.analyzers.drawdown.get_analysis()

    print(f'Контракт: {data.p.name}')
    print(f'Стартовый капитал: {start_value:.2f}')
    print(f'Финальный капитал: {finish_value:.2f}')
    print(f'Итог: {finish_value - start_value:.2f}')
    print_trade_analysis(trades)
    print(f"Max DD: {drawdown.max.drawdown:.2f}% / {drawdown.max.moneydown:.2f}")
    print('-' * 60)

    return finish_value - start_value


def main():
    # Фильтр AutoTune https://financial-hacker.com/the-autotune-filter/
    start_cash = 300000.0
    params = dict( # CNY_1h - final 07-05-26
        write_history=True,
        risk=5,
        window=28,  #[48, 49, 50],  #range(16,57),  #30,
        bandwidth=[i / 100 for i in range(43, 48)], #[0.4, 0.45, 0.45],[0.34, 0.35, 0.36],  # [i/100 for i in range(30, 51, 2)],  #[0.16, 0.24, 0.32, 0.4], # 0.22, #
        thresh=[-i / 100 for i in range(65, 86)],  #-0.7,  #[-0.48, -0.49, 0.50],  #[-i/100 for i in range(42, 55, 2)],  #[-i / 12.5 for i in range(4, 9)],  #[0.32, 0.4, 0.48, 0.56, 0.64], #
        allow_short=True,
        printlog=False,
        tp_mult=[i / 10 for i in range(17, 21)],  #1.5,  #[1+i/10 for i in range(1,7)],   # тейк-профит в R
    )

    params = dict(  # RTS_1h
        write_history=True,
        risk=5,
        window=36,  #range(31, 44),
        bandwidth=0.25,  #0.21,  #[i / 100 for i in range(19, 24)],  # [0.4, 0.45, 0.45],
        thresh=-0.48,  #[-i / 100 for i in range(48, 53)],  # [-0.45, -0.5, -0.55],
        allow_short=True,
        tp_mult=1.2,  #[i / 10 for i in range(12, 15)],  # [1.7, 1.8, 1.9, 2],
        printlog=False
    )

    # tf = params['tf'] = '15m'
    # tf = params['tf'] = '30m'
    # tf = params['tf'] = '1h'
    tf = '1h'
    # tf = params['tf'] = '1d'
    start_date = '2025-6-20'

    # end_date = params['end_date'] = '2026-3-17'  # datetime.today()
    end_date = datetime.today()
    # futures = ['RTS', 'RTSM', 'NASD', 'CNY', 'Eu', 'NG', 'GOLD', 'SBRF']
    # futures = ['CNY', ]
    # futures = ['Si', ]
    # futures = ['RTS', ]
    # futures = ['SPYF', ]

    sec = 'MIX'  # 'CNY' 'RTS'
    ensure_single_params(params)

    total_time = _time.time()
    store = MoexStore()
    contracts = store.futures.contracts_between(sec, start_date, end_date)
    print(contracts)
    print(f'Запускаем одиночную стратегию для {len(contracts)} контрактов, {datetime.now():%H:%M:%S}')

    total_pnl = 0.0
    for contract in contracts:
        data = make_contract_data(store, sec, contract, contracts, start_date, end_date, tf)
        total_pnl += run_contract(data, params, start_cash)

    print(f'Суммарный PNL по контрактам: {total_pnl:.2f}')
    print(f'Весь прогон за {round(_time.time() - total_time, 2)} сек.')


if __name__ == '__main__':
    main()
