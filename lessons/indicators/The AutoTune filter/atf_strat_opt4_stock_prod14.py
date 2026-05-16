from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import os
import re
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

SCRIPT_NAME = os.path.splitext(os.path.basename(__file__))[0]
SCRIPT_VERSION_MATCH = re.search(r'(prod\d+)', SCRIPT_NAME, re.IGNORECASE)
SCRIPT_VERSION = SCRIPT_VERSION_MATCH.group(1).lower() if SCRIPT_VERSION_MATCH else SCRIPT_NAME

# 08-05-26 Это пока крайняя версия, новая opt5 - в тесте, версии opt_csv имеют упрощенный
#           вывод результата и пока дальше не развиваем.
# 09-05-26 Добавил фильтры по dc и adx. Семантика границ такая:
#           - min_dc=25 означает «вход разрешён, если dc >= 25», то есть отсекаются сделки с dc < 25.
#           - max_adx=40 означает «вход разрешён, если adx <= 40», отсекаются сделки с adx > 40.
#           Дефлтные значения (фильтры выключены):
#           - min_dc=0,      # минимальный доминирующий цикл AutoTune для входа (бар)
#           - max_adx=999,   # максимальный ADX для входа
# 11-05-26 prod12: добавил adx_period в params, чтобы оптимизировать период ADX;
#           имя версии prodXX теперь автоматически попадает в имя XLSX-файла.
# 13-05-26 prod14: добавлен явный режим торговли акциями/фьючерсами.
#           Для акций используется один непрерывный инструмент без futures-rollover,
#           отдельная StockCommission с шагом цены и stocklike-логикой,
#           long-only режим по умолчанию для stock-сценария.

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


# Класс для расчета комиссии при работе с акциями.
# ВАЖНО: moexcomm и brokercomm задаются десятичной долей оборота:
# 0.0003 = 0.03%, 0.0005 = 0.05%.
class StockCommission(bt.CommInfoBase):
    params = dict(
        moexcomm=0.0000,          # комиссия Биржи от оборота
        brokercomm=0.0005,        # комиссия брокера от оборота
        cost_of_price_step=0.01,  # стандартный шаг цены для рублёвых акций
        margin=None,              # акции покупаются на cash, без ГО как у фьючерсов
        mult=1.0,                 # 1 акция = 1 единица инструмента
        stocklike=True,           # явно указываем stock-like механику BackBroker
    )

    def _getcommission(self, size, price, pseudoexec):
        turnover = abs(size) * price
        return turnover * (self.p.moexcomm + self.p.brokercomm)


# Комиссии для акций. Если тикер не найден в словаре, используется DEFAULT_STOCK_COMMISSION.
DEFAULT_STOCK_COMMISSION = StockCommission(
    moexcomm=0.0000,
    brokercomm=0.0005,
    cost_of_price_step=0.01,
)

stock_comm = dict(
    SBER=DEFAULT_STOCK_COMMISSION,
    SBERP=DEFAULT_STOCK_COMMISSION,
    GAZP=DEFAULT_STOCK_COMMISSION,
    LKOH=DEFAULT_STOCK_COMMISSION,
    ROSN=DEFAULT_STOCK_COMMISSION,
    NVTK=DEFAULT_STOCK_COMMISSION,
    GMKN=DEFAULT_STOCK_COMMISSION,
    TATN=DEFAULT_STOCK_COMMISSION,
    TATNP=DEFAULT_STOCK_COMMISSION,
    MOEX=DEFAULT_STOCK_COMMISSION,
    YDEX=DEFAULT_STOCK_COMMISSION,
    VTBR=StockCommission(moexcomm=0.0000, brokercomm=0.0005, cost_of_price_step=0.000005),
)


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
                          margin=11779,  # ГО  05.12.2024 08-05-26
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
                          margin=4878.91 ,  # ГО 11-05-26
                          mult=1,  # мультипликатор
                          moexcomm=FUTURE_TYPE['stock'],
                          cost_of_price_step=1),
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
                          margin=4252.12 ,  # ГО 11-05-26
                          mult=0.83563 / 0.01,  # мультипликатор Стоимость шага цены/Шаг цены
                          moexcomm=FUTURE_TYPE['xindex'], cost_of_price_step=0.01))


def get_commission_info(sec, asset_type):
    """
    Возвращает объект комиссии для выбранного типа инструмента.

    asset_type='futures':
        sec — базовый код фьючерса из futures_comm, например MIX, Si, RTS.
    asset_type='stock':
        sec — тикер акции MOEX, например SBER, GAZP, LKOH.
        Если тикера нет в stock_comm, используется DEFAULT_STOCK_COMMISSION.
    """
    if asset_type == 'futures':
        if sec not in futures_comm:
            raise KeyError(f"Для фьючерса {sec!r} нет настроек комиссии/ГО в futures_comm")
        return futures_comm[sec]

    if asset_type == 'stock':
        return stock_comm.get(sec, DEFAULT_STOCK_COMMISSION)

    raise ValueError("asset_type должен быть 'futures' или 'stock'")


def load_market_datas(store, sec, asset_type, start_date, end_date, tf):
    """
    Загружает данные в едином формате для фьючерсов и акций.

    Для фьючерсов:
        берём список контрактов через store.futures.contracts_between(...)
        и прогоняем каждый контракт отдельно, как в предыдущих версиях.

    Для акций:
        берём один непрерывный инструмент sec за весь период.
        Никаких prevexpdate/expdate/rollover нет, потому что у акции нет экспирации.
    """
    datas = list()

    if asset_type == 'futures':
        contracts = store.futures.contracts_between(sec, start_date, end_date)
        print(contracts)

        for contract in contracts:
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
            data.asset_type = asset_type
            datas.append(data)

        return datas

    if asset_type == 'stock':
        print([sec])
        data = store.getdata(
            sec_id=sec,
            fromdate=pd.to_datetime(start_date),
            todate=end_date,
            tf=tf,
            name=sec,
        )
        data.sec = sec
        data.asset_type = asset_type
        datas.append(data)
        return datas

    raise ValueError("asset_type должен быть 'futures' или 'stock'")


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

    В версии prod12 были добавлены два дополнительных фильтра входа:
      * min_dc  — минимальный доминирующий цикл AutoTune. Сделки с dc < min_dc
                  не открываются (короткие циклы = шум, на нём mean-reversion
                  систематически проигрывает).
      * max_adx — максимальный ADX. Сделки при ADX > max_adx не
                  открываются (на сильном тренде возврат к среднему ломается).
    Нейтральные дефолты (min_dc=0, max_adx=999) воспроизводят прежнее
    поведение стратегии. В prod12 добавлен параметр adx_period для оптимизации
    периода ADX, а не только порога max_adx.
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
        # === Дополнительные фильтры на условие входа =========================
        # Нейтральные дефолты (фильтры выключены): min_dc=0, max_adx=999.
        # Чтобы активировать — задайте конкретные значения (например, 25 и 40
        # по результатам диагностического анализа).
        min_dc=0,      # минимальный доминирующий цикл AutoTune для входа (бар)
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

        # Базовые условия входа из статьи + два дополнительных фильтра:
        #   self.atf.dc >= self.p.min_dc   -> отсекаем короткие циклы (шум)
        # При нейтральных дефолтах (min_dc=0) условия истинны
        # всегда и поведение стратегии совпадает с предыдущей версией.
        self.long_signal = bt.And(
            self.cross_up,
            self.atf.mincorr < self.p.thresh,
            self.atf.dc >= self.p.min_dc,
        )

        self.short_signal = bt.And(
            self.cross_down,
            self.atf.mincorr < self.p.thresh,
            self.atf.filt > 0,
            self.atf.dc >= self.p.min_dc,
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
    start_cash = 300000.0

    # ---------------------------------------------------------------------
    # Выбор рынка
    # ---------------------------------------------------------------------
    # asset_type='futures' — старый режим: sec = базовый код фьючерса,
    #                        данные берутся отдельными контрактами.
    # asset_type='stock'   — режим акций: sec = тикер акции MOEX,
    #                        данные берутся одним непрерывным рядом.
    #
    # Примеры:
    #   futures: asset_type='futures', sec='MIX'
    #   stock  : asset_type='stock',   sec='SBER'
    # ---------------------------------------------------------------------
    asset_type = 'stock'  # 'futures' или 'stock'
    sec = 'SBER'          # для futures, например: 'MIX'; для stock, например: 'SBER'

    params = dict(  # базовый набор параметров
        write_history=False,
        risk=5,
        window=range(20, 66, 5),
        bandwidth=[i / 100 for i in range(15, 36, 5)], #,
        thresh=[-i / 100 for i in range(20, 61, 5)],  #-0.5,
        # Для акций по умолчанию оставляем long-only. Шорты по акциям в реальности
        # требуют маржинального режима и доступности бумаги в short-list брокера.
        allow_short=False if asset_type == 'stock' else True,
        printlog=False,
        tp_mult=[i / 10 for i in range(5, 26, 5)],
        min_dc=range(10,36,5), #25, #
    )

    tf = '1h'
    start_date = '2023-6-20'
    end_date = datetime.today()
    main_opt_metric = 'PROM'

    total_time = _time.time()
    store = MoexStore()

    datas = load_market_datas(
        store=store,
        sec=sec,
        asset_type=asset_type,
        start_date=start_date,
        end_date=end_date,
        tf=tf,
    )

    variants = count_param_variants(params)
    data_count = len(datas)
    data_count_label = 'контрактов' if asset_type == 'futures' else 'инструментов'

    sheet_size = (variants * data_count) > 1048576
    if sheet_size:
        print(
            f"Excel sheet is too large! Your sheet size is: {variants * data_count}, "
            f"Max sheet size is: 1'048'576"
        )

    print(
        f'Рассчитываем {variants} вариантов стратегии для каждого из '
        f'{data_count} {data_count_label}. Итого {variants * data_count} вариантов.'
    )
    print(f'Режим рынка: {asset_type}, инструмент: {sec}, таймфрейм: {tf}')
    print(f'Время пошло, {datetime.now():%H:%M:%S}')

    results = []
    trades = []
    analyzer_params = dict(it_params=iterable_params(params))

    for data in datas:
        analyzer_params['asset'] = data.sec
        st_time = _time.time()

        cerebro = bt.Cerebro()
        cerebro.broker = bt.brokers.BackBroker()
        cerebro.broker.setcash(start_cash)

        commission_info = get_commission_info(data.sec, asset_type)
        cerebro.broker.addcommissioninfo(commission_info, name=data.p.name)

        cerebro.addsizer(AllInSizer)
        cerebro.addanalyzer(SmartAnalyzer, _name='full', **analyzer_params)
        cerebro.adddata(data)

        cerebro.optstrategy(AutoTuneFilterStrategy, **params)
        runs = cerebro.run(
            stdstats=False,
            tradehistory=params["write_history"],
            maxcpus=maxcpus,
        )

        for run in runs:  # тут все варианты для одного инструмента/контракта
            for strategy in run:  # тут уникальные варианты по параметрам
                analyzer = strategy.analyzers.full
                analysis = dict()
                analysis.update(analyzer.get_analysis())
                analysis['Data'] = data.p.name
                analysis['PNLs'] = analyzer.get_trades_pnl()
                analysis['Asset'] = data.sec
                analysis['AssetType'] = asset_type
                results.append(analysis)

                if params['write_history']:
                    trades_data = analyzer.get_trades()
                    trades.extend(trades_data)

        print(
            f'Прогон {len(runs)} вариантов стратегии для '
            f'{asset_type} {data.p.name} за {round(_time.time() - st_time, 2)} сек., '
            f'{round((_time.time() - st_time) / 60, 2)} мин., '
            f'V (скорость) = {round(len(runs) / (_time.time() - st_time), 2)} вар/сек, '
            f'{str(datetime.now().time())[:5]}'
        )
        gc.collect()

    print(
        f'Весь прогон за {round(_time.time() - total_time, 2)} сек., '
        f'{round((_time.time() - total_time) / 3600, 2)} часов.'
    )

    df1 = pd.DataFrame(results).round(2)
    if params['write_history']:
        df2 = pd.DataFrame(trades).round(3)

    df3 = aggregate_df(df1, start_cash, sort_by=main_opt_metric)
    df4 = pd.DataFrame(
        list(params.items()) + [
            ('asset_type', asset_type),
            ('sec', sec),
            ('start_date', start_date),
            ('end_date', end_date),
        ],
        columns=['Parameter', 'Value']
    )

    del df1['PNLs']

    timestamp = datetime.now().strftime("%d-%m-%y %H-%M")

    # В имени файла теперь дополнительно есть asset_type, чтобы не путать
    # результаты фьючерсов и акций.
    results_file = f'opt_results_{SCRIPT_VERSION}_{asset_type}_{sec}_{tf}_{timestamp}.xlsx'

    with pd.ExcelWriter(results_file, engine='xlsxwriter') as writer:
        if not sheet_size:
            df1.to_excel(writer, sheet_name='by Contacts', index=False)
            if params['write_history']:
                df2.to_excel(writer, sheet_name='trades', index=False)
        df3.to_excel(writer, sheet_name='results', index=False)
        df4.to_excel(writer, sheet_name='params', index=False)

    print(f"Результаты успешно сохранены в файл '{results_file}'.")
    os.startfile(results_file)


if __name__ == '__main__':
    maxcpus = os.cpu_count()
    available_cpus = maxcpus - 2
    print(f'Задействуем {available_cpus} потоков из {maxcpus} возможных.')
    main(available_cpus)


# -------------------------------------------------------
'''
    params = dict( # MIX
        write_history=True,
        risk=5,
        window=45, #range(45,56, 5),   #28,  #range(48,53, 2),   #28,  #[48, 49, 50],  #range(16,57),  #30,
        bandwidth=0.25,  #[i / 100 for i in range(20, 32)], #[0.3, 0.35, 0.4], #[i / 100 for i in range(30, 56, 5)], #0.46,  #, #[0.4, 0.45, 0.45],[0.34, 0.35, 0.36],  # [i/100 for i in range(30, 51, 2)],  #[0.16, 0.24, 0.32, 0.4], # 0.22, #
        thresh=-0.5,  #[-i / 100 for i in range(40, 51, 2)],  #-0.5,  #[-i / 100 for i in range(25, 56, 5)],  #-0.68,  #-0.7,  #[-0.48, -0.49, 0.50],  #[-i/100 for i in range(42, 55, 2)],  #[-i / 12.5 for i in range(4, 9)],  #[0.32, 0.4, 0.48, 0.56, 0.64], #
        allow_short=True,
        printlog=False,
        tp_mult=[i / 10 for i in range(7, 16)],  #1.8,  #[i / 10 for i in range(15, 22, 3)],  #1.8,  #1.5,  #[1+i/10 for i in range(1,7)],   # тейк-профит в R
        min_dc=25,
    )
    
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
    
        params = dict(  # SIM6 - final 08-05-26 44-0.3--0.54-2.2 1 год ничего, 4 года - плохо
        write_history=True,
        risk=5,
        window=48,  #(43,44,45,47,48,49,50,51),
        bandwidth=0.26,  #[i / 100 for i in range(24, 39, 2)], #[0.4, 0.45, 0.45],
        thresh=-0.54,  #[-i / 100 for i in range(50, 59, 2)], #[-0.45, -0.5, -0.55],
        allow_short=True,
        tp_mult=2.2, #[i / 10 for i in range(20, 25)],  #[1.7, 1.8, 1.9, 2],
        printlog=False
    )
        
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
