from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import os
import time as _time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from statistics import mean
from itertools import chain

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
    return itp if itp else ['params']


def aggregate_results(df: pd.DataFrame) -> pd.DataFrame:
    """
    Агрегирует результаты по вариантам параметров стратегии.

    Важно:
    оптимизация запускается отдельно для каждого фьючерсного контракта,
    поэтому одна и та же строка параметров встречается несколько раз.
    Здесь мы объединяем все сделки по одному варианту параметров
    и считаем итоговые метрики уже по всем контрактам вместе.
    """
    params_col = df.columns[0]
    rows = []

    for params_value, group in df.groupby(params_col, sort=False):
        pnls = list(chain.from_iterable(group['PNLs']))
        win_trades = [p for p in pnls if p >= 0]
        loss_trades = [p for p in pnls if p < 0]

        pnl = sum(pnls)
        win_count = len(win_trades)
        loss_count = len(loss_trades)
        sum_win = sum(win_trades)
        sum_loss = sum(loss_trades)
        avg_win = mean(win_trades) if win_count else 0.0
        avg_loss = mean(loss_trades) if loss_count else 0.0
        ratio = round(win_count / loss_count, 2) if loss_count else '∞'
        pf = round(sum_win / abs(sum_loss), 2) if sum_loss else '∞'

        rows.append({
            'Вариант параметров': params_value,
            'PnL': round(pnl, 2),
            'Приб. сделок': win_count,
            'Убыт. сделок': loss_count,
            'Отношение': ratio,
            'PF': pf,
            'Средн. приб. сделка': round(avg_win, 2),
            'Средн. убыточ. сделка': round(avg_loss, 2),
        })

    table = pd.DataFrame(rows)
    table = table.sort_values(by='PnL', ascending=False).reset_index(drop=True)
    return table


def print_optimization_table(table: pd.DataFrame):
    """
    Печатает агрегированную таблицу результатов оптимизации в терминал.
    """
    print('\nТАБЛИЦА РЕЗУЛЬТАТОВ ОПТИМИЗАЦИИ:\n')
    with pd.option_context(
        'display.max_rows', None,
        'display.max_columns', None,
        'display.width', 220,
        'display.max_colwidth', 60,
    ):
        print(table.to_string(index=False))


class SmartAnalyzer(Analyzer):
    """
    Собирает простые метрики для одного прогона стратегии.

    Дальше эти результаты будут агрегированы по вариантам параметров
    уже между разными фьючерсными контрактами.
    """

    params = dict(it_params=None)

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
        params_head = '-'.join(str(k) for k in self.p.it_params)
        params_str = '-'.join(str(st_params[k]) for k in self.p.it_params)

        wt = len(self.pt_arr)
        lt = len(self.lt_arr)
        aw = mean(self.pt_arr) if wt else 0.0
        al = mean(self.lt_arr) if lt else 0.0
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

    def get_trades_pnl(self):
        return self.pt_arr + self.lt_arr


class AutoTuneFilterStrategy(bt.Strategy):
    """
    Strategy based on Financial Hacker article:
    - ROC = BP - BP[2]
    - Long  when ROC crosses above 0 and MinCorr < Thresh
    - Short when ROC crosses below 0 and MinCorr < Thresh and Filt > 0
    """

    params = dict(
        write_history=False,
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
        window=[48, 49, 50],
        bandwidth=[0.34, 0.35, 0.36],
        thresh=[-0.48, -0.49, -0.50],
        allow_short=True,
        printlog=False,
        tp_mult=1.5,
    )

    tf = '1h'
    start_date = params['start_date'] = '2022-6-20'
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
                analysis['PNLs'] = analyzer.get_trades_pnl()
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
    table = aggregate_results(df)
    print_optimization_table(table)


if __name__ == '__main__':
    maxcpus = os.cpu_count()
    print(f'Задействуем {maxcpus - 3} потоков и {maxcpus} возможных.')
    main(maxcpus)
