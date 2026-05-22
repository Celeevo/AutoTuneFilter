from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import os
import time as _time
from datetime import datetime, time, timedelta
import backtrader as bt
from matplotlib.style.core import available
from moex_store import MoexStore
import gc
from atf import AutoTuneFilter
from backtrader import Analyzer
from math import sqrt
import numpy as np
import pandas as pd
from itertools import chain
from statistics import mean, stdev
import xlsxwriter
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
                          margin=26358, # 27/04/25, 24700,  # ГО 05.12.2024 08-05-26
                          mult=14.92418/10,  # мультипликатор Стоимость шага цены/Шаг цены 07-05-26 - 15.04492
                          moexcomm=FUTURE_TYPE['xindex'],
                          cost_of_price_step=10),
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
                          margin=11934,  # ГО  05.12.2024 07-05-26
                          mult=1,  # мультипликатор
                          moexcomm=FUTURE_TYPE['currency'],
                          cost_of_price_step=1),
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


class ATRRiskSizer(bt.Sizer):
    """
        Размер позиции для фьючерсов:
        - по риску до ATR-стопа
        - с учётом ограничения по ГО

        Стратегия ДО buy()/sell() должна записать:
        - self.entry_price
        - self.stop_loss_price

        Логика:
            size_by_risk  = risk_cash / (stop_dist * mult)
            size_by_go    = cash / margin
            size          = min(size_by_risk, size_by_go)

        Где:
        - mult   = денежная стоимость 1 пункта движения на 1 контракт
        - margin = ГО на 1 контракт
        """

    def _getsizing(self, comminfo, cash, data, isbuy):
        entry_price = getattr(self.strategy, 'entry_price', None)
        stop_loss_price = getattr(self.strategy, 'stop_loss_price', None)

        if entry_price is None or stop_loss_price is None:
            return 0

        stop_dist = abs(entry_price - stop_loss_price)
        if stop_dist <= 0:
            return 0

        # Риск в деньгах — как у тебя раньше: % от текущего cash
        risk_cash = cash * (self.strategy.p.risk / 100)

        # Денежный риск на 1 контракт
        risk_per_contract = stop_dist * comminfo.p.mult
        if risk_per_contract <= 0:
            return 0

        # Размер по риску
        size_by_risk = risk_cash / risk_per_contract

        # Размер по ГО
        if comminfo.p.margin:
            size_by_go = cash / comminfo.p.margin
        else:
            size_by_go = cash / entry_price

        # Как и в твоём AllInSizer, оставим запас в 1 контракт
        size = int(min(size_by_risk, size_by_go)) - 1

        if size <= 0:
            return 0

        return size

def iterable_params(p:dict):
    '''
    Анализируем params (р), передаваемые в cerebro.optstrategy
    и возвращаем имена (ключи params) тех, которые оптимизируются (итерируются)
    Если нет итерируемых параметров (такое бывает) - возвращаем строку 'params'
    '''
    names = [k for k,v in p.items() if isinstance(v, (list, tuple, set, range))]
    return names if names else ['params']

def count_param_variants(params_dict):
    variants = 1
    for value in params_dict.values():
        if isinstance(value, (list, tuple, set, range)):
            variants *= len(value)
    return variants

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
    aggr = df.groupby(first_col, sort=False)[['PNLs', 'PNL', 'Asset']].apply(compute_group_metrics, startingcash=startingcash).reset_index()
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
                    # entry_ma_disposition = entry_order.info.ma_disp,

                    exit_ref=exit_order.ref,
                    exit_created_date=f'{bt.num2date(exit_order.created.dt):%d.%m.%y}',
                    exit_created_time=f'{bt.num2date(exit_order.created.dt):%H:%M}',
                    exit_executed_date=f'{bt.num2date(exit_order.executed.dt):%d.%m.%y}',
                    exit_executed_time=f'{bt.num2date(exit_order.executed.dt):%H:%M}',
                    exit_requested_price=exit_order.created.price,
                    exit_executed_price=exit_order.executed.price,
                    # exit_type=exit_order.info.name,

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
            self.atf.filt > 0
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



def main(maxcpus=None):
    # Фильтр AutoTune https://financial-hacker.com/the-autotune-filter/
    # 26-04-26 50-0.34--0.48-1.5-MIX
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
        bandwidth=0.21,  #[i / 100 for i in range(19, 24)],  # [0.4, 0.45, 0.45],
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
    start_date = '2022-6-20'

    # end_date = params['end_date'] = '2026-3-17'  # datetime.today()
    end_date = datetime.today()
    main_opt_metric = 'PROM'  # 'PROM'

    # futures = ['RTS', 'RTSM', 'NASD', 'CNY', 'Eu', 'NG', 'GOLD', 'SBRF']
    # futures = ['CNY', ]
    # futures = ['Si', ]
    # futures = ['RTS', ]
    # futures = ['SPYF', ]

    sec = 'RTS'  # 'CNY'
    total_time = _time.time()
    store = MoexStore()
    datas = list()

    # for sec in futures:
    contracts = store.futures.contracts_between(sec, start_date, end_date)
    print(contracts)

    variants = count_param_variants(params)


    sheet_size = (variants * len(contracts)) > 1048576
    if sheet_size:
        print(f"Excel sheet is too large! Your sheet size is: {variants * len(contracts)}, Max sheet size is: 1'048'576")
    print(f'Рассчитываем {variants} вариантов стратегии для '
          f'каждого из {len(contracts)} контрактов. Итого '
          f'{variants * len(contracts)} вариантов.')
    print(f'Время пошло, {datetime.now():%H:%M:%S}')

    for contract in contracts:
        prevexpdate = pd.to_datetime(store.futures.prevexpdate(contract))

        if contract == contracts[0]:
            fromdate = pd.to_datetime(start_date) - timedelta(days=5)
            # start_trades = pd.to_datetime(start_date)
        else:
            fromdate = prevexpdate - timedelta(days=5)
            # start_trades = prevexpdate
        if contract == contracts[-1]:
            todate = end_date
        else:
            todate = store.futures.expdate(contract)

        data = store.getdata(sec_id=contract,
                                   fromdate=fromdate,
                                   todate=todate,
                                   tf=tf, name=contract)

        data.sec = sec
        datas.append(data)

    results = []
    trades = []
    analyzer_params = dict(it_params=iterable_params(params))

    for data in datas:
        analyzer_params['asset'] = data.sec
        st_time = _time.time()
        cerebro = bt.Cerebro()
        cerebro.broker = bt.brokers.BackBroker()
        cerebro.broker.setcash(start_cash)
        cerebro.broker.addcommissioninfo(futures_comm[data.sec], name=data.p.name)
        cerebro.addsizer(AllInSizer)
        # cerebro.addsizer(ATRRiskSizer)
        cerebro.addanalyzer(SmartAnalyzer, _name='full', **analyzer_params)
        cerebro.adddata(data)

        cerebro.optstrategy(AutoTuneFilterStrategy, **params)
        # cerebro.optstrategy(TrioVesperFin_Chaikin, **params)
        # cerebro.optstrategy(TrioChaikin, **params)
        # cerebro.optstrategy(TrioChaikinWithTrailingExit, **params)
        runs = cerebro.run(stdstats=False, tradehistory=params["write_history"], maxcpus=maxcpus)

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
    df3 = aggregate_df(df1, start_cash, sort_by=main_opt_metric)
    df4 = pd.DataFrame(list(params.items()), columns=['Parameter', 'Value'])
    del df1['PNLs']

    # Сохраняем штамп времени для имени XLSX-файла с результатами
    timestamp = datetime.now().strftime("%d-%m-%y %H-%M")

    # Создаём имя XLSX-файла результатов
    results_file = f'opt_results_{sec}_{tf}_{timestamp}.xlsx'

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
    maxcpus = os.cpu_count()
    available_cpus = maxcpus - 3
    print(f'Задействуем {available_cpus} потоков из {maxcpus} возможных.')
    main(available_cpus)


# -------------------------------------------------------
'''
    params = dict( # CNY_1h - final 07-05-26, для 1 год - хорошо, для 4 - плохо!
        write_history=True,
        risk=5,
        window=28,  #[48, 49, 50],  #range(16,57),  #30,
        bandwidth=[i / 100 for i in range(43, 48)], #[0.4, 0.45, 0.45],[0.34, 0.35, 0.36],  # [i/100 for i in range(30, 51, 2)],  #[0.16, 0.24, 0.32, 0.4], # 0.22, #
        thresh=[-i / 100 for i in range(65, 86)],  #-0.7,  #[-0.48, -0.49, 0.50],  #[-i/100 for i in range(42, 55, 2)],  #[-i / 12.5 for i in range(4, 9)],  #[0.32, 0.4, 0.48, 0.56, 0.64], #
        allow_short=True,
        printlog=False,
        tp_mult=[i / 10 for i in range(17, 21)],  #1.5,  #[1+i/10 for i in range(1,7)],   # тейк-профит в R
    )
    
        params = dict( # RTS_1h
        write_history=True,
        risk=5,
        window=range(31, 44),
        bandwidth=[i / 100 for i in range(19, 24)],  # [0.4, 0.45, 0.45],
        thresh=[-i / 100 for i in range(48, 53)],  # [-0.45, -0.5, -0.55],
        allow_short=True,
        tp_mult=[i / 10 for i in range(12, 15)],  # [1.7, 1.8, 1.9, 2],
        printlog=False
        
        params = dict(  # RTS_1h  - final 07-05-26, для 1 год - 670_375, для 4 - 1_044_848 плохо !
        write_history=True,
        risk=5,
        window=36,  #range(31, 44),
        bandwidth=0.21,  #[i / 100 for i in range(19, 24)],  # [0.4, 0.45, 0.45],
        thresh=-0.48,  #[-i / 100 for i in range(48, 53)],  # [-0.45, -0.5, -0.55],
        allow_short=True,
        tp_mult=1.2,  #[i / 10 for i in range(12, 15)],  # [1.7, 1.8, 1.9, 2],
        printlog=False
    )
    )
'''