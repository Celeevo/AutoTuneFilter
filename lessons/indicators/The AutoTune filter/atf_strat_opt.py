from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
import time as _time
from datetime import datetime, time, timedelta
import backtrader as bt
# from _moex_store import MoexStor
from moex_store import MoexStore
import pandas as pd
import math
import gc
from atf import AutoTuneFilter
from backtrader import Analyzer
from math import sqrt
import numpy as np
import pandas as pd
from itertools import chain
from statistics import mean, stdev
import xlsxwriter


def iterable_params(p:dict):
    '''
    Анализируем params (р), передаваемые в cerebro.optstrategy
    и возвращаем имена (ключи params) тех, которые оптимизируются (итерируются)
    Если нет итерируемых параметров (такое бывает) - возвращаем строку 'params'
    '''
    itp = [k for k,v in p.items() if isinstance(v, (list, tuple, set, range))]
    if itp:
        return itp
    return 'params'

def compute_group_metrics(group, startingcash=1):
    # Объединяем списки PNLs из группы
    pnls = list(chain.from_iterable(group['PNLs']))
    wta = [i for i in pnls if i >= 0]  # Выигрышные сделки
    lta = [i for i in pnls if i < 0]  # Проигрышные сделки
    pnl = sum(pnls)
    lenw = len(wta)
    lenl = len(lta)
    swt = sum(wta)
    slt = sum(lta)
    aw = np.mean(wta) if lenw > 0 else 0
    al = np.mean(lta) if lenl > 0 else 0
    w_l = lenw - lenl
    w_div_l = lenw / lenl if lenl != 0 else 0
    mw = max(wta) if lenw > 0 else 0
    mean = np.mean(pnls) if pnls else 0
    # stdev = np.std(pnls, ddof=1) if len(pnls) > 1 else 0
    stdev = np.std(group['PNL'], ddof=1) if len(group) > 1 else 0

    # Получаем последние значения PNL
    last_pnl = group['PNL'].iloc[-1]
    pre_last_pnl = group['PNL'].iloc[-2] if len(group) >= 2 else 0

    # Вычисляем Profit Factor
    pf = -swt / slt if slt else 0

    # Вычисляем PROM
    prom = (aw * (lenw - sqrt(lenw)) + al * (lenl + sqrt(lenl))) / startingcash

    # Вычисляем e-Pardo
    if lenw > 1:
        e_pardo = ((swt - mw) / (lenw - 1) * (lenw - 1 - sqrt(lenw)) + al * (lenl + sqrt(lenl))) / startingcash
    else:
        e_pardo = 0

    # Вычисляем s-Pardo
    if stdev > 0 and mean > 0:
        s_pardo = e_pardo * sqrt(mean / stdev)
    else:
        s_pardo = 0

    # Вычисляем кол-во отриц PNL для набора параметров
    neg_pnls = (group['PNL'] < 0).sum()
    last4 = group['PNL'].iloc[-4:]
    last4neg = (last4 < 0).sum()

    # Берем значение Asset из первой строки группы
    asset = group['Asset'].iloc[0]

    # Формируем результирующую серию
    result = pd.Series({
        'Asset': asset,  # Добавляем колонку Asset
        'PNL': pnl,
        'WinTr': lenw,
        'LossTr': lenl,
        'SumWin': swt,
        'SumLoss': slt,
        'W-L': w_l,
        'W/L': w_div_l,
        'AvgWin': aw,
        'AvgLoss': al,
        'StdDev': stdev,
        'LastPNL': last_pnl,
        'PreLastPNL': pre_last_pnl,
        'PF': pf,
        'PROM': prom,
        'e-Pardo': e_pardo,
        's-Pardo': s_pardo,
        'NegPNLs': neg_pnls,
        'Last4Neg': last4neg
    })

    return result

def aggregate_df(df, startingcash=1, sort_by='s-Pardo', sort_by_second='s-Pardo'):
    first_col = df.columns[0]
    aggr = df.groupby(first_col, sort=False).apply(compute_group_metrics, startingcash=startingcash).reset_index()
    aggr = aggr.sort_values(sort_by, ascending=False)
    # Проверка значения в столбце 's-Pardo' и вторичная сортировка при необходимости
    if 's-Pardo' in aggr.columns and aggr['s-Pardo'].iloc[0] <= 0:
        aggr = aggr.sort_values(sort_by_second, ascending=False)
    return aggr.round(2)

class SmartAnalyzer(Analyzer):
    '''
    Возвращаем:
    - params: значения параметров для данного варианта Стратегии
    - PNL: сумма прибыльных сделок минус сумма убыточных сделок
    - win_total: кол-во прибыльных сделок
    - loss_total: кол-во убыточных сделок
    - win_rate: отношение прибыльных и убыточных сделок в %
    - PF: профит-фактор
    - avr_win: средняя прибыльная сделка
    - avr_loss: средняя убыточная сделка
    - PROM:
        Пессимистическая доходность на маржу (The pessimistic return on margin, PROM) – это доход на
        маржу, скорректированный на «пессимистическое допущение», согласно которому в реальной торговле система будет
        выигрывать меньше и проигрывать больше, чем при тестировании.
        PROM = (AW * (WT – Sqrt(WT)) – AL * (LT + Sqrt(LT))) / Margin
            WT       = Number of Wins
            AW       = Average Win
            LT       = Number of Losses
            AL       = Average Loss
            Sqrt     = Square root
            Margin   = Starting capital
    - max_DD: максимальная относительная просадка в процентах (выражена как десятичная дробь, например, 0.25
        соответствует 25%). Это показатель наибольшего снижения стоимости вашего портфеля от предыдущего максимума в
        процентном выражении за весь период тестирования стратегии. Он позволяет оценить, насколько сильно могла
        уменьшиться стоимость вашего портфеля в самый неблагоприятный момент.
    - max_money_DD: максимальная просадка в денежных единицах. Это наибольшая сумма денег, которую ваш портфель
        потерял от предыдущего максимума за весь период. Этот показатель важен для понимания абсолютных денежных
        рисков и того, насколько значимыми могут быть потери в реальных деньгах.
    - SQN: SystemQualityNumber
    - SHARP: Этот анализатор вычисляет коэффициент Шарпа стратегии, используя безрисковый актив, который представляет
        собой просто процентную ставку (на дворе 2024 год - я взял 20% годовых для безрискового актива)
    '''

    params=dict(it_params=None, asset=None)

    def __init__(self):
        # Списки результатов прибыльных и убыточных сделок
        self.pt_arr = list()
        self.lt_arr = list()
        self.trades = list()
        self.trades_details = list()
        self.depos = self.strategy.broker.startingcash

    def notify_trade(self, trade):
        if trade.isclosed:
            if trade.pnlcomm >= 0:
                self.pt_arr.append(trade.pnlcomm)
            else:
                self.lt_arr.append(trade.pnlcomm)

            # Section for trade-book
            if self.strategy.p.write_history:
                trade.start_cash = self.depos
                trade.finish_cash = self.strategy.broker.getcash()
                trade.stop_loss_price = self.strategy.stop_loss_price
                trade.sec_id = trade.getdataname()
                # Был ли источник склеен с помощью rollover?
                # Если да, добавляем имена склеенных источников
                # if hasattr(trade.data, '_d'):
                #     trade.sec_id += '-' + trade.data._d._name
                self.trades.append(trade)
                self.depos = self.strategy.broker.getcash()

    def stop(self):
        '''
        В self.strategy.p.itp хранятся имена параметров params, которые оптимизируются (итераторы):
        - Формируем заголовок первой колонки params_head из этих имен, разделенных "-".
        - Формируем ячейки (строки) первой колонки params_values из значений имен итерируемых параметров,
        полученных из self.strategy.p._getkwargs() для этого конкретного экземпляра стратегии. Значения
        также разделяются "-".
        '''
        st_params = self.strategy.p._getkwargs()
        params_head = '-'.join(str(k) for k in st_params.keys() if k in self.p.it_params) + '-asset'
        params_str = '-'.join(str(v) for k, v in st_params.items() if k in self.p.it_params) + '-' + self.p.asset

        wt = len(self.pt_arr)  # win_total
        aw = mean(self.pt_arr) if wt else 0  # avr_win
        lt = len(self.lt_arr)  # loss_total
        al = mean(self.lt_arr) if lt else 0  # avr_loss
        swt = sum(self.pt_arr)  # sum profit trades
        slt = sum(self.lt_arr)  # sum losing trades
        pnl = int(sum(self.pt_arr + self.lt_arr))

        self.rets[params_head] = params_str
        self.rets['PNL'] = pnl
        self.rets['WinTr'] = wt
        self.rets['LossTr'] = lt
        self.rets['SumWin'] = swt
        self.rets['SumLoss'] = slt
        self.rets['AvgWin'] = aw
        self.rets['AvgLoss'] = al

        if self.strategy.p.write_history:

            for trade in self.trades:
                # entry_event, exit_event = trade.history[0], trade.history[-1]
                # entry_order, exit_order = entry_event.event.order, exit_event.event.order

                entry_event, exit_event = trade.history[0].event, trade.history[-1].event
                entry_order, exit_order = entry_event.order, exit_event.order

                tr = dict(
                    params=params_str,
                    sec=getattr(trade, 'sec_id', 0),
                    entry_ref=entry_order.ref,
                    entry_created_date=f'{bt.num2date(entry_order.created.dt):%d.%m.%y}',
                    entry_created_time=f'{bt.num2date(entry_order.created.dt):%H:%M}',
                    entry_executed_time=f'{bt.num2date(entry_order.executed.dt):%H:%M}',
                    entry_requested_price=entry_order.created.price,
                    entry_executed_price=entry_order.executed.price,
                    stop_loss_price=getattr(trade, 'stop_loss_price', 0),
                    size=entry_order.size,
                    entry_type='long' if entry_order.isbuy() else 'short',
                    entry_ma_disposition = entry_order.info.ma_disp,

                    exit_ref=exit_order.ref,
                    exit_created_date=f'{bt.num2date(exit_order.created.dt):%d.%m.%y}',
                    exit_created_time=f'{bt.num2date(exit_order.created.dt):%H:%M}',
                    exit_executed_date=f'{bt.num2date(exit_order.executed.dt):%d.%m.%y}',
                    exit_executed_time=f'{bt.num2date(exit_order.executed.dt):%H:%M}',
                    exit_requested_price=exit_order.created.price,
                    exit_executed_price=exit_order.executed.price,
                    exit_type=exit_order.info.name,

                    result=1 if trade.pnl > 0 else 0,
                    pnl=int(trade.pnlcomm),

                    cash_before=getattr(trade, 'start_cash', 0),
                    cash_after=getattr(trade, 'finish_cash', 0),
                )
                self.trades_details.append(tr)

    def get_trades(self):
        return self.trades_details

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
        write_history=None,  # Записываем или нет детальную инфу о каждой сделке
        depo=0,  # Начальный депозит
        tf=None,
        start_date=None,
        end_date=None,
        window=26,
        bandwidth=0.22,
        thresh=0.22,
        allow_short=True,
        printlog=False,
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

        # ROC из статьи: BP - BP[2]
        self.roc = self.atf.bp - self.atf.bp(-2)

        # Сигналы пересечения нуля
        self.cross_up = bt.indicators.CrossUp(self.roc, 0.0)
        self.cross_down = bt.indicators.CrossDown(self.roc, 0.0)

        self.order = None

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status == order.Completed:
            if order.isbuy():
                self.log(f'BUY EXECUTED at {order.executed.price:.2f}')
            else:
                self.log(f'SELL EXECUTED at {order.executed.price:.2f}')

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log(f'ORDER FAILED: {order.getstatusname()}')

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
                self.order = self.buy()

            elif self.p.allow_short and short_signal:
                self.log('SHORT SIGNAL -> sell()')
                self.order = self.sell()

            return

        # Уже long
        if self.position.size > 0:
            if self.p.allow_short and short_signal:
                self.log('REVERSE LONG -> SHORT')
                self.close()
                self.order = self.sell()

        # Уже short
        elif self.position.size < 0:
            if long_signal:
                self.log('REVERSE SHORT -> LONG')
                self.close()
                self.order = self.buy()


def main():

    # Фильтр AutoTune https://financial-hacker.com/the-autotune-filter/

    params = dict(
        write_history=False,
        depo=300000.0,  # Начальный депозит
        window=range(10,31),  #26,
        bandwidth=[0.1, 0.12, 0.14, 0.16, 0.18, 0.2, 0.22, 0.24, 0.26, 0.28, 0.3], # 0.22,
        thresh=[0.1, 0.12, 0.14, 0.16, 0.18, 0.2, 0.22, 0.24, 0.26, 0.28, 0.3], # 0.22,
        allow_short=True,
        printlog=False,

    )


    # 19-06-25 Смотрю MIX
    # 29-03-26 Смотрю MIX 1H
    params1 = dict(
        write_history=False,
        risk=5,  #range(4,7), #range(5,7),  # (float: 0.04 = 4%) Сколько % депозита можем потерять за одну сделку
        check_bar=6, #range(4,7),  # Кол-во свеч ожидания доп сигналов, после срабатывания основного
        depo=300000.0,  # Начальный депозит
        open_time=time(9, 0),  # [time(a,b) for a in range(9,11) for b in (0,30)],   #time(9,0),  #
        close_time=time(22, 0),  # [time(a,b) for a in range(18,23) for b in (0,30)],   #time(22,30),  #
        rsi=11,  #range(10, 14),  # Период входного RSI 8
        entry_ma=range(12, 21),  # Входное MA, период 15
        exit_ma=range(40, 71), #range(30, 51, 2),  # Exit MA, период 40
        macd_pp=(8, 20, 13),  #macd_set(7, 14, 8),  #[(4, 25, 5),], # macd_set(9, 30, 15),  # [(12, 26, 9)], # Параметры MACD (короткий, длинный период, период скользящей)
        vperc=50,  #range(20, 51, 10),
        # Пересечение Exit MA линией close для получения сигнала на вход. False - не используем.
        cross_exit_ma_for_entry_signal=False,
        # Фильтр на вход в лонг, если RSI уже больше 70 - не входим, и в short, если RSI уже меньше 30 - не входим в лонг
        rsi_filter_for_entry=False,  #(True,False), #
        # Выход в зависимости от входа. Если вход произошел до пересечения линией close линии Exit MA, то выход
        # осуществляются по их нормальному пересечению (close пересекает МА снизу вверх для Long). Если же вход
        # выполнен после пересечения линии close линии Exit Ma, то выход по обратному пересечению линии close и
        # линии Exit MA (close пересекает МА сверху вниз для Long). 1 - используем, 0 - всегда выход по обратному
        # пересечению.
        normal_ma_cross_for_exit=False,
    )


    # tf = params['tf'] = '30m'
    tf = params['tf'] = '1h'
    start_date = params['start_date'] = '2025-3-20'  # datetime.today() - timedelta(days=365)

    end_date = params['end_date'] = '2026-3-17'  # datetime.today()
    main_opt_metric = 'PROM'  # 'PROM'

    # futures = ['RTS', 'RTSM', 'NASD', 'CNY', 'Eu', 'NG', 'GOLD', 'SBRF']
    futures = ['MIX', ]
    # futures = ['Si', ]
    # futures = ['RTS', ]
    # futures = ['SPYF', ]

    total_time = _time.time()
    store = MoexStore()
    datas = list()

    for sec in futures:
        contracts = store.futures.contracts_between(sec, start_date, end_date)
        print(contracts)

        variants = 1
        for v in params.values():
            if isinstance(v, (tuple, range, list)):
                variants *= len(v)
        sheet_size = (variants * len(contracts)) > 1048576
        if sheet_size:
            print(f"Excel sheet is too large! Your sheet size is: {variants * len(contracts)}, Max sheet size is: 1'048'576")
        print(
            f'Рассчитываем {variants} вариантов стратегии для каждого из {len(contracts)} контрактов. Итого {variants * len(contracts)} '
            f'вариантов, est. time = {round(variants * len(contracts) / 50 / 60 / 60, 2)} часов.')
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

            data = store.getdata(sec_id=contract,
                                       fromdate=fromdate,
                                       todate=todate,
                                       tf=tf, name=contract)
            # data.start_trades = prevexpdate
            data.start_trades = start_trades
            data.end_trades = pd.to_datetime(todate)
            data.sec = sec
            # data.avg_volume = avg_vol(data)
            # print(f'Contract: {contract}, average volume: {data.avg_volume}')
            datas.append(data)

    results = []
    trades = []
    # it_params = iterable_params(params)
    aparams = dict(it_params=iterable_params(params))

    for data in datas:
        aparams['asset'] = data.sec
        st_time = _time.time()
        cerebro = bt.Cerebro()
        cerebro.broker = bt.brokers.BackBroker()
        cerebro.broker.setcash(params['depo'])
        # cerebro.broker.addcommissioninfo(futures_comm[data.sec], name=data.p.name)
        # cerebro.addsizer(AllInSizer)
        cerebro.addanalyzer(SmartAnalyzer, _name='full', **aparams)
        cerebro.adddata(data)

        cerebro.optstrategy(AutoTuneFilterStrategy, **params)
        # cerebro.optstrategy(TrioVesperFin_Chaikin, **params)
        # cerebro.optstrategy(TrioChaikin, **params)
        # cerebro.optstrategy(TrioChaikinWithTrailingExit, **params)
        runs = cerebro.run(stdstats=False, tradehistory=params["write_history"], maxcpus=30)

        for run in runs:  # тут все варианты для одного контракта
            for strategy in run:  # тут уникальные варианты по параметрам
                analyzer = strategy.analyzers.full
                analysis = dict()
                analysis.update(analyzer.get_analysis())
                analysis['Data'] = data.p.name
                analysis['PNLs'] = analyzer.get_trades_pnl()
                analysis['Asset'] = data.sec
                results.append(analysis)

                if params['write_history']:
                    trades_data = analyzer.get_trades()
                    trades.extend(trades_data)

        print(
            f'Прогон {len(runs)} вариантов стратегии для контракта '
            f'{data.p.name} за {round(_time.time() - st_time, 2)} сек., '
            f'{round((_time.time() - st_time) / 60, 2)} мин., '
            f'V (скорость) = {round(len(runs) / (_time.time() - st_time), 2)} вар/сек, '
            f'{str(datetime.now().time())[:5]}'
        )
        gc.collect()

    print(f'Весь прогон за {round(_time.time() - total_time, 2)} сек., '
          f'{round((_time.time() - total_time) / 3600, 2)} часов.')

    df1 = pd.DataFrame(results).round(2)
    if params['write_history']:
        df2 = pd.DataFrame(trades).round(3)
    df3 = aggregate_df(df1, params['depo'], sort_by=main_opt_metric)
    df4 = pd.DataFrame(list(params.items()), columns=['Parameter', 'Value'])
    del df1['PNLs']

    # Сохраняем штамп времени для имени XLSX-файла с результатами
    timestamp = datetime.now().strftime("%d-%m-%y %H-%M-%S")

    # Создаём имя XLSX-файла результатов
    results_file = f'opt_results_{tf}_{timestamp}.xlsx'

    # Записываем df в xlsx файл, xlsxwriter импортируем
    # отдельно pip install xlsxwriter
    with pd.ExcelWriter(results_file, engine='xlsxwriter') as writer:
        if not sheet_size:
            df1.to_excel(writer, sheet_name='by Contacts', index=False)
            if params['write_history']:
                df2.to_excel(writer, sheet_name='trades', index=False)
        df3.to_excel(writer, sheet_name='results', index=False)
        df4.to_excel(writer, sheet_name='params', index=False)

    print(f"Результаты успешно сохранены в файл '{results_file}'.")


if __name__ == '__main__':
    main()