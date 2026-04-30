from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import os
import time as _time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from statistics import mean

import backtrader as bt
import gc
import pandas as pd
from backtrader import Analyzer
from moex_store import MoexStore

from atf import AutoTuneFilter


class FuturesCommission(bt.CommInfoBase):
    params = dict(moexcomm=0.0, cost_of_price_step=0)

    def _getcommission(self, size, price, pseudoexec):
        brokers_pocket = abs(size) * self.p.commission
        moexs_pocket = abs(size) * price * self.p.mult * self.p.moexcomm / 100
        return brokers_pocket + moexs_pocket


futures_comm = FuturesCommission(
    commission=2.0,          # 2 руб за контракт
    margin=33000,            # ГО
    mult=1,                  # стоимость пункта на 1 контракт
    moexcomm=0.00660,        # комиссия биржи для индексных контрактов
    cost_of_price_step=25,   # шаг цены
)


def round_to_nearest_price_step(step, value, isbuy):
    """
    Округляет цену к шагу инструмента.

    isbuy=True  -> округление вниз
    isbuy=False -> округление вверх
    """
    if step <= 0:
        raise ValueError('step должен быть > 0')

    step_d = Decimal(str(step))
    value_d = Decimal(str(value))
    steps_cnt = value_d / step_d

    rounding_mode = ROUND_FLOOR if isbuy else ROUND_CEILING
    steps_cnt = steps_cnt.to_integral_value(rounding=rounding_mode)

    return float(steps_cnt * step_d)


class AllInSizer(bt.Sizer):
    """
    Максимально возможный вход по ГО.

    Дополнительно рассчитывает stop_loss_price из заданного риска %.
    """

    def _getsizing(self, comminfo, cash, data, isbuy):
        if comminfo.p.margin:  # для фьючерсов
            max_size = cash / comminfo.p.margin
        else:
            max_size = cash / self.strategy.entry_price

        size = int(max_size) - 1
        if size <= 0:
            return 0

        direction = 2 * isbuy - 1  # 1 для long, -1 для short
        stop_loss_price = (
            self.strategy.entry_price
            - direction * cash * (self.strategy.p.risk / 100) / (size * comminfo.p.mult)
        )

        if comminfo.p.cost_of_price_step != 0:
            self.strategy.stop_loss_price = round_to_nearest_price_step(
                comminfo.p.cost_of_price_step,
                stop_loss_price,
                isbuy,
            )
        else:
            self.strategy.stop_loss_price = stop_loss_price

        return size


def iterable_params(params_dict: dict):
    """
    Возвращает имена параметров, которые реально оптимизируются.
    """
    itp = [k for k, v in params_dict.items() if isinstance(v, (list, tuple, set, range))]
    return itp if itp else 'params'


def print_optimization_table(df: pd.DataFrame):
    """
    Печатает простую таблицу результатов оптимизации в терминал.

    Колонки:
    1. Вариант параметров
    2. PnL
    3. Количество прибыльных сделок
    4. Количество убыточных сделок
    5. Их отношение
    6. Профит-фактор
    7. Средняя прибыльная сделка
    8. Средняя убыточная сделка
    """
    params_col = df.columns[0]

    table = pd.DataFrame()
    table['Вариант параметров'] = df[params_col].astype(str) + ' | ' + df['Data'].astype(str)
    table['PnL'] = df['PNL'].round(2)
    table['Приб. сделок'] = df['WinTr'].astype(int)
    table['Убыт. сделок'] = df['LossTr'].astype(int)

    table['Отношение'] = df.apply(
        lambda row: round(row['WinTr'] / row['LossTr'], 2) if row['LossTr'] != 0 else '∞',
        axis=1,
    )

    table['PF'] = df.apply(
        lambda row: round(row['SumWin'] / abs(row['SumLoss']), 2) if row['SumLoss'] != 0 else '∞',
        axis=1,
    )

    table['Средн. приб. сделка'] = df['AvgWin'].round(2)
    table['Средн. убыточ. сделка'] = df['AvgLoss'].round(2)

    table = table.sort_values(by='PnL', ascending=False).reset_index(drop=True)

    print('\nТАБЛИЦА РЕЗУЛЬТАТОВ ОПТИМИЗАЦИИ:\n')
    with pd.option_context(
        'display.max_rows', None,
        'display.max_columns', None,
        'display.width', 240,
        'display.max_colwidth', 70,
    ):
        print(table.to_string(index=False))


class SmartAnalyzer(Analyzer):
    """
    Собирает простые метрики для таблицы оптимизации.
    """

    params = dict(it_params=None, asset=None)

    def __init__(self):
        self.pt_arr = []
        self.lt_arr = []

    def notify_trade(self, trade):
        if trade.isclosed:
            if trade.pnlcomm >= 0:
                self.pt_arr.append(trade.pnlcomm)
            else:
                self.lt_arr.append(trade.pnlcomm)

    def stop(self):
        st_params = self.strategy.p._getkwargs()
        params_head = '-'.join(str(k) for k in st_params.keys() if k in self.p.it_params) + '-asset'
        params_str = '-'.join(str(v) for k, v in st_params.items() if k in self.p.it_params) + '-' + self.p.asset

        wt = len(self.pt_arr)
        lt = len(self.lt_arr)
        aw = mean(self.pt_arr) if wt else 0
        al = mean(self.lt_arr) if lt else 0
        swt = sum(self.pt_arr)
        slt = sum(self.lt_arr)
        pnl = int(sum(self.pt_arr + self.lt_arr))

        self.rets[params_head] = params_str
        self.rets['PNL'] = pnl
        self.rets['WinTr'] = wt
        self.rets['LossTr'] = lt
        self.rets['SumWin'] = swt
        self.rets['SumLoss'] = slt
        self.rets['AvgWin'] = aw
        self.rets['AvgLoss'] = al


class AutoTuneFilterStrategy(bt.Strategy):
    """
    Strategy based on Financial Hacker article:
    - ROC = BP - BP[2]
    - Long  when ROC crosses above 0 and MinCorr < Thresh
    - Short when ROC crosses below 0 and MinCorr < Thresh and Filt > 0
    """

    params = dict(
        write_history=False,   # для учебной таблицы история сделок не нужна
        depo=0,
        risk=None,
        start_date=None,
        end_date=None,
        window=26,
        bandwidth=0.22,
        thresh=-0.22,
        allow_short=True,
        printlog=False,
        tp_mult=2.0,
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
                    raw_take_profit = executed_entry + self.p.tp_mult * risk_points
                    if comminfo.p.cost_of_price_step != 0:
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
                    raw_take_profit = executed_entry - self.p.tp_mult * risk_points
                    if comminfo.p.cost_of_price_step != 0:
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

            elif order.info.name == 'stop_loss':
                self.stop_order = None
                self.take_profit_order = None
                self.stop_loss_price = 0.0
                self.take_profit_price = 0.0
                self.entry_price = 0.0

            elif order.info.name == 'take_profit':
                self.take_profit_order = None
                self.stop_order = None
                self.stop_loss_price = 0.0
                self.take_profit_price = 0.0
                self.entry_price = 0.0

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            if order == self.stop_order:
                self.stop_order = None
            elif order == self.take_profit_order:
                self.take_profit_order = None

        self.order = None

    def next(self):
        if self.order:
            return

        mincorr_now = self.atf.mincorr[0]
        filt_now = self.atf.filt[0]

        long_signal = bool(self.cross_up[0] and mincorr_now < self.p.thresh)
        short_signal = bool(
            self.cross_down[0]
            and mincorr_now < self.p.thresh
            and filt_now > 0
        )

        if not self.position:
            if long_signal:
                self.entry_price = self.data.close[0]
                self.order = self.buy(name='long')
            elif self.p.allow_short and short_signal:
                self.entry_price = self.data.close[0]
                self.order = self.sell(name='short')
            return

        # Если позиция уже есть, ждём только stop-loss или take-profit
        if self.position:
            return

        if self.stop_order or self.take_profit_order:
            return


def main(maxcpus=None):
    # Учебный пример оптимизации
    params = dict(
        write_history=False,
        depo=300000.0,
        risk=5,
        window=[49, 50],
        bandwidth=[0.34, 0.35],
        thresh=[-0.48, -0.49],
        allow_short=True,
        printlog=False,
        tp_mult=1.5,
    )

    tf = '1h'
    start_date = params['start_date'] = '2025-6-20'
    end_date = params['end_date'] = datetime.today()

    futures = ['MIX']

    total_time = _time.time()
    store = MoexStore()
    datas = []

    for sec in futures:
        contracts = store.futures.contracts_between(sec, start_date, end_date)
        print(contracts)

        variants = 1
        for v in params.values():
            if isinstance(v, (tuple, range, list)):
                variants *= len(v)

        print(
            f'Рассчитываем {variants} вариантов стратегии для каждого из {len(contracts)} контрактов. '
            f'Итого {variants * len(contracts)} вариантов.'
        )
        print(f'Время пошло, {str(datetime.now().time())[:8]}')

        for contract in contracts:
            prevexpdate = pd.to_datetime(store.futures.prevexpdate(contract))
            if contract == contracts[0]:
                fromdate = pd.to_datetime(start_date) - timedelta(days=5)
                start_trades = pd.to_datetime(start_date)
            else:
                fromdate = prevexpdate - timedelta(days=5)
                start_trades = prevexpdate

            if contract == contracts[-1]:
                todate = end_date
            else:
                todate = store.futures.expdate(contract)

            data = store.getdata(
                sec_id=contract,
                fromdate=fromdate,
                todate=todate,
                tf=tf,
                name=contract
            )
            data.start_trades = start_trades
            data.end_trades = pd.to_datetime(todate)
            data.sec = sec
            datas.append(data)

    results = []
    aparams = dict(it_params=iterable_params(params))

    for data in datas:
        aparams['asset'] = data.sec
        st_time = _time.time()

        cerebro = bt.Cerebro()
        cerebro.broker = bt.brokers.BackBroker()
        cerebro.broker.setcash(params['depo'])
        cerebro.broker.addcommissioninfo(futures_comm, name=data.p.name)
        cerebro.addsizer(AllInSizer)
        cerebro.addanalyzer(SmartAnalyzer, _name='full', **aparams)
        cerebro.adddata(data)

        cerebro.optstrategy(AutoTuneFilterStrategy, **params)
        runs = cerebro.run(stdstats=False, tradehistory=False, maxcpus=maxcpus)

        for run in runs:
            for strategy in run:
                analyzer = strategy.analyzers.full
                analysis = dict(analyzer.get_analysis())
                analysis['Data'] = data.p.name
                results.append(analysis)

        print(
            f'Прогон {len(runs)} вариантов стратегии для контракта '
            f'{data.p.name} за {round(_time.time() - st_time, 2)} сек., '
            f'V = {round(len(runs) / (_time.time() - st_time), 2)} вар/сек'
        )
        gc.collect()

    print(
        f'Весь прогон за {round(_time.time() - total_time, 2)} сек., '
        f'{round((_time.time() - total_time) / 3600, 2)} часов.'
    )

    df = pd.DataFrame(results).round(2)
    print_optimization_table(df)


if __name__ == '__main__':
    maxcpus = os.cpu_count()
    print(f'Задействуем {maxcpus - 3} потоков и {maxcpus} возможных.')
    main(maxcpus)
