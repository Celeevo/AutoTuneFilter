from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
import sys
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
from itertools import chain, product
from statistics import mean, stdev
import xlsxwriter
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from tqdm import tqdm
from additional_plotting import save_best_equity_dd_plot

_OPT_PBAR = None


def opt_progress_cb(_):
    """
    Callback для cerebro.optcallback.

    Важно:
    - функция должна быть объявлена на уровне модуля, не внутри main()
    - аргумент '_' — это результат одного завершённого варианта оптимизации
    - сам результат нам не нужен, мы только обновляем progress bar
    """
    global _OPT_PBAR

    if _OPT_PBAR is not None:
        _OPT_PBAR.update(1)


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
# 17-05-26 prod16: добавлен режим управления стартовым капиталом по контрактам:
#           capital_mode='fixed' — каждый контракт стартует с одинакового депозита;
#           capital_mode='cumulative' — следующий контракт стартует с финальной стоимости счёта
#           предыдущего контракта для той же комбинации параметров.
#           Добавлена опция close_on_expiration: если в день экспирации позиция
#           ещё открыта, стратегия закрывает её на заданном баре дня экспирации
#           и больше не открывает новые сделки в этот день.
#           Добавлены метрики максимальной просадки через bt.analyzers.DrawDown:
#           MaxDDPct, MaxDDMoney, MaxDDLen.
# 18-05-26 prod18-fix2: добавлен отдельный класс AutoTuneFilterEhlersStrategy,
#           но режим bracket запускается строго по исходному коду prod17.
# 19-05-26 prod19: исправлено логирование planned stop-loss/take-profit в trade book;
#           добавлены MaxClosedDDMoney / MaxClosedDDPct по закрытым сделкам;
#           входные настройки запуска перенесены вниз скрипта и передаются в main().
# 19-05-26 prod20: исправлен sizing входного bracket-ордера с учётом комиссии,
#           добавлены диагностические листы orders и signals.
# 19-05-26 prod21: добавлен режим instrument_type='futures'/'stocks'.
#           Скрипт умеет запускаться как по фьючерсным сериям MOEX, так и по акциям MOEX.
# 19-05-26 prod19-fix: исправлен MaxClosedDDPct и добавлен внешний модуль additional_plotting.py
#           для сохранения PNG-графика closed Equity/DD.

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
        # ВАЖНО: комиссии задаются десятичной долей, а не процентом.
        # Например, 0.03% = 0.0003.
        moexcomm=0.0003,
        brokercomm=0.0003,
        cost_of_price_step=0.01,
    )

    def _getcommission(self, size, price, pseudoexec):
        return abs(size) * price * (self.p.moexcomm + self.p.brokercomm)


stocks_comm = dict(
    # Универсальный дефолт для акций MOEX. При необходимости для конкретной
    # акции можно добавить отдельную запись с другим шагом цены или комиссией:
    # SBER=StockCommission(moexcomm=0.0003, brokercomm=0.0003, cost_of_price_step=0.01)
    DEFAULT=StockCommission(moexcomm=0.0003, brokercomm=0.0003, cost_of_price_step=0.01),
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


def expand_param_combinations(params_dict):
    '''
    Разворачивает словарь params в последовательность конкретных наборов параметров.

    Нужно для capital_mode='cumulative': в этом режиме нельзя запускать один общий
    optstrategy на контракт, потому что стартовый капитал следующего контракта
    должен быть своим для каждой комбинации параметров.
    '''
    keys = list(params_dict.keys())
    values = []

    for value in params_dict.values():
        if isinstance(value, (list, tuple, set, range)):
            values.append(list(value))
        else:
            values.append([value])

    for combo in product(*values):
        yield dict(zip(keys, combo))


def normalize_instrument_type(instrument_type):
    """Приводит тип инструмента к одному из значений: futures / stocks."""
    value = str(instrument_type or 'futures').lower().strip()

    if value in ('future', 'futures', 'fut'):
        return 'futures'

    if value in ('stock', 'stocks', 'share', 'shares', 'equity', 'equities'):
        return 'stocks'

    raise ValueError("instrument_type должен быть 'futures' или 'stocks'")


def as_list(value):
    """Позволяет передавать sec как строку или как список тикеров."""
    if isinstance(value, (list, tuple, set)):
        return list(value)

    return [value]


def get_commission_info(sec, instrument_type, settings=None):
    """Возвращает commission-info для фьючерса или акции."""
    instrument_type = normalize_instrument_type(instrument_type)
    settings = settings or {}

    if instrument_type == 'futures':
        if sec not in futures_comm:
            raise KeyError(
                f"Для фьючерса '{sec}' нет записи в futures_comm. "
                f"Добавьте ГО, мультипликатор, биржевую комиссию и шаг цены."
            )
        return futures_comm[sec]

    if sec in stocks_comm:
        return stocks_comm[sec]

    return StockCommission(
        moexcomm=float(settings.get('stock_moexcomm', stocks_comm['DEFAULT'].p.moexcomm)),
        brokercomm=float(settings.get('stock_brokercomm', stocks_comm['DEFAULT'].p.brokercomm)),
        cost_of_price_step=float(settings.get('stock_price_step', stocks_comm['DEFAULT'].p.cost_of_price_step)),
    )


def load_moex_datas(store, sec, instrument_type, start_date, end_date, tf):
    """
    Загружает данные MOEX.

    futures:
        sec = базовый код фьючерса, например SPYF / RTS / MIX.
        Скрипт сам находит серии через store.futures.contracts_between().

    stocks:
        sec = тикер акции или список тикеров, например 'SBER' или ['SBER', 'GAZP'].
        Данные загружаются напрямую, без логики контрактов и экспираций.
    """
    instrument_type = normalize_instrument_type(instrument_type)
    datas = []

    if instrument_type == 'futures':
        contracts = store.futures.contracts_between(sec, start_date, end_date)
        print(contracts)

        for contract in contracts:
            prevexpdate = pd.to_datetime(store.futures.prevexpdate(contract))

            if contract == contracts[0]:
                fromdate = pd.to_datetime(start_date) - timedelta(days=5)
            else:
                fromdate = prevexpdate - timedelta(days=5)

            contract_expdate = pd.to_datetime(store.futures.expdate(contract)).date()

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
            data.contract_expdate = contract_expdate
            datas.append(data)

        return datas, contracts

    stock_tickers = as_list(sec)
    print(stock_tickers)

    for ticker in stock_tickers:
        data = store.getdata(
            sec_id=ticker,
            fromdate=start_date,
            todate=end_date,
            tf=tf,
            name=ticker,
        )

        data.sec = ticker
        data.contract_expdate = None
        datas.append(data)

    return datas, stock_tickers


def calc_max_drawdown_from_values(values):
    """
    Рассчитывает максимальную просадку по последовательности значений капитала.

    Используется для MaxClosedDD*: это просадка только по закрытым сделкам,
    т.е. по последовательности cash_after после закрытия сделок.
    Она не включает внутрисделочную плавающую просадку открытой позиции.

    Важно: максимальная денежная просадка и максимальная процентная просадка
    считаются независимо. Они могут возникать в разных точках equity-кривой.
    """
    clean_values = []

    for value in values or []:
        if value is None:
            continue

        try:
            value = float(value)
        except (TypeError, ValueError):
            continue

        if not np.isfinite(value):
            continue

        clean_values.append(value)

    if not clean_values:
        return 0.0, 0.0, 0

    peak = clean_values[0]
    peak_index = 0

    max_dd_money = 0.0
    max_dd_money_len = 0

    max_dd_pct = 0.0
    max_dd_pct_len = 0

    for index, value in enumerate(clean_values):
        if value > peak:
            peak = value
            peak_index = index
            continue

        dd_money = peak - value
        dd_pct = dd_money / peak * 100.0 if peak else 0.0
        dd_len = index - peak_index

        if dd_money > max_dd_money:
            max_dd_money = dd_money
            max_dd_money_len = dd_len

        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            max_dd_pct_len = dd_len

    max_dd_len = max(max_dd_money_len, max_dd_pct_len)

    return max_dd_pct, max_dd_money, max_dd_len

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

    max_dd_pct = group['MaxDDPct'].max() if 'MaxDDPct' in group.columns else 0
    max_dd_money = group['MaxDDMoney'].max() if 'MaxDDMoney' in group.columns else 0
    max_dd_len = group['MaxDDLen'].max() if 'MaxDDLen' in group.columns else 0

    max_closed_dd_pct = group['MaxClosedDDPct'].max() if 'MaxClosedDDPct' in group.columns else 0
    max_closed_dd_money = group['MaxClosedDDMoney'].max() if 'MaxClosedDDMoney' in group.columns else 0

    # В cumulative-режиме логичнее считать закрытую просадку по всей
    # последовательности контрактов одного набора параметров. Для этого
    # склеиваем ClosedEquity из строк группы в один ряд.
    if 'ClosedEquity' in group.columns:
        modes = set(str(x).lower() for x in group.get('CapitalMode', pd.Series(dtype=str)).dropna())
        if 'cumulative' in modes:
            closed_equity = []
            for series in group['ClosedEquity']:
                if not isinstance(series, (list, tuple)) or not series:
                    continue
                series = [float(x) for x in series]
                if not closed_equity:
                    closed_equity.extend(series)
                else:
                    # Если первый элемент нового фрагмента совпадает с последним
                    # элементом предыдущего, не дублируем его.
                    if abs(closed_equity[-1] - series[0]) < 0.01:
                        closed_equity.extend(series[1:])
                    else:
                        closed_equity.extend(series)

            if closed_equity:
                max_closed_dd_pct, max_closed_dd_money, _ = calc_max_drawdown_from_values(closed_equity)

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
        'MaxDDPct': max_dd_pct,
        'MaxDDMoney': max_dd_money,
        'MaxDDLen': max_dd_len,
        'MaxClosedDDMoney': max_closed_dd_money,
        'MaxClosedDDPct': max_closed_dd_pct,
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
    metric_cols = ['PNLs', 'PNL', 'Asset']
    for col in (
        'MaxDDPct', 'MaxDDMoney', 'MaxDDLen',
        'MaxClosedDDMoney', 'MaxClosedDDPct',
        'ClosedEquity', 'CapitalMode',
    ):
        if col in df.columns:
            metric_cols.append(col)

    aggr = df.groupby(first_col, sort=False)[metric_cols].apply(compute_group_metrics, startingcash=startingcash).reset_index()
    aggr = aggr.sort_values(sort_by, ascending=False)
    # Проверка значения в столбце 's-Pardo' и вторичная сортировка при необходимости
    if 's-Pardo' in aggr.columns and aggr['s-Pardo'].iloc[0] <= 0:
        aggr = aggr.sort_values(sort_by_second, ascending=False)
    return aggr.round(2)


def add_drawdown_metrics(strategy, analysis):
    """
    Добавляет в строку результата метрики просадки из bt.analyzers.DrawDown.

    Важно: DrawDown считается внутри конкретного прогона Cerebro.
    В fixed-режиме это просадка отдельного контракта. В cumulative-режиме это
    просадка внутри очередного контракта с учётом стартового капитала, который
    был перенесён с предыдущего контракта. Это не сквозная equity-curve DD по
    всей цепочке контрактов.
    """
    dd = {}

    try:
        dd = strategy.analyzers.dd.get_analysis()
    except Exception:
        dd = {}

    max_dd = dd.get('max', {}) if isinstance(dd, dict) else {}

    analysis['MaxDDPct'] = max_dd.get('drawdown', 0.0)
    analysis['MaxDDMoney'] = max_dd.get('moneydown', 0.0)
    analysis['MaxDDLen'] = max_dd.get('len', 0)


class SmartAnalyzer(Analyzer):
    """
    Возвращает основные метрики стратегии, trade-book и диагностические журналы.

    В prod20 добавлены:
    - orders: журнал финальных статусов ордеров, включая Margin/Rejected/Canceled;
    - signals: журнал сигналов стратегии и причин, почему сигнал стал/не стал ордером.
    """

    params = dict(it_params=None, asset=None)

    def __init__(self):
        # Списки результатов прибыльных и убыточных сделок
        self.pt_arr = list()
        self.lt_arr = list()
        self.trades = list()
        self.trades_details = list()
        self.orders_details = list()
        self.signals_details = list()
        self.depos = self.strategy.broker.startingcash
        self.closed_equity = [float(self.depos)]

    def _params_str(self):
        st_params = self.strategy.p._getkwargs()
        return '-'.join(str(v) for k, v in st_params.items() if k in self.p.it_params) + '-' + self.p.asset

    @staticmethod
    def _fmt_dt(value, fmt='%d.%m.%y %H:%M'):
        try:
            if value is None or value == 0:
                return ''
            return f'{bt.num2date(value):{fmt}}'
        except Exception:
            return ''

    @staticmethod
    def _safe_float(value, default=0.0):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return default

        if not np.isfinite(value):
            return default

        return value

    def _order_price_for_estimate(self, order):
        price = self._safe_float(getattr(order.created, 'price', 0.0), 0.0)

        if price <= 0:
            price = self._safe_float(getattr(order.executed, 'price', 0.0), 0.0)

        if price <= 0:
            try:
                price = self._safe_float(order.data.close[0], 0.0)
            except Exception:
                price = 0.0

        return price

    def _order_cost_estimate(self, order):
        """
        Оценивает деньги, которые могли потребоваться на ордер:
        margin/cost + комиссия.

        Это диагностическая оценка для листа orders. Для bracket child-ордеров
        оценка не является требованием к свободному cash, потому что они OCO-выходы.
        Главная цель — быстро увидеть, почему входной ордер мог получить Margin.
        """
        data = getattr(order, 'data', None)
        if data is None:
            return 0.0, 0.0, 0.0, 0.0

        try:
            comminfo = self.strategy.broker.getcommissioninfo(data)
        except Exception:
            return 0.0, 0.0, 0.0, 0.0

        size = abs(self._safe_float(getattr(order.created, 'size', 0.0), 0.0))
        if size <= 0:
            size = abs(self._safe_float(getattr(order, 'size', 0.0), 0.0))

        price = self._order_price_for_estimate(order)

        try:
            margin = self._safe_float(comminfo.p.margin, 0.0)
        except Exception:
            margin = 0.0

        if margin:
            estimated_margin = size * margin
        else:
            estimated_margin = size * price

        try:
            estimated_commission = self._safe_float(comminfo.getcommission(size, price), 0.0)
        except Exception:
            try:
                estimated_commission = self._safe_float(comminfo._getcommission(size, price, True), 0.0)
            except Exception:
                estimated_commission = 0.0

        estimated_total = estimated_margin + estimated_commission
        cash_after_estimate = self._safe_float(self.strategy.broker.getcash(), 0.0) - estimated_total

        return estimated_margin, estimated_commission, estimated_total, cash_after_estimate

    def notify_order(self, order):
        # Submitted/Accepted обычно только шумят журнал. Для диагностики нам
        # важны финальные статусы: Completed, Canceled, Margin, Rejected, Expired.
        if not self.strategy.p.write_history:
            return

        if order.status in [order.Submitted, order.Accepted]:
            return

        order_name = getattr(order.info, 'name', '')
        parent = getattr(order, 'parent', None)
        parent_ref = getattr(parent, 'ref', '') if parent is not None else ''

        estimated_margin, estimated_commission, estimated_total, cash_after_estimate = self._order_cost_estimate(order)

        try:
            current_dt = self.strategy.data.datetime.datetime(0)
            current_dt_str = f'{current_dt:%d.%m.%y %H:%M}'
        except Exception:
            current_dt_str = ''

        try:
            position_size = self.strategy.getposition(order.data).size
        except Exception:
            position_size = 0

        data_name = ''
        try:
            data_name = order.data.p.name
        except Exception:
            pass

        order_row = dict(
            params=self._params_str(),
            data=data_name,
            sec=self.p.asset,
            current_dt=current_dt_str,
            order_ref=order.ref,
            parent_ref=parent_ref,
            order_name=order_name,
            status=order.getstatusname(),
            isbuy=1 if order.isbuy() else 0,
            size=getattr(order.created, 'size', getattr(order, 'size', 0)),
            created_date=self._fmt_dt(getattr(order.created, 'dt', 0), '%d.%m.%y'),
            created_time=self._fmt_dt(getattr(order.created, 'dt', 0), '%H:%M'),
            created_price=getattr(order.created, 'price', 0),
            executed_date=self._fmt_dt(getattr(order.executed, 'dt', 0), '%d.%m.%y'),
            executed_time=self._fmt_dt(getattr(order.executed, 'dt', 0), '%H:%M'),
            executed_price=getattr(order.executed, 'price', 0),
            executed_size=getattr(order.executed, 'size', 0),
            executed_value=getattr(order.executed, 'value', 0),
            executed_comm=getattr(order.executed, 'comm', 0),
            cash=self.strategy.broker.getcash(),
            value=self.strategy.broker.getvalue(),
            position_size=position_size,
            estimated_margin=estimated_margin,
            estimated_commission=estimated_commission,
            estimated_total=estimated_total,
            cash_after_estimate=cash_after_estimate,
        )

        self.orders_details.append(order_row)

    def notify_trade(self, trade):
        if trade.isclosed:
            if trade.pnlcomm >= 0:
                self.pt_arr.append(trade.pnlcomm)
            else:
                self.lt_arr.append(trade.pnlcomm)

            finish_cash = float(self.strategy.broker.getcash())
            self.closed_equity.append(finish_cash)

            # Section for trade-book
            if self.strategy.p.write_history:
                trade.start_cash = self.depos
                trade.finish_cash = finish_cash
                trade.stop_loss_price = self.strategy.stop_loss_price
                trade.take_profit_price = self.strategy.take_profit_price
                trade.sec_id = trade.getdataname()
                self.trades.append(trade)

            self.depos = finish_cash

    def stop(self):
        """
        В self.strategy.p.itp хранятся имена параметров params, которые оптимизируются (итераторы):
        - Формируем заголовок первой колонки params_head из этих имен, разделенных "-".
        - Формируем ячейки (строки) первой колонки params_values из значений имен итерируемых параметров,
        полученных из self.strategy.p._getkwargs() для этого конкретного экземпляра стратегии. Значения
        также разделяются "-".
        """
        st_params = self.strategy.p._getkwargs()
        params_head = '-'.join(str(k) for k in st_params.keys() if k in self.p.it_params) + '-asset'
        params_str = self._params_str()

        wt = len(self.pt_arr)  # win_total
        aw = mean(self.pt_arr) if wt else 0  # avr_win
        lt = len(self.lt_arr)  # loss_total
        al = mean(self.lt_arr) if lt else 0  # avr_loss
        swt = sum(self.pt_arr)  # sum profit trades
        slt = sum(self.lt_arr)  # sum losing trades
        pnl = int(sum(self.pt_arr + self.lt_arr))
        start_cash = float(getattr(self.strategy.broker, 'startingcash', self.depos))
        end_cash = float(self.strategy.broker.getcash())
        end_value = float(self.strategy.broker.getvalue())

        self.rets[params_head] = params_str
        max_closed_dd_pct, max_closed_dd_money, _ = calc_max_drawdown_from_values(self.closed_equity)

        self.rets['StartCash'] = start_cash
        self.rets['EndCash'] = end_cash
        self.rets['EndValue'] = end_value
        self.rets['ContractPNL'] = end_value - start_cash
        self.rets['MaxClosedDDMoney'] = max_closed_dd_money
        self.rets['MaxClosedDDPct'] = max_closed_dd_pct
        self.rets['ClosedEquity'] = list(self.closed_equity)
        self.rets['PNL'] = pnl
        self.rets['WinTr'] = wt
        self.rets['LossTr'] = lt
        self.rets['SumWin'] = swt
        self.rets['SumLoss'] = slt
        self.rets['AvgWin'] = aw
        self.rets['AvgLoss'] = al

        if self.strategy.p.write_history:

            for trade in self.trades:
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
                    stop_loss_price=getattr(
                        entry_order.info,
                        'planned_stop_loss_price',
                        getattr(trade, 'stop_loss_price', 0),
                    ),
                    take_profit_price=getattr(
                        entry_order.info,
                        'planned_take_profit_price',
                        getattr(trade, 'take_profit_price', 0),
                    ),
                    planned_risk_points=getattr(entry_order.info, 'planned_risk_points', 0),
                    size=entry_order.size,
                    entry_type='long' if entry_order.isbuy() else 'short',

                    exit_ref=exit_order.ref,
                    exit_created_date=f'{bt.num2date(exit_order.created.dt):%d.%m.%y}',
                    exit_created_time=f'{bt.num2date(exit_order.created.dt):%H:%M}',
                    exit_executed_date=f'{bt.num2date(exit_order.executed.dt):%d.%m.%y}',
                    exit_executed_time=f'{bt.num2date(exit_order.executed.dt):%H:%M}',
                    exit_requested_price=exit_order.created.price,
                    exit_executed_price=exit_order.executed.price,
                    exit_type=getattr(exit_order.info, 'name', ''),

                    result=1 if trade.pnl > 0 else 0,
                    pnl=int(trade.pnlcomm),

                    cash_before=getattr(trade, 'start_cash', 0),
                    cash_after=getattr(trade, 'finish_cash', 0),
                )
                self.trades_details.append(tr)

            for signal in getattr(self.strategy, 'signal_log', []):
                signal_row = dict(signal)
                signal_row['params'] = params_str
                signal_row['sec'] = self.p.asset
                self.signals_details.append(signal_row)

    def get_trades(self):
        return self.trades_details

    def get_orders(self):
        return self.orders_details

    def get_signals(self):
        return self.signals_details

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
        close_on_expiration=True,  # закрывать открытую позицию в день экспирации контракта
        expiration_exit_bar=3,     # номер бара дня экспирации, на котором отправляем close()
        contract_expdate=None,     # дата экспирации текущего контракта; передаётся из main()
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
        self.expiration_close_order = None

        # Счётчик баров внутри текущего календарного дня нужен для выхода
        # на N-м баре дня экспирации. Считаем именно бары, которые реально
        # пришли из data feed, а не абстрактные часы торговой сессии.
        self._current_session_date = None
        self._session_bar_no = 0

        # Диагностический журнал сигналов. Заполняется только если write_history=True.
        self.signal_log = []

    def _reset_bracket_state(self):
        self.order = None
        self.stop_order = None
        self.take_profit_order = None
        self.expiration_close_order = None
        self.stop_loss_price = 0.0
        self.take_profit_price = 0.0
        self.entry_price = 0.0

    def _has_active_orders(self):
        return any(
            order is not None and order.alive()
            for order in (self.order, self.stop_order, self.take_profit_order, self.expiration_close_order)
        )

    def _round_exit_price(self, comminfo, price, isbuy):
        if comminfo.p.cost_of_price_step != 0:
            return round_to_nearest_price_step(comminfo.p.cost_of_price_step, price, isbuy)
        return price

    @staticmethod
    def _safe_line_value(line, default=0.0):
        try:
            value = float(line[0])
        except Exception:
            return default

        if not np.isfinite(value):
            return default

        return value

    def _record_signal(self, decision, long_signal=False, short_signal=False):
        """
        Пишет диагностическую строку по сигналу.

        Журнал нужен для случаев: сигнал был, но сделка не появилась.
        Например: blocked_by_position, blocked_by_active_order, blocked_by_expiration,
        blocked_by_size_or_cash.
        """
        if not self.p.write_history:
            return

        if not long_signal and not short_signal and not decision:
            return

        try:
            dt = self.data.datetime.datetime(0)
            date_str = f'{dt:%d.%m.%y}'
            time_str = f'{dt:%H:%M}'
        except Exception:
            date_str = ''
            time_str = ''

        self.signal_log.append(dict(
            data=self.data.p.name,
            date=date_str,
            time=time_str,
            decision=decision,
            long_signal=1 if long_signal else 0,
            short_signal=1 if short_signal else 0,
            close=self.data.close[0],
            roc=self._safe_line_value(self.roc),
            bp=self._safe_line_value(self.atf.bp),
            filt=self._safe_line_value(self.atf.filt),
            mincorr=self._safe_line_value(self.atf.mincorr),
            dc=self._safe_line_value(self.atf.dc),
            cash=self.broker.getcash(),
            value=self.broker.getvalue(),
            position_size=self.position.size,
            has_active_orders=1 if self._has_active_orders() else 0,
            is_expiration_day=1 if self._is_expiration_day() else 0,
            session_bar_no=self._session_bar_no,
        ))

    def _estimate_entry_cost(self, comminfo, price, size):
        """Оценивает требования к cash для входного рыночного ордера."""
        size = abs(int(size))

        if size <= 0:
            return 0.0, 0.0, 0.0

        if comminfo.p.margin:
            margin_required = size * comminfo.p.margin
        else:
            margin_required = size * price

        try:
            commission = comminfo.getcommission(size, price)
        except Exception:
            commission = comminfo._getcommission(size, price, True)

        total_required = margin_required + commission

        return margin_required, commission, total_required

    def _fit_size_to_cash(self, comminfo, cash, price, size):
        """
        Уменьшает size так, чтобы хватало денег не только на ГО/стоимость,
        но и на комиссию входного ордера.

        В предыдущей версии size = int(cash / margin) - 1 мог не хватать,
        потому что один контракт запаса покрывал ГО, но не всегда покрывал
        комиссию входа при большом размере позиции.
        """
        size = int(size)

        while size > 0:
            _, _, total_required = self._estimate_entry_cost(comminfo, price, size)

            if total_required <= cash:
                return size

            size -= 1

        return 0

    def _contract_expdate(self):
        """Возвращает дату экспирации текущего контракта как date или None."""
        expdate = self.p.contract_expdate

        if expdate is None:
            return None

        return pd.to_datetime(expdate).date()

    def _update_session_bar_no(self):
        """Считает номер бара внутри текущего календарного дня."""
        current_date = self.data.datetime.date(0)

        if current_date != self._current_session_date:
            self._current_session_date = current_date
            self._session_bar_no = 1
        else:
            self._session_bar_no += 1

    def _is_expiration_day(self):
        expdate = self._contract_expdate()
        return expdate is not None and self.data.datetime.date(0) == expdate

    def _is_after_expiration_exit_bar(self):
        return (
            self.p.close_on_expiration
            and self._is_expiration_day()
            and self._session_bar_no >= int(self.p.expiration_exit_bar)
        )

    def _cancel_bracket_children(self):
        """Отменяет защитные bracket-ордера перед принудительным закрытием."""
        for order in (self.stop_order, self.take_profit_order):
            if order is not None and order.alive():
                self.cancel(order)

        self.stop_order = None
        self.take_profit_order = None

    def _submit_expiration_close(self):
        """Закрывает открытую позицию на заданном баре дня экспирации."""
        if not self.position:
            return

        if self.expiration_close_order is not None and self.expiration_close_order.alive():
            return

        self._cancel_bracket_children()
        self.log(
            f'EXPIRATION EXIT -> close() on bar '
            f'{self._session_bar_no} of {self.data.datetime.date(0)}'
        )
        self.expiration_close_order = self.close(name='expiration_close')

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
        size = self._fit_size_to_cash(
            comminfo=comminfo,
            cash=cash,
            price=self.entry_price,
            size=size,
        )

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
            return False

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
            oargs={
                'name': side,
                'planned_stop_loss_price': self.stop_loss_price,
                'planned_take_profit_price': self.take_profit_price,
                'planned_risk_points': abs(self.entry_price - self.stop_loss_price),
            },
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

        return True

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
            elif order_name == 'expiration_close':
                self.log(f'EXPIRATION CLOSE EXECUTED at {order.executed.price:.2f}')
                self._reset_bracket_state()

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log(f'ORDER {order_name} FAILED: {order.getstatusname()}')

            if order_name in ('long', 'short'):
                self._reset_bracket_state()
            elif order_name == 'stop_loss':
                self.stop_order = None
            elif order_name == 'take_profit':
                self.take_profit_order = None
            elif order_name == 'expiration_close':
                self.expiration_close_order = None

    def next(self):
        self._update_session_bar_no()

        long_now = bool(self.long_signal[0])
        short_now = bool(self.short_signal[0])

        # В день экспирации не открываем новые сделки. Если позиция была
        # перенесена в этот день, на заданном баре отправляем рыночный close().
        # В обычном режиме Backtrader такой close() исполнится на следующем баре.
        if self.p.close_on_expiration and self._is_expiration_day():
            if long_now or short_now:
                self._record_signal(
                    decision='blocked_by_expiration',
                    long_signal=long_now,
                    short_signal=short_now,
                )

            if self._is_after_expiration_exit_bar():
                self._submit_expiration_close()
            return

        if self.position or self._has_active_orders():
            if long_now or short_now:
                decision = 'blocked_by_position' if self.position else 'blocked_by_active_order'
                self._record_signal(
                    decision=decision,
                    long_signal=long_now,
                    short_signal=short_now,
                )
            return

        if long_now:
            submitted = self._submit_bracket(isbuy=True)
            self._record_signal(
                decision='submit_long' if submitted else 'blocked_by_size_or_cash',
                long_signal=long_now,
                short_signal=short_now,
            )
        elif short_now:
            if self.p.allow_short:
                submitted = self._submit_bracket(isbuy=False)
                self._record_signal(
                    decision='submit_short' if submitted else 'blocked_by_size_or_cash',
                    long_signal=long_now,
                    short_signal=short_now,
                )
            else:
                self._record_signal(
                    decision='blocked_short_disabled',
                    long_signal=long_now,
                    short_signal=short_now,
                )


class AutoTuneFilterEhlersStrategy(AutoTuneFilterStrategy):
    """
    Вариант стратегии с выходом/разворотом по оригинальной логике Эйлерса.

    Базовый класс AutoTuneFilterStrategy в prod17 оставлен для bracket-режима
    без изменения логики. Этот отдельный класс нужен, чтобы режим bracket давал
    те же результаты, что и prod17, а эксперимент с выходом по Эйлерсу не влиял
    на текущий рабочий алгоритм.
    """

    def _calc_ehlers_target_size(self):
        """
        Рассчитывает целевой размер позиции для always-in-the-market логики.

        Здесь нет SL/TP bracket-ордера. При обратном сигнале стратегия должна
        перейти к противоположной позиции. Размер считаем от текущей стоимости
        счёта, а не только от свободного cash, потому что при развороте часть
        средств уже занята маржой открытой позиции.
        """
        comminfo = self.broker.getcommissioninfo(self.data)
        account_value = self.broker.getvalue()
        self.entry_price = self.data.close[0]

        if self.entry_price <= 0:
            return 0

        if comminfo.p.margin:
            max_size = account_value / comminfo.p.margin
        else:
            max_size = account_value / self.entry_price

        size = int(max_size) - 1
        return max(size, 0)

    def _submit_ehlers_target(self, isbuy):
        """
        Отправляет ордер к целевой позиции по логике Эйлерса.

        Long signal  -> целевая позиция +size.
        Short signal -> целевая позиция -size.

        Если уже открыта противоположная позиция, один рыночный ордер закрывает
        текущую позицию и открывает новую в другую сторону.
        """
        if self.position.size > 0 and isbuy:
            return
        if self.position.size < 0 and not isbuy:
            return

        target_size = self._calc_ehlers_target_size()
        if target_size <= 0:
            return

        target_position = target_size if isbuy else -target_size
        delta = target_position - self.position.size

        if delta == 0:
            return

        side = 'long' if delta > 0 else 'short'
        action = 'BUY' if delta > 0 else 'SELL'
        size = abs(delta)

        self.log(
            f'EHLERS {action} SIGNAL -> target={target_position}, '
            f'current={self.position.size}, order_size={size}'
        )

        if delta > 0:
            self.order = self.buy(size=size, name=side)
        else:
            self.order = self.sell(size=size, name=side)

    def _submit_ehlers_close(self):
        """Закрывает позицию по обратному сигналу без разворота в short."""
        if not self.position:
            return
        if self.order is not None and self.order.alive():
            return

        self.log('EHLERS EXIT -> close()')
        self.order = self.close(name='ehlers_exit')

    def notify_order(self, order):
        super().notify_order(order)

        if order.status in [order.Submitted, order.Accepted]:
            return

        order_name = getattr(order.info, 'name', None)

        if order_name == 'ehlers_exit':
            if order.status == order.Completed:
                self.log(f'EHLERS EXIT EXECUTED at {order.executed.price:.2f}')
            self.order = None

    def next(self):
        self._update_session_bar_no()

        # В день экспирации не открываем новые сделки. Если позиция была
        # перенесена в этот день, на заданном баре отправляем рыночный close().
        if self.p.close_on_expiration and self._is_expiration_day():
            if self._is_after_expiration_exit_bar():
                self._submit_expiration_close()
            return

        if self._has_active_orders():
            return

        if not self.position:
            if self.long_signal[0]:
                self._submit_ehlers_target(isbuy=True)
            elif self.p.allow_short and self.short_signal[0]:
                self._submit_ehlers_target(isbuy=False)

        elif self.position.size > 0:
            if self.short_signal[0]:
                if self.p.allow_short:
                    self._submit_ehlers_target(isbuy=False)
                else:
                    self._submit_ehlers_close()

        elif self.position.size < 0:
            if self.long_signal[0]:
                self._submit_ehlers_target(isbuy=True)


def main(maxcpus=None, settings=None):
    # Фильтр AutoTune https://financial-hacker.com/the-autotune-filter/
    global _OPT_PBAR

    if settings is None:
        raise ValueError('settings must be provided')

    start_cash = float(settings.get('start_cash', 300000.0))
    instrument_type = normalize_instrument_type(settings.get('instrument_type', 'futures'))
    capital_mode = settings.get('capital_mode', 'fixed')
    exit_mode = settings.get('exit_mode', 'bracket')
    close_on_expiration = bool(settings.get('close_on_expiration', True))
    expiration_exit_bar = int(settings.get('expiration_exit_bar', 3))

    params = dict(settings['params'])

    tf = settings.get('tf', '1h')
    start_date = settings.get('start_date', '2023-6-20')
    end_date = settings.get('end_date') or datetime.today()
    main_opt_metric = settings.get('main_opt_metric', 'PROM')
    sec = settings.get('sec', 'SPYF')
    save_equity_dd_plot = bool(settings.get('save_equity_dd_plot', True))
    equity_dd_plot_freq = settings.get('equity_dd_plot_freq', 'M')

    total_time = _time.time()
    store = MoexStore()

    datas, loaded_items = load_moex_datas(
        store=store,
        sec=sec,
        instrument_type=instrument_type,
        start_date=start_date,
        end_date=end_date,
        tf=tf,
    )

    variants = count_param_variants(params)

    sheet_size = (variants * len(datas)) > 1048576
    if sheet_size:
        print(f"Excel sheet is too large! Your sheet size is: {variants * len(datas)}, Max sheet size is: 1'048'576")

    item_name = 'контрактов' if instrument_type == 'futures' else 'инструментов'
    print(f'Рассчитываем {variants} вариантов стратегии для '
          f'каждого из {len(datas)} {item_name}. Итого '
          f'{variants * len(datas)} вариантов.')
    print(f'Время пошло, {datetime.now():%H:%M:%S}')

    results = []
    trades = []
    orders = []
    signals = []
    analyzer_params = dict(it_params=iterable_params(params))

    if capital_mode not in ('fixed', 'cumulative'):
        raise ValueError("capital_mode должен быть 'fixed' или 'cumulative'")

    exit_mode = str(exit_mode).lower()
    if exit_mode not in ('bracket', 'ehlers'):
        raise ValueError("exit_mode должен быть 'bracket' или 'ehlers'")

    if capital_mode == 'fixed':
        # Старый режим: каждый контракт тестируется независимо и стартует
        # с одинакового депозита start_cash.
        for data in datas:
            analyzer_params['asset'] = data.sec
            st_time = _time.time()
            cerebro = bt.Cerebro()
            cerebro.broker = bt.brokers.BackBroker()
            cerebro.broker.setcash(start_cash)
            cerebro.broker.addcommissioninfo(get_commission_info(data.sec, instrument_type, settings), name=data.p.name)
            cerebro.addanalyzer(SmartAnalyzer, _name='full', **analyzer_params)
            cerebro.addanalyzer(bt.analyzers.DrawDown, _name='dd')
            cerebro.adddata(data)

            strategy_params = dict(
                params,
                close_on_expiration=close_on_expiration,
                expiration_exit_bar=expiration_exit_bar,
                contract_expdate=data.contract_expdate,
            )
            if exit_mode == 'bracket':
                cerebro.optstrategy(AutoTuneFilterStrategy, **strategy_params)
            else:
                cerebro.optstrategy(AutoTuneFilterEhlersStrategy, **strategy_params)
            if tqdm is not None:
                _OPT_PBAR = tqdm(
                    total=variants,
                    desc=data.p.name,
                    dynamic_ncols=True,
                    unit='var',
                    file=sys.stdout,
                )
                cerebro.optcallback(opt_progress_cb)

            runs = cerebro.run(stdstats=False, tradehistory=params["write_history"], maxcpus=maxcpus)

            if _OPT_PBAR is not None:
                _OPT_PBAR.close()
                _OPT_PBAR = None

            for run in runs:  # тут все варианты для одного контракта
                for strategy in run:  # тут уникальные варианты по параметрам
                    analyzer = strategy.analyzers.full
                    analysis = dict()
                    analysis.update(analyzer.get_analysis())
                    add_drawdown_metrics(strategy, analysis)
                    analysis['Data'] = data.p.name
                    analysis['PNLs'] = analyzer.get_trades_pnl()
                    analysis['Asset'] = data.sec
                    analysis['CapitalMode'] = capital_mode
                    results.append(analysis)

                    if params['write_history']:
                        trades_data = analyzer.get_trades()
                        for tr in trades_data:
                            tr['capital_mode'] = capital_mode
                        trades.extend(trades_data)

                        orders_data = analyzer.get_orders()
                        for order_row in orders_data:
                            order_row['capital_mode'] = capital_mode
                        orders.extend(orders_data)

                        signals_data = analyzer.get_signals()
                        for signal_row in signals_data:
                            signal_row['capital_mode'] = capital_mode
                        signals.extend(signals_data)

            print(
                f'Прогон {len(runs)} вариантов стратегии для контракта '
                f'{data.p.name} за {round(_time.time() - st_time, 2)} сек., '
                f'{round((_time.time() - st_time) / 60, 2)} мин., '
                f'V (скорость) = {round(len(runs) / (_time.time() - st_time), 2)} вар/сек, '
                f'{str(datetime.now().time())[:5]}'
            )
            gc.collect()

    else:
        # Кумулятивный режим: одна комбинация параметров последовательно проходит
        # все контракты. Финальная стоимость счёта после контракта N становится
        # стартовым капиталом для контракта N+1 той же комбинации параметров.
        param_variants = list(expand_param_combinations(params))
        total_runs = len(param_variants) * len(datas)

        if tqdm is not None:
            _OPT_PBAR = tqdm(
                total=total_runs,
                desc='cumulative',
                dynamic_ncols=True,
                unit='run',
                file=sys.stdout,
            )

        for variant_no, strategy_params in enumerate(param_variants, start=1):
            current_cash = start_cash
            variant_time = _time.time()

            for data in datas:
                analyzer_params['asset'] = data.sec
                st_time = _time.time()

                cerebro = bt.Cerebro()
                cerebro.broker = bt.brokers.BackBroker()
                cerebro.broker.setcash(current_cash)
                cerebro.broker.addcommissioninfo(get_commission_info(data.sec, instrument_type, settings), name=data.p.name)
                cerebro.addanalyzer(SmartAnalyzer, _name='full', **analyzer_params)
                cerebro.addanalyzer(bt.analyzers.DrawDown, _name='dd')
                cerebro.adddata(data)
                run_strategy_params = dict(
                    strategy_params,
                    close_on_expiration=close_on_expiration,
                    expiration_exit_bar=expiration_exit_bar,
                    contract_expdate=data.contract_expdate,
                )
                if exit_mode == 'bracket':
                    cerebro.addstrategy(AutoTuneFilterStrategy, **run_strategy_params)
                else:
                    cerebro.addstrategy(AutoTuneFilterEhlersStrategy, **run_strategy_params)

                runs = cerebro.run(stdstats=False, tradehistory=params["write_history"])
                strategy = runs[0]
                analyzer = strategy.analyzers.full

                analysis = dict()
                analysis.update(analyzer.get_analysis())
                add_drawdown_metrics(strategy, analysis)
                analysis['Data'] = data.p.name
                analysis['PNLs'] = analyzer.get_trades_pnl()
                analysis['Asset'] = data.sec
                analysis['CapitalMode'] = capital_mode
                results.append(analysis)

                if params['write_history']:
                    trades_data = analyzer.get_trades()
                    for tr in trades_data:
                        tr['capital_mode'] = capital_mode
                    trades.extend(trades_data)

                    orders_data = analyzer.get_orders()
                    for order_row in orders_data:
                        order_row['capital_mode'] = capital_mode
                    orders.extend(orders_data)

                    signals_data = analyzer.get_signals()
                    for signal_row in signals_data:
                        signal_row['capital_mode'] = capital_mode
                    signals.extend(signals_data)

                # Для следующего контракта используем финальную стоимость счёта.
                # При close_on_expiration=True позиция должна быть закрыта до конца контракта,
                # поэтому EndValue не должен включать незакрытый mark-to-market хвост.
                current_cash = analysis.get('EndValue', cerebro.broker.getvalue())

                if _OPT_PBAR is not None:
                    _OPT_PBAR.update(1)
                    _OPT_PBAR.set_postfix_str(f'{variant_no}/{len(param_variants)} {data.p.name}')

                print(
                    f'Кумулятивный прогон {variant_no}/{len(param_variants)}, '
                    f'контракт {data.p.name}: start={analysis.get("StartCash", 0):.2f}, '
                    f'end={analysis.get("EndValue", 0):.2f}, '
                    f'PNL={analysis.get("ContractPNL", 0):.2f}, '
                    f'{round(_time.time() - st_time, 2)} сек., '
                    f'{str(datetime.now().time())[:5]}'
                )
                gc.collect()

            print(
                f'Комбинация {variant_no}/{len(param_variants)} прошла все контракты за '
                f'{round((_time.time() - variant_time) / 60, 2)} мин.'
            )

        if _OPT_PBAR is not None:
            _OPT_PBAR.close()
            _OPT_PBAR = None

    print(f'Весь прогон за {round(_time.time() - total_time, 2)} сек., '
          f'{round((_time.time() - total_time) / 3600, 2)} часов.')

    df1 = pd.DataFrame(results).round(2)
    if params['write_history']:
        df2 = pd.DataFrame(trades).round(3)
        df_orders = pd.DataFrame(orders).round(3)
        df_signals = pd.DataFrame(signals).round(6)
    df3 = aggregate_df(df1, start_cash, sort_by=main_opt_metric)
    df4 = pd.DataFrame(
        list(params.items()) + [
            ('start_cash', start_cash),
            ('instrument_type', instrument_type),
            ('capital_mode', capital_mode),
            ('exit_mode', exit_mode),
            ('close_on_expiration', close_on_expiration),
            ('expiration_exit_bar', expiration_exit_bar),
            ('start_date', start_date),
            ('end_date', end_date),
        ],
        columns=['Parameter', 'Value']
    )
    for col in ('PNLs', 'ClosedEquity'):
        if col in df1.columns:
            del df1[col]

    # Сохраняем штамп времени для имени XLSX-файла с результатами
    timestamp = datetime.now().strftime("%d-%m-%y %H-%M")

    # Создаём имя XLSX-файла результатов. Версия берётся из имени
    # запускаемого скрипта: например, atf_strat_opt4_xlsx_prod12.py -> prod12.
    results_file = f'opt_results_{SCRIPT_VERSION}_{sec}_{tf}_{timestamp}.xlsx'

    # Записываем df в xlsx файл, xlsxwriter импортируем
    # отдельно pip install xlsxwriter
    with pd.ExcelWriter(results_file, engine='xlsxwriter') as writer:
        if not sheet_size:
            df1.to_excel(writer, sheet_name='by Contacts', index=False)
            if params['write_history']:
                df2.to_excel(writer, sheet_name='trades', index=False)
                if not df_orders.empty:
                    df_orders.to_excel(writer, sheet_name='orders', index=False)
                if not df_signals.empty:
                    df_signals.to_excel(writer, sheet_name='signals', index=False)
        df3.to_excel(writer, sheet_name='results', index=False)
        df4.to_excel(writer, sheet_name='params', index=False)

    plot_file = None

    if params['write_history'] and save_equity_dd_plot and 'df2' in locals() and not df2.empty:
        plot_file = save_best_equity_dd_plot(
            trades_df=df2,
            results_df=df3,
            results_file=results_file,
            start_cash=start_cash,
            freq=equity_dd_plot_freq,
        )

    print(f"Результаты успешно сохранены в файл '{results_file}'.")

    if plot_file:
        print(f"Сохранён график Equity/DD: '{plot_file}'.")

    os.startfile(results_file)



if __name__ == '__main__':
    # params = dict( # MIX
    #     write_history=False,
    #     risk=5,
    #     window=range(58,65, 2),   #28,  #range(48,53, 2),   #28,  #[48, 49, 50],  #range(16,57),  #30,
    #     bandwidth=[i / 100 for i in range(24, 35, 2)], #[0.3, 0.35, 0.4], #[i / 100 for i in range(30, 56, 5)], #0.46,  #, #[0.4, 0.45, 0.45],[0.34, 0.35, 0.36],  # [i/100 for i in range(30, 51, 2)],  #[0.16, 0.24, 0.32, 0.4], # 0.22, #
    #     thresh=[-i / 100 for i in range(34, 51, 2)],  #-0.5,  #[-i / 100 for i in range(25, 56, 5)],  #-0.68,  #-0.7,  #[-0.48, -0.49, 0.50],  #[-i/100 for i in range(42, 55, 2)],  #[-i / 12.5 for i in range(4, 9)],  #[0.32, 0.4, 0.48, 0.56, 0.64], #
    #     allow_short=True,
    #     printlog=False,
    #     tp_mult=[i / 10 for i in range(11, 20, 2)],  #1.8,  #[i / 10 for i in range(15, 22, 3)],  #1.8,  #1.5,  #[1+i/10 for i in range(1,7)],   # тейк-профит в R
    #     min_dc=range(10,36,5),
    # )

    STRATEGY_PARAMS = dict(
        write_history=False,
        risk=5,
        window=range(55,96,5),
        bandwidth=[i / 100 for i in range(15, 41, 5)],
        thresh=[-i / 100 for i in range(40, 71, 5)],
        allow_short=False,
        printlog=False,
        tp_mult=[i / 10 for i in range(2, 13, 2)],
        min_dc=(0, 25),
    )

    RUN_SETTINGS = dict(
        start_cash=300000.0,

        # futures — фьючерсы MOEX: sec задаёт базовый код, серии подбираются автоматически.
        # stocks  — акции MOEX: sec задаёт тикер акции или список тикеров, без экспираций.
        instrument_type='stocks',

        # fixed      — каждый контракт/инструмент запускается с одинакового start_cash.
        # cumulative — капитал переносится от контракта к контракту отдельно
        #              для каждой комбинации параметров.
        capital_mode='fixed',

        # bracket — текущая рабочая логика: stop-loss / take-profit.
        # ehlers  — выход/разворот по обратному сигналу ATF из статьи Эйлерса.
        exit_mode='bracket',

        # Для stocks эти параметры игнорируются, потому что contract_expdate=None.
        close_on_expiration=True,
        expiration_exit_bar=3,

        # Настройки комиссии для stock-режима. Значения задаются долей: 0.0003 = 0.03%.
        stock_moexcomm=0.0003,
        stock_brokercomm=0.0003,
        stock_price_step=0.01,

        params=STRATEGY_PARAMS,
        tf='1h',
        start_date='2025-6-20',
        end_date=datetime.today(),
        main_opt_metric='PROM',
        sec='SBER',  # 'SBER' 'MIX' 'SPYF'
        save_equity_dd_plot=True,
        equity_dd_plot_freq='M',  # месячные столбцы Equity/DD
    )

    maxcpus = os.cpu_count()
    available_cpus = maxcpus - 2
    print(f'Задействуем {available_cpus} потоков из {maxcpus} возможных.')
    main(available_cpus, settings=RUN_SETTINGS)
