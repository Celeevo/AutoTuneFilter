from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import os
import math
import time as _time
from datetime import datetime, time, timedelta
import backtrader as bt
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

# =============================================================================
# CHANGELOG prod12 vs prod11 (10-05-26, ред. 11-05-26 после диагностики)
# =============================================================================
# (1) Адаптивный фильтр Correlation Angle, период которого равен текущему
#     доминантному циклу (DC) AutoTune. Это убирает рассогласованность периодов
#     и заставляет оба индикатора смотреть на одну и ту же волну.
#     ВАЖНО: DC передаётся в индикатор ПОЗИЦИОННО (вторым аргументом), иначе
#     backtrader не отслеживает minperiod-зависимость и индикатор получает
#     мусор на вход.
#
# (2) Фильтр согласия фаз с тремя режимами:
#       'off'       — фильтр выключен (= prod11);
#       'direction' — только направление угла (восходящая/нисходящая полусфера);
#       'state'     — только state==0 (циклический режим);
#       'both'      — обе проверки.
#     Дефолт 'direction'. State исключён из дефолта, потому что после wraparound
#     фазы (+180 -> -180) он ложно срабатывает как тренд и режет именно те
#     моменты, когда AutoTune даёт сигнал на впадине цикла.
#     Граничные условия по углу — нестрогие (-180 и +180 включены), потому
#     что на cross_up впадины цикла угол часто оказывается ровно -180.
#
# (3) Управление стопом и выходом перестроено:
#       - стоп-лосс = entry ± stop_dc_mult * DC * ATR (динамически по циклу
#         и волатильности);
#       - размер позиции считается из risk% и расстояния до стопа;
#       - выход — по противоположному сигналу AutoTune
#         (exit_on_opposite_signal=True) и/или по time-exit
#         (time_exit_dc_mult * DC баров с момента входа);
#       - фиксированный тейк-профит (tp_mult) удалён как структурно
#         несовместимый с mean-reversion-логикой.
#
# (4) Диагностические счётчики (diag_print_filter_stats=True):
#     на stop() стратегии печатаются разрезы прохождения сигналов через
#     каждый этап фильтрации — сколько было базовых сигналов AutoTune, сколько
#     отрезал Correlation Angle, сколько провалилось на расчёте размера,
#     сколько стало сделок. Помогает быстро понять, кто кого режет.
#
# Удалённые параметры:  tp_mult, use_corr_angle_filter (заменён на
#                       corr_filter_mode).
# Удалённые классы:     AllInSizer (расчёт размера переехал в стратегию).
# Прежние фильтры min_dc, max_adx сохранены без изменений.
# =============================================================================

FUTURE_TYPE = dict(     # Базовая ставка комиссии Биржи
    currency=0.00462,   # Валютные контракты
    percent=0.01650,    # Процентные контракты
    stock=0.01980,      # Фондовые контракты
    xindex=0.00660,     # Индексные контракты
    commodity=0.01320   # Товарные контракты
)


class FuturesCommission(bt.CommInfoBase):
    params = dict(moexcomm=0.0, cost_of_price_step=0)

    def _getcommission(self, size, price, pseudoexec):
        brokers_pocket = abs(size) * self.p.commission
        moexs_pocket = abs(size) * price * self.p.mult * self.p.moexcomm / 100
        return brokers_pocket + moexs_pocket


class StockCommission(bt.CommInfoBase):
    params = dict(moexcomm=0, brokercomm=0)

    def _getcommission(self, size, price, pseudoexec):
        return abs(size) * price * (self.p.moexcomm + self.p.brokercomm)


futures_comm = dict(  # Комиссии для фьючерсов
    RTS=FuturesCommission(commission=2.0, margin=26358, mult=14.92418/10,
                          moexcomm=FUTURE_TYPE['xindex'], cost_of_price_step=10),
    RTSM=FuturesCommission(commission=2.0, margin=2900, mult=8.26549/0.5,
                           moexcomm=FUTURE_TYPE['xindex']),
    NASD=FuturesCommission(commission=2.0, margin=2607, mult=0.97966/1,
                           moexcomm=FUTURE_TYPE['xindex']),
    CNY=FuturesCommission(commission=2.0, margin=1050, mult=1/0.001,
                          moexcomm=FUTURE_TYPE['currency'], cost_of_price_step=0.001),
    Si=FuturesCommission(commission=2.0, margin=11779, mult=1,
                         moexcomm=FUTURE_TYPE['currency'], cost_of_price_step=1),
    Eu=FuturesCommission(commission=2.0, margin=16000, mult=1,
                         moexcomm=FUTURE_TYPE['currency']),
    NG=FuturesCommission(commission=2.0, margin=6300, mult=9.8/0.001,
                         moexcomm=FUTURE_TYPE['commodity']),
    GOLD=FuturesCommission(commission=2.0, margin=16600, mult=9.8/0.1,
                           moexcomm=FUTURE_TYPE['commodity']),
    SBRF=FuturesCommission(commission=2.0, margin=5200, mult=1,
                           moexcomm=FUTURE_TYPE['stock']),
    BR=FuturesCommission(commission=2.0, margin=10374, mult=10.167/0.01,
                         moexcomm=FUTURE_TYPE['commodity']),
    MIX=FuturesCommission(commission=2.0, margin=33000, mult=25/25,
                          moexcomm=FUTURE_TYPE['xindex'], cost_of_price_step=25),
    MXI=FuturesCommission(commission=2.0, margin=3500, mult=0.5/0.05,
                          moexcomm=FUTURE_TYPE['xindex'], cost_of_price_step=0.05),
    SPYF=FuturesCommission(commission=2.0, margin=5600, mult=0.82655/0.01,
                           moexcomm=FUTURE_TYPE['xindex'], cost_of_price_step=0.05),
)


def round_to_nearest_price_step(step, value, isbuy):
    """
    Универсальное округление до ближайшего кратного шага цены.
    """
    if step <= 0:
        raise ValueError('step должен быть > 0')
    step_d = Decimal(str(step))
    value_d = Decimal(str(value))
    steps_cnt = value_d / step_d
    rounding_mode = ROUND_FLOOR if isbuy else ROUND_CEILING
    steps_cnt = steps_cnt.to_integral_value(rounding=rounding_mode)
    return float(steps_cnt * step_d)


def iterable_params(p: dict):
    names = [k for k, v in p.items() if isinstance(v, (list, tuple, set, range))]
    return names if names else ['params']


def count_param_variants(params_dict):
    variants = 1
    for value in params_dict.values():
        if isinstance(value, (list, tuple, set, range)):
            variants *= len(value)
    return variants


# =============================================================================
# Аналитика — без изменений по сравнению с prod11
# =============================================================================

def compute_group_metrics(group, startingcash=1):
    pnls = list(chain.from_iterable(group['PNLs']))
    wta = [i for i in pnls if i >= 0]
    lta = [i for i in pnls if i < 0]
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
    mean_pnl = np.mean(pnls) if pnls else 0
    stdev_pnl = np.std(group['PNL'], ddof=1) if len(group) > 1 else 0
    last_pnl = group['PNL'].iloc[-1]
    pre_last_pnl = group['PNL'].iloc[-2] if len(group) >= 2 else 0
    pf = -swt / slt if slt else 0
    prom = (aw * (lenw - sqrt(lenw)) + al * (lenl + sqrt(lenl))) / startingcash

    if lenw > 1:
        e_pardo = ((swt - mw) / (lenw - 1) * (lenw - 1 - sqrt(lenw))
                   + al * (lenl + sqrt(lenl))) / startingcash
    else:
        e_pardo = 0
    if stdev_pnl > 0 and mean_pnl > 0:
        s_pardo = e_pardo * sqrt(mean_pnl / stdev_pnl)
    else:
        s_pardo = 0

    neg_pnls = (group['PNL'] < 0).sum()
    last4 = group['PNL'].iloc[-4:]
    last4neg = (last4 < 0).sum()
    asset = group['Asset'].iloc[0]

    return pd.Series({
        'Asset': asset, 'PNL': pnl, 'WinTr': lenw, 'LossTr': lenl,
        'SumWin': swt, 'SumLoss': slt, 'W-L': w_l, 'W/L': w_div_l,
        'AvgWin': aw, 'AvgLoss': al, 'StdDev': stdev_pnl,
        'LastPNL': last_pnl, 'PreLastPNL': pre_last_pnl,
        'PF': pf, 'PROM': prom, 'e-Pardo': e_pardo, 's-Pardo': s_pardo,
        'NegPNLs': neg_pnls, 'Last4Neg': last4neg,
    })


def aggregate_df(df, startingcash=1, sort_by='s-Pardo', sort_by_second='s-Pardo'):
    first_col = df.columns[0]
    aggr = df.groupby(first_col, sort=False)[['PNLs', 'PNL', 'Asset']].apply(
        compute_group_metrics, startingcash=startingcash).reset_index()
    aggr = aggr.sort_values(sort_by, ascending=False)
    if 's-Pardo' in aggr.columns and aggr['s-Pardo'].iloc[0] <= 0:
        aggr = aggr.sort_values(sort_by_second, ascending=False)
    return aggr.round(2)


class SmartAnalyzer(Analyzer):
    params = dict(it_params=None, asset=None)

    def __init__(self):
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
            if self.strategy.p.write_history:
                trade.start_cash = self.depos
                trade.finish_cash = self.strategy.broker.getcash()
                trade.stop_loss_price = self.strategy.stop_loss_price
                trade.sec_id = trade.getdataname()
                self.trades.append(trade)
                self.depos = self.strategy.broker.getcash()

    def stop(self):
        st_params = self.strategy.p._getkwargs()
        params_head = '-'.join(str(k) for k in st_params.keys() if k in self.p.it_params) + '-asset'
        params_str = '-'.join(str(v) for k, v in st_params.items() if k in self.p.it_params) + '-' + self.p.asset

        wt = len(self.pt_arr)
        aw = mean(self.pt_arr) if wt else 0
        lt = len(self.lt_arr)
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
                    stop_loss_price=getattr(trade, 'stop_loss_price', 0),
                    size=entry_order.size,
                    entry_type='long' if entry_order.isbuy() else 'short',
                    exit_ref=exit_order.ref,
                    exit_created_date=f'{bt.num2date(exit_order.created.dt):%d.%m.%y}',
                    exit_created_time=f'{bt.num2date(exit_order.created.dt):%H:%M}',
                    exit_executed_date=f'{bt.num2date(exit_order.executed.dt):%d.%m.%y}',
                    exit_executed_time=f'{bt.num2date(exit_order.executed.dt):%H:%M}',
                    exit_requested_price=exit_order.created.price,
                    exit_executed_price=exit_order.executed.price,
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


# =============================================================================
# (1) Адаптивный Correlation Angle Indicator с периодом = DC от AutoTune
# =============================================================================

class CorrelationAngleAdaptive(bt.Indicator):
    """
    John Ehlers - Correlation Angle, адаптивная версия.

    В отличие от стандартной демки, период корреляции на каждом баре равен
    текущему доминантному циклу AutoTune (DC), переданному ВТОРЫМ позиционным
    аргументом. Позиционная передача обязательна — иначе backtrader не
    отслеживает minperiod-зависимость и не гарантирует, что DC будет
    рассчитан до этого индикатора.

    Использование:
        self.corr = CorrelationAngleAdaptive(self.data.close, self.atf.dc)

    Линии:
    - real, imag : корреляции с Cosine и -Sine соответственно;
    - angle      : фазовый угол, диапазон примерно -180..+180 градусов;
    - state      : 0 = cycle mode, 1 = trend up, -1 = trend down.

    Параметры:
    - max_period       : верхняя граница окна корреляции; задаёт minperiod
                         индикатора (нужно достаточно баров истории);
    - min_period       : нижняя граница для защиты от слишком коротких циклов;
    - trend_threshold  : порог из статьи (9° = 40-баровый цикл).
    """

    lines = ('real', 'imag', 'angle', 'state')

    params = (
        ('max_period', 80),
        ('min_period', 6),
        ('trend_threshold', 9.0),
        ('eps', 1e-12),
    )

    plotinfo = dict(subplot=True, plotname='CorrAngle (adaptive)')

    plotlines = dict(
        real=dict(_name='Real / CosCorr', _plotskip=True),
        imag=dict(_name='Imag / NegSineCorr', _plotskip=True),
        angle=dict(_name='Phase angle'),
        state=dict(_name='State'),
    )

    def __init__(self):
        if self.p.max_period < 2:
            raise ValueError('CorrelationAngleAdaptive: max_period должен быть >= 2')
        # Достаточно памяти для самой длинной возможной корреляции
        self.addminperiod(int(self.p.max_period))

    @staticmethod
    def _safe(value, default=0.0):
        if value is None:
            return default
        try:
            if math.isnan(value):
                return default
        except TypeError:
            pass
        return value

    def _correlate_with_wave(self, period, wave_func, fallback):
        """Pearson correlation между Price (data) и заданной волной wave_func."""
        sx = sy = sxx = sxy = syy = 0.0
        for count in range(1, period + 1):
            bars_ago = count - 1
            x = self._safe(self.data[-bars_ago], 0.0)
            y = wave_func(bars_ago, period)
            sx += x
            sy += y
            sxx += x * x
            sxy += x * y
            syy += y * y
        vx = period * sxx - sx * sx
        vy = period * syy - sy * sy
        if vx <= self.p.eps or vy <= self.p.eps:
            return fallback
        corr = (period * sxy - sx * sy) / math.sqrt(vx * vy)
        return self._safe(corr, fallback)

    def next(self):
        # DC передан вторым позиционным аргументом — backtrader отслеживает
        # его minperiod-зависимость, поэтому к этому моменту DC уже посчитан.
        dc_now = self._safe(self.data1[0], self.p.min_period)
        period = max(int(self.p.min_period),
                     min(int(self.p.max_period), int(round(dc_now))))

        prev_real = self._safe(self.l.real[-1], 0.0) if len(self) > 1 else 0.0
        prev_imag = self._safe(self.l.imag[-1], 0.0) if len(self) > 1 else 0.0
        prev_angle = self._safe(self.l.angle[-1], 0.0) if len(self) > 1 else 0.0

        # 1. Корреляция с Cosine
        real = self._correlate_with_wave(
            period,
            lambda ago, T: math.cos(math.radians(360.0 * ago / T)),
            fallback=prev_real,
        )
        # 2. Корреляция с -Sine
        imag = self._correlate_with_wave(
            period,
            lambda ago, T: -math.sin(math.radians(360.0 * ago / T)),
            fallback=prev_imag,
        )

        # 3. Phase angle через арктангенс с разрешением неоднозначности
        if abs(imag) > self.p.eps:
            angle_raw = 90.0 + math.degrees(math.atan(real / imag))
            if imag > 0.0:
                angle_raw -= 180.0
        else:
            angle_raw = prev_angle

        angle = angle_raw

        # 4. Anti-regression: фаза не идёт назад, кроме wraparound (+180 -> -180)
        if len(self) > 1:
            if (prev_angle - angle < 270.0) and (angle < prev_angle):
                angle = prev_angle

        # 5. State: если изменение фазы меньше порога — это трендовый режим
        state = 0.0
        if len(self) > 1 and abs(angle - prev_angle) < float(self.p.trend_threshold):
            state = -1.0 if angle < 0.0 else 1.0

        self.l.real[0] = real
        self.l.imag[0] = imag
        self.l.angle[0] = angle
        self.l.state[0] = state


# =============================================================================
# AutoTuneFilterStrategy v12: фильтр согласия фаз + ATR-стоп + выход по сигналу
# =============================================================================

class AutoTuneFilterStrategy(bt.Strategy):
    """
    Стратегия по AutoTune-фильтру (Эрлерс) с дополнениями prod12:

    - Базовая логика входа из статьи + фильтры min_dc / max_adx из prod11.
    - (prod12) Фильтр согласия фаз через Correlation Angle (адаптивный):
        вход в long  при state==0 и угле в восходящей полусфере (-180..0);
        вход в short при state==0 и угле в нисходящей    (0..+180).
    - (prod12) Стоп-лосс динамический: stop_dc_mult * DC * ATR.
    - (prod12) Размер позиции: (cash * risk%) / (stop_distance * mult).
    - (prod12) Выход — по противоположному сигналу AutoTune
      и/или по time-exit (time_exit_dc_mult * DC баров).
    - (prod12) Тейк-профит удалён как структурно несовместимый с mean-reversion.
    """

    params = dict(
        write_history=None,
        risk=5,                       # риск на одну сделку, % от cash
        window=26,
        bandwidth=0.22,
        thresh=-0.22,
        allow_short=True,
        printlog=False,

        # === Старые фильтры из prod11 — поведение прежнее ===
        min_dc=0,                     # минимальный DC AutoTune для входа
        max_adx=999,                  # максимальный ADX(14) для входа

        # === (prod12) Correlation Angle filter ===
        # Режимы фильтра согласия фаз:
        #   'off'       — фильтр выключен (поведение как в prod11);
        #   'direction' — только направление угла должно совпадать с
        #                 направлением сигнала AutoTune;
        #   'state'     — только state==0 (циклический режим);
        #   'both'      — обе проверки (state==0 И направление совпадает).
        # Дефолт 'direction' — компромисс: state выкинут, потому что после
        # wraparound фазы (+180 -> -180) он ложно срабатывает как тренд и
        # режет ровно те моменты, ради которых фильтр и нужен.
        corr_filter_mode='direction',
        corr_max_period=80,
        corr_min_period=6,
        corr_trend_threshold=9.0,

        # === (prod12) Управление стопом и выходом ===
        stop_atr_period=14,           # период ATR
        stop_dc_mult=0.4,             # stop_distance = stop_dc_mult * DC * ATR
        time_exit_dc_mult=0.75,       # time-exit через time_exit_dc_mult*DC баров;
                                      #   0 или None отключает time-exit
        exit_on_opposite_signal=True, # выход по противоположному сигналу AutoTune

        # === (prod12) Диагностика ===
        # Если True — на stop() стратегии печатаются счётчики прохождения
        # сигналов через каждый этап фильтрации. Очень полезно, когда сделок
        # подозрительно мало. На оптимизацию не влияет (только output).
        diag_print_filter_stats=False,
    )

    def log(self, txt):
        if self.p.printlog:
            dt = self.data.datetime.datetime(0)
            print(f'{dt} | {txt}')

    def __init__(self):
        # AutoTune
        self.atf = AutoTuneFilter(
            self.data.close,
            window=self.p.window,
            bandwidth=self.p.bandwidth,
        )

        # Фильтры из prod11
        self.adx = bt.indicators.ADX(self.data, period=14)

        # ATR для динамического стопа
        self.atr = bt.indicators.ATR(self.data, period=int(self.p.stop_atr_period))

        # Correlation Angle с DC от AutoTune.
        # КРИТИЧНО: DC передаётся ПОЗИЦИОННО (вторым аргументом), иначе
        # backtrader не отслеживает minperiod-зависимость и индикатор может
        # считаться раньше, чем DC рассчитан — на выходе будет мусор.
        self.corr = CorrelationAngleAdaptive(
            self.data.close,
            self.atf.dc,
            max_period=int(self.p.corr_max_period),
            min_period=int(self.p.corr_min_period),
            trend_threshold=float(self.p.corr_trend_threshold),
        )

        # Сигналы AutoTune
        self.roc = self.atf.bp - self.atf.bp(-2)
        self.cross_up = bt.indicators.CrossUp(self.roc, 0.0)
        self.cross_down = bt.indicators.CrossDown(self.roc, 0.0)

        # Базовое условие входа из статьи + старые фильтры prod11
        self._long_base = bt.And(
            self.cross_up,
            self.atf.mincorr < self.p.thresh,
            self.atf.dc >= self.p.min_dc,
            self.adx <= self.p.max_adx,
        )
        self._short_base = bt.And(
            self.cross_down,
            self.atf.mincorr < self.p.thresh,
            self.atf.filt > 0,
            self.atf.dc >= self.p.min_dc,
            self.adx <= self.p.max_adx,
        )

        # Состояние сделки
        self.entry_order = None
        self.stop_order = None
        self.close_order = None  # ордер, отправленный через _close_position_by_signal
        self.entry_price = 0.0
        self.stop_loss_price = 0.0
        self.entry_bar = 0
        self.entry_dc = 0.0
        self.entry_isbuy = None  # True для long, False для short
        # Причина выхода из текущей сделки. Устанавливается перед закрытием
        # и используется в notify_trade(), который backtrader вызывает после
        # notify_order(). НЕ обнуляется в _reset_state — иначе теряется к
        # моменту notify_trade. Обнуляется только при открытии нового входа.
        self._last_exit_reason = None

        # === Диагностические счётчики прохождения через фильтры ===
        # Считаем сигналы базы AutoTune (cross_up + mincorr<thresh + min_dc + max_adx)
        # и далее — куда они девались.
        self._diag = dict(
            base_long=0,
            base_short=0,
            ca_rejected_long=0,
            ca_rejected_short=0,
            entry_calc_failed_long=0,
            entry_calc_failed_short=0,
            entries_long=0,
            entries_short=0,
            # === Разрез по типу выхода (заполняется в notify_trade) ===
            exit_time=0,
            exit_opposite=0,
            exit_stop=0,
            exit_unknown=0,
            pnl_time=0.0,
            pnl_opposite=0.0,
            pnl_stop=0.0,
            pnl_unknown=0.0,
            bars_held_sum=0,
            bars_held_count=0,
            # === Контекст входа (заполняется в _submit_entry_with_params) ===
            dc_at_entry_sum=0.0,
            atr_at_entry_sum=0.0,
            stop_dist_sum=0.0,
            # === Отказы ордеров (заполняется в notify_order) ===
            rejected_entry=0,
            rejected_stop=0,
            rejected_close=0,
        )

    # -------------------------------------------------------------------------
    # Применение фильтра согласия фаз (prod12)
    # -------------------------------------------------------------------------

    def _phase_allows_long(self):
        """True, если Correlation Angle подтверждает направление long."""
        mode = self.p.corr_filter_mode
        if mode == 'off':
            return True

        state_ok = int(round(self.corr.state[0])) == 0
        # Восходящая фаза: angle в [-180, 0]. Нестрогие границы — потому что
        # на cross_up впадины цикла угол часто оказывается ровно -180.
        angle = self.corr.angle[0]
        direction_ok = -180.0 <= angle <= 0.0

        if mode == 'state':
            return state_ok
        if mode == 'direction':
            return direction_ok
        if mode == 'both':
            return state_ok and direction_ok
        # Неизвестный режим — fail-safe в сторону пропуска
        return True

    def _phase_allows_short(self):
        """True, если Correlation Angle подтверждает направление short."""
        mode = self.p.corr_filter_mode
        if mode == 'off':
            return True

        state_ok = int(round(self.corr.state[0])) == 0
        # Нисходящая фаза: angle в [0, 180]. Нестрогие границы.
        angle = self.corr.angle[0]
        direction_ok = 0.0 <= angle <= 180.0

        if mode == 'state':
            return state_ok
        if mode == 'direction':
            return direction_ok
        if mode == 'both':
            return state_ok and direction_ok
        return True

    # -------------------------------------------------------------------------
    # Расчёт стоп-лосса и размера позиции (prod12)
    # -------------------------------------------------------------------------

    def _round_price(self, comminfo, price, isbuy):
        if comminfo.p.cost_of_price_step != 0:
            return round_to_nearest_price_step(comminfo.p.cost_of_price_step, price, isbuy)
        return price

    def _calc_entry_params(self, isbuy):
        """
        Возвращает (size, entry_price, stop_loss_price) или None, если вход
        невозможен (нулевой DC, нулевой ATR, недостаточно средств и т.д.).
        """
        comminfo = self.broker.getcommissioninfo(self.data)
        cash = self.broker.getcash()
        entry_price = self.data.close[0]
        if entry_price <= 0:
            return None

        dc_now = float(self.atf.dc[0])
        atr_now = float(self.atr[0])
        if dc_now <= 0 or atr_now <= 0:
            return None

        # Расстояние стопа в пунктах
        stop_distance = float(self.p.stop_dc_mult) * dc_now * atr_now
        if stop_distance <= 0:
            return None

        # Размер позиции: убыток на стопе = risk% от cash
        # Один контракт = mult денег за 1 пункт цены (для фьючерса)
        risk_money = cash * (self.p.risk / 100.0)
        denom = stop_distance * comminfo.p.mult
        if denom <= 0:
            return None
        size = int(risk_money / denom)

        # Дополнительный лимит: не больше, чем позволяет ГО
        if comminfo.p.margin > 0:
            max_by_margin = int(cash / comminfo.p.margin) - 1
            if max_by_margin <= 0:
                return None
            size = min(size, max_by_margin)

        if size <= 0:
            return None

        direction = 1 if isbuy else -1
        raw_stop = entry_price - direction * stop_distance
        stop_price = self._round_price(comminfo, raw_stop, isbuy)

        return size, entry_price, stop_price

    # -------------------------------------------------------------------------
    # Управление ордерами (prod12)
    # -------------------------------------------------------------------------

    def _is_busy(self):
        """True, если есть активные ордера, ожидающие исполнения/отмены."""
        for o in (self.entry_order,):
            if o is not None and o.alive():
                return True
        return False

    def _reset_state(self):
        self.entry_order = None
        self.stop_order = None
        self.close_order = None
        self.entry_price = 0.0
        self.stop_loss_price = 0.0
        self.entry_bar = 0
        self.entry_dc = 0.0
        self.entry_isbuy = None

    def _submit_entry_with_params(self, isbuy, params):
        """Отправляет ордер на вход с уже рассчитанными параметрами."""
        size, entry_price, stop_price = params

        side = 'long' if isbuy else 'short'
        self.log(f'{side.upper()} SIGNAL | size={size} | stop={stop_price:.4f}')

        # Фиксируем планируемые значения, чтобы при исполнении выставить стоп
        self.entry_price = entry_price
        self.stop_loss_price = stop_price
        self.entry_isbuy = isbuy
        self.entry_dc = float(self.atf.dc[0])

        # Новый вход — обнуляем причину выхода из предыдущей сделки
        self._last_exit_reason = None

        # Контекст входа для усреднений в diag-выводе
        self._diag['dc_at_entry_sum'] += self.entry_dc
        self._diag['atr_at_entry_sum'] += float(self.atr[0])
        self._diag['stop_dist_sum'] += abs(entry_price - stop_price)

        method = self.buy if isbuy else self.sell
        # Имя пишем через addinfo, а не через oargs:
        # oargs работает только в buy_bracket/sell_bracket, для простых
        # buy/sell оно молча игнорируется и order.info.name остаётся пустым.
        order = method(size=size, exectype=bt.Order.Market)
        order.addinfo(name=side)
        self.entry_order = order

    def _submit_stop_after_fill(self, executed_price):
        """Выставляет защитный стоп после исполнения входного ордера."""
        comminfo = self.broker.getcommissioninfo(self.data)
        isbuy = self.entry_isbuy

        # Пересчитываем стоп от фактической цены исполнения, а не от close
        dc_at_entry = self.entry_dc if self.entry_dc > 0 else float(self.atf.dc[0])
        atr_at_entry = float(self.atr[0])
        stop_distance = float(self.p.stop_dc_mult) * dc_at_entry * atr_at_entry
        if stop_distance <= 0:
            self.log('WARNING: stop_distance <= 0, stop не выставлен')
            return

        direction = 1 if isbuy else -1
        raw_stop = executed_price - direction * stop_distance
        self.stop_loss_price = self._round_price(comminfo, raw_stop, isbuy)

        # Стоп-ордер на закрытие позиции: для long — sell stop, для short — buy stop
        size = abs(self.position.size)
        stop_method = self.sell if isbuy else self.buy
        order = stop_method(
            size=size,
            exectype=bt.Order.Stop,
            price=self.stop_loss_price,
        )
        order.addinfo(name='stop_loss')
        self.stop_order = order
        self.log(f'STOP placed at {self.stop_loss_price:.4f}')

    def _close_position_by_signal(self, reason_type, reason_detail=''):
        """
        Закрытие позиции рыночным ордером, отмена защитного стопа.

        reason_type: 'time' | 'opposite' — для счётчиков диагностики.

        Закрытие отправляется явным sell/buy с name='close_signal', чтобы
        notify_order мог отличить его от срабатывания стопа и от обычного
        входного ордера. self.close() не используется, потому что у созданного
        им ордера нет name в info, и он попадает в общую else-ветку.
        """
        # Защита от race condition: если стоп успел сработать в тот же момент,
        # позиция уже 0 — ничего закрывать не нужно.
        if self.position.size == 0:
            return

        self._last_exit_reason = reason_type
        if self.stop_order is not None and self.stop_order.alive():
            self.cancel(self.stop_order)

        full_reason = f'{reason_type} ({reason_detail})' if reason_detail else reason_type
        self.log(f'CLOSE by {full_reason} at close={self.data.close[0]:.4f}')

        size = abs(self.position.size)
        # Для long-позиции закрываем sell; для short — buy.
        method = self.sell if self.position.size > 0 else self.buy
        order = method(size=size, exectype=bt.Order.Market)
        order.addinfo(name='close_signal')
        self.close_order = order

    # -------------------------------------------------------------------------
    # Колбэки
    # -------------------------------------------------------------------------

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        # Идентификация ордера ПО ОБЪЕКТУ, а не по order.info.name.
        # Это надёжно: даже если addinfo по какой-то причине не сработает,
        # сравнение `order is self.entry_order` всегда даст правильный ответ.
        # name (если есть) используется только для логов.
        order_name = getattr(order.info, 'name', None) or '?'
        is_entry = (order is self.entry_order)
        is_stop = (order is self.stop_order)
        is_close = (order is self.close_order)

        if order.status == order.Completed:
            if is_entry:
                self.log(f'ENTRY {order_name.upper()} EXECUTED at {order.executed.price:.4f}')
                self.entry_price = order.executed.price
                self.entry_bar = len(self)
                self.entry_order = None
                # Выставляем защитный стоп
                self._submit_stop_after_fill(order.executed.price)
            elif is_stop:
                self.log(f'STOP LOSS HIT at {order.executed.price:.4f}')
                # Причина выхода — для notify_trade. Должна быть установлена
                # ДО _reset_state, потому что notify_trade приходит после
                # notify_order и читает self._last_exit_reason.
                self._last_exit_reason = 'stop'
                self._reset_state()
            elif is_close:
                self.log(f'CLOSE EXECUTED at {order.executed.price:.4f}')
                # _last_exit_reason уже выставлен в _close_position_by_signal
                self._reset_state()
            else:
                # Сюда попадаем, если ордер не один из наших — теоретически
                # такого не должно быть. НЕ сбрасываем состояние, чтобы не
                # сломать отслеживание реальных ордеров.
                self.log(f'WARNING: completed UNKNOWN order at '
                         f'{order.executed.price:.4f} (name={order_name!r})')
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log(f'ORDER {order_name} FAILED: {order.getstatusname()}')
            if is_entry:
                self._diag['rejected_entry'] += 1
                self._reset_state()
            elif is_stop:
                # Cancel ожидаем (мы сами отменяем стоп при close-by-signal),
                # это не ошибка. Margin/Rejected — это плохо: позиция останется
                # без стопа. Отдельно считаем только реальные отказы.
                if order.status != order.Canceled:
                    self._diag['rejected_stop'] += 1
                self.stop_order = None
            elif is_close:
                self._diag['rejected_close'] += 1
                self.close_order = None
                # Позиция всё ещё открыта; на следующем баре next() снова
                # попытается закрыть. _last_exit_reason сохраняется.

    def notify_trade(self, trade):
        """
        Учёт закрытых сделок в разрезе по причине выхода. Выполняется ПОСЛЕ
        notify_order, поэтому self._last_exit_reason к этому моменту уже
        содержит правильное значение ('time' / 'opposite' / 'stop').
        """
        if not trade.isclosed:
            return
        reason = self._last_exit_reason or 'unknown'
        # Защита от опечаток / неожиданных значений
        if reason not in ('time', 'opposite', 'stop'):
            reason = 'unknown'
        self._diag[f'exit_{reason}'] += 1
        self._diag[f'pnl_{reason}'] += trade.pnlcomm
        self._diag['bars_held_sum'] += trade.barlen
        self._diag['bars_held_count'] += 1

    def next(self):
        # Если есть позиция — следим за условиями выхода
        if self.position:
            # Time-based выход
            if self.p.time_exit_dc_mult and self.entry_dc > 0:
                bars_held = len(self) - self.entry_bar
                max_bars = int(self.p.time_exit_dc_mult * self.entry_dc)
                if max_bars > 0 and bars_held >= max_bars:
                    self._close_position_by_signal('time', f'{bars_held} bars')
                    return
            # Выход по противоположному сигналу AutoTune
            if self.p.exit_on_opposite_signal:
                if self.entry_isbuy is True and self.cross_down[0]:
                    self._close_position_by_signal('opposite', 'cross_down')
                    return
                if self.entry_isbuy is False and self.cross_up[0]:
                    self._close_position_by_signal('opposite', 'cross_up')
                    return
            return  # пока в позиции — новых входов не делаем

        # Нет позиции и нет ожидающих ордеров — проверяем сигналы на вход
        if self._is_busy():
            return

        # Базовые сигналы AutoTune (cross + mincorr<thresh + min_dc + max_adx)
        long_base = bool(self._long_base[0])
        short_base = bool(self.p.allow_short and self._short_base[0])

        if long_base:
            self._diag['base_long'] += 1
            if not self._phase_allows_long():
                self._diag['ca_rejected_long'] += 1
            else:
                # Прошёл фильтр Correlation Angle — пробуем рассчитать вход
                params = self._calc_entry_params(isbuy=True)
                if params is None:
                    self._diag['entry_calc_failed_long'] += 1
                else:
                    self._diag['entries_long'] += 1
                    self._submit_entry_with_params(isbuy=True, params=params)
                    return

        if short_base:
            self._diag['base_short'] += 1
            if not self._phase_allows_short():
                self._diag['ca_rejected_short'] += 1
            else:
                params = self._calc_entry_params(isbuy=False)
                if params is None:
                    self._diag['entry_calc_failed_short'] += 1
                else:
                    self._diag['entries_short'] += 1
                    self._submit_entry_with_params(isbuy=False, params=params)

    def stop(self):
        """Печать диагностических счётчиков по фильтрам."""
        if not self.p.diag_print_filter_stats:
            return

        # Открытая позиция в конце бэктеста — явный признак, что exit-логика
        # не сработала. Это сразу видно глазами в логе и позволяет не гадать,
        # «реально 0 сделок или просто все висят открытыми».
        if self.position:
            print(
                f'[DIAG {self.data._name}] WARNING: позиция ОТКРЫТА в конце '
                f'бэктеста, size={self.position.size}, '
                f'entry_isbuy={self.entry_isbuy}, entry_dc={self.entry_dc:.1f}, '
                f'entry_bar={self.entry_bar}, last_bar={len(self)}. '
                f'Это значит, что ни stop, ни time-exit, ни opposite-signal '
                f'не сработали — копайте логи notify_order.'
            )
        d = self._diag
        total_base = d['base_long'] + d['base_short']
        total_entries = d['entries_long'] + d['entries_short']
        total_ca_rej = d['ca_rejected_long'] + d['ca_rejected_short']
        total_calc_fail = d['entry_calc_failed_long'] + d['entry_calc_failed_short']

        # Не засоряем вывод, если стратегия совсем не активировалась
        if total_base == 0:
            print(
                f'[DIAG {self.data._name}] base_signals=0 — '
                f'AutoTune не выдал ни одного сигнала. '
                f'Проверьте window/bandwidth/thresh/min_dc/max_adx, '
                f'возможно условия слишком жёсткие.'
            )
            return

        print(
            f'[DIAG {self.data._name}] '
            f'mode={self.p.corr_filter_mode} | '
            f'base={total_base} (L={d["base_long"]}, S={d["base_short"]}) -> '
            f'CA_rejected={total_ca_rej} (L={d["ca_rejected_long"]}, S={d["ca_rejected_short"]}), '
            f'calc_failed={total_calc_fail} -> '
            f'entries={total_entries} (L={d["entries_long"]}, S={d["entries_short"]})'
        )

        # === Разрез по типу выхода ===
        total_exits = (d['exit_time'] + d['exit_opposite']
                       + d['exit_stop'] + d['exit_unknown'])
        if total_exits > 0:
            avg_bars = (d['bars_held_sum'] / d['bars_held_count']
                        if d['bars_held_count'] else 0.0)

            def _fmt_exit(name, key):
                cnt = d[f'exit_{key}']
                if cnt == 0:
                    return f'{name}=0'
                pnl = d[f'pnl_{key}']
                avg = pnl / cnt
                return f'{name}={cnt} (PnL={pnl:+.0f}, avg={avg:+.0f})'

            unknown_part = ''
            if d['exit_unknown']:
                unknown_part = ', ' + _fmt_exit('unknown', 'unknown')

            print(
                f'  exits: {_fmt_exit("time", "time")}, '
                f'{_fmt_exit("opposite", "opposite")}, '
                f'{_fmt_exit("stop", "stop")}'
                f'{unknown_part} | '
                f'avg_bars_held={avg_bars:.1f}'
            )

        # === Контекст входа (средние DC, ATR, stop_distance) ===
        if total_entries > 0:
            avg_dc = d['dc_at_entry_sum'] / total_entries
            avg_atr = d['atr_at_entry_sum'] / total_entries
            avg_stop = d['stop_dist_sum'] / total_entries
            # Ожидаемый time-exit при средних DC и текущем коэффициенте
            expected_max_bars = int(self.p.time_exit_dc_mult * avg_dc) \
                if self.p.time_exit_dc_mult else 0
            print(
                f'  entry context: avg_DC={avg_dc:.1f}, avg_ATR={avg_atr:.4f}, '
                f'avg_stop_dist={avg_stop:.4f} '
                f'(=stop_dc_mult*DC*ATR), '
                f'expected_time_exit={expected_max_bars} bars'
            )

        # === Отказы ордеров (если есть) ===
        total_rejected = (d['rejected_entry'] + d['rejected_stop']
                          + d['rejected_close'])
        if total_rejected > 0:
            print(
                f'  REJECTED orders: '
                f'entry={d["rejected_entry"]}, '
                f'stop={d["rejected_stop"]}, '
                f'close={d["rejected_close"]} '
                f'-- ВНИМАНИЕ: проверьте checksubmit=False у broker.'
            )


# =============================================================================
# Запуск (тот же скелет, что и в prod11)
# =============================================================================

def main(maxcpus=None):
    start_cash = 300000.0

    # Дефолтный набор: фильтр в режиме 'direction' (компромиссный),
    # ATR-стоп и выход по сигналу активны, диагностика включена,
    # чтобы при подозрительно малом числе сделок видеть, кто кого режет.
    params = dict(  # CNY_1h - стартовый набор для prod12
        write_history=True,
        risk=5,
        window=45,
        bandwidth=0.3,
        thresh=-0.5,
        allow_short=True,
        printlog=False,

        # Фильтры из prod11 (нейтральные дефолты — выключены)
        min_dc=0,
        max_adx=999,

        # === Correlation Angle filter (prod12) ===
        # Режимы: 'off', 'direction', 'state', 'both'.
        # Для A/B-сравнения можно: corr_filter_mode=['off', 'direction', 'both']
        corr_filter_mode='off',
        corr_max_period=80,
        corr_min_period=6,
        corr_trend_threshold=9.0,

        # === ATR-стоп и выход (prod12) ===
        # stop_atr_period=14,
        # stop_dc_mult=0.4,              # для оптимизации: [i/10 for i in range(3, 9)]
        # time_exit_dc_mult=0.75,        # 0 отключает time-exit
        # exit_on_opposite_signal=True,

        # === ОПТИМИЗИРУЕМ ВЫХОДЫ ===
        stop_atr_period=14,
        stop_dc_mult=[i/10 for i in range(3, 16, 2)],          # 0.3, 0.5, 0.7, 0.9, 1.1, 1.3, 1.5
        time_exit_dc_mult=[0, 0.5, 0.75, 1.0, 1.5, 2.0],       # 0 = отключено
        exit_on_opposite_signal=[True, False],

        # === Диагностика ===
        # На stop() стратегии печатается, сколько базовых сигналов AutoTune
        # было, сколько отрезано Correlation Angle, сколько провалилось на
        # расчёте размера, и сколько в итоге стало сделок.
        # diag_print_filter_stats=True,
    )

    tf = '1h'
    start_date = '2023-6-20'
    end_date = datetime.today()
    main_opt_metric = 'PROM'

    sec = 'MIX'  #'CNY'
    total_time = _time.time()
    store = MoexStore()
    datas = list()

    contracts = store.futures.contracts_between(sec, start_date, end_date)
    print(contracts)

    variants = count_param_variants(params)
    sheet_size = (variants * len(contracts)) > 1048576
    if sheet_size:
        print(f"Excel sheet is too large! Your sheet size is: {variants * len(contracts)}, "
              f"Max sheet size is: 1'048'576")
    print(f'Рассчитываем {variants} вариантов стратегии для '
          f'каждого из {len(contracts)} контрактов. Итого '
          f'{variants * len(contracts)} вариантов.')
    print(f'Время пошло, {datetime.now():%H:%M:%S}')

    for contract in contracts:
        prevexpdate = pd.to_datetime(store.futures.prevexpdate(contract))
        if contract == contracts[0]:
            fromdate = pd.to_datetime(start_date) - timedelta(days=5)
        else:
            fromdate = prevexpdate - timedelta(days=5)
        todate = end_date if contract == contracts[-1] else store.futures.expdate(contract)

        data = store.getdata(sec_id=contract, fromdate=fromdate, todate=todate,
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
        # ВАЖНО: checksubmit=False отключает pre-submit margin-check у BackBroker.
        # Без этого закрывающие ордера (стопы и close-by-signal) часто
        # отвергаются с Margin-статусом, потому что брокер видит каждый ордер
        # как независимый и проверяет margin как для нового открытия позиции.
        # В prod11 эта проблема обходилась через `_checksubmit=False` на
        # bracket children. Здесь мы делаем то же самое глобально.
        cerebro.broker = bt.brokers.BackBroker(checksubmit=False)
        cerebro.broker.setcash(start_cash)
        cerebro.broker.addcommissioninfo(futures_comm[data.sec], name=data.p.name)
        # AllInSizer убран — расчёт размера выполняется в стратегии
        cerebro.addanalyzer(SmartAnalyzer, _name='full', **analyzer_params)
        cerebro.adddata(data)

        cerebro.optstrategy(AutoTuneFilterStrategy, **params)
        runs = cerebro.run(stdstats=False, tradehistory=params["write_history"], maxcpus=maxcpus)

        for run in runs:
            for strategy in run:
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
    df4 = pd.DataFrame(
        list(params.items()) + [('start_date', start_date), ('end_date', end_date)],
        columns=['Parameter', 'Value']
    )
    del df1['PNLs']

    timestamp = datetime.now().strftime("%d-%m-%y %H-%M")
    results_file = f'opt_results_prod12_{sec}_{tf}_{timestamp}.xlsx'

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
    available_cpus = maxcpus - 3
    print(f'Задействуем {available_cpus} потоков из {maxcpus} возможных.')
    main(available_cpus)
