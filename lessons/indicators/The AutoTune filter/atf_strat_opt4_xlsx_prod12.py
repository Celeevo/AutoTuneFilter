from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import os
import math
import time as _time
from datetime import datetime, time, timedelta
import backtrader as bt
from moex_store import MoexStore
import gc
from atf_new import AutoTuneFilter
from backtrader import Analyzer
from math import sqrt
import numpy as np
import pandas as pd
from itertools import chain
from statistics import mean, stdev
import xlsxwriter
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

# =============================================================================
# CHANGELOG prod12 vs prod11 (10-05-26)
# =============================================================================
# (1) Адаптивный фильтр Correlation Angle, период которого равен текущему
#     доминантному циклу (DC) AutoTune. Это убирает рассогласованность периодов
#     и заставляет оба индикатора смотреть на одну и ту же волну.
#
# (2) Фильтр согласия фаз: лонг разрешён только при state==0 и угле в восходящей
#     полусфере (-180..0), шорт — при state==0 и угле в нисходящей (0..+180).
#     Флаг use_corr_angle_filter позволяет включать/отключать фильтр для
#     честного A/B-сравнения на одних и тех же данных.
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
# Удалённые параметры:  tp_mult.
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
    текущему доминантному циклу AutoTune (DC), переданному через параметр
    dc_line. Это убирает рассогласованность периодов между двумя индикаторами:
    оба смотрят на одну и ту же волну.

    Линии:
    - real, imag : корреляции с Cosine и -Sine соответственно;
    - angle      : фазовый угол, диапазон примерно -180..+180 градусов;
    - state      : 0 = cycle mode, 1 = trend up, -1 = trend down.

    Параметры:
    - dc_line          : линия доминантного цикла (например, self.atf.dc);
    - max_period       : верхняя граница окна корреляции; задаёт minperiod
                         индикатора (нужно достаточно баров истории);
    - min_period       : нижняя граница для защиты от слишком коротких циклов;
    - trend_threshold  : порог из статьи (9° = 40-баровый цикл).
    """

    lines = ('real', 'imag', 'angle', 'state')

    params = (
        ('dc_line', None),
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
        if self.p.dc_line is None:
            raise ValueError('CorrelationAngleAdaptive: параметр dc_line обязателен')
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
        """Pearson correlation между Price и заданной волной wave_func."""
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
        # Берём текущий DC и приводим его к допустимому диапазону окна
        dc_now = self._safe(self.p.dc_line[0], self.p.min_period)
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
        use_corr_angle_filter=True,   # включает фильтр согласия фаз
        corr_max_period=80,           # верхняя граница окна корреляции
        corr_min_period=6,            # нижняя граница окна корреляции
        corr_trend_threshold=9.0,     # порог Эрлерса (9° = 40-баровый цикл)

        # === (prod12) Управление стопом и выходом ===
        stop_atr_period=14,           # период ATR
        stop_dc_mult=0.4,             # stop_distance = stop_dc_mult * DC * ATR
        time_exit_dc_mult=0.75,       # time-exit через time_exit_dc_mult*DC баров;
                                      #   0 или None отключает time-exit
        exit_on_opposite_signal=True, # выход по противоположному сигналу AutoTune
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

        # Correlation Angle с DC от AutoTune (всегда вычисляется,
        # применяется в зависимости от use_corr_angle_filter)
        self.corr = CorrelationAngleAdaptive(
            self.data.close,
            dc_line=self.atf.dc,
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
        self.entry_price = 0.0
        self.stop_loss_price = 0.0
        self.entry_bar = 0
        self.entry_dc = 0.0
        self.entry_isbuy = None  # True для long, False для short

    # -------------------------------------------------------------------------
    # Применение фильтра согласия фаз (prod12)
    # -------------------------------------------------------------------------

    def _phase_allows_long(self):
        """True, если Correlation Angle подтверждает направление long."""
        if not self.p.use_corr_angle_filter:
            return True
        if int(round(self.corr.state[0])) != 0:
            return False  # трендовый режим — не входим
        # Восходящая фаза: angle в (-180, 0)
        return -180.0 < self.corr.angle[0] < 0.0

    def _phase_allows_short(self):
        """True, если Correlation Angle подтверждает направление short."""
        if not self.p.use_corr_angle_filter:
            return True
        if int(round(self.corr.state[0])) != 0:
            return False
        # Нисходящая фаза: angle в [0, 180)
        return 0.0 <= self.corr.angle[0] < 180.0

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
        self.entry_price = 0.0
        self.stop_loss_price = 0.0
        self.entry_bar = 0
        self.entry_dc = 0.0
        self.entry_isbuy = None

    def _submit_entry(self, isbuy):
        params = self._calc_entry_params(isbuy)
        if params is None:
            return
        size, entry_price, stop_price = params

        side = 'long' if isbuy else 'short'
        self.log(f'{side.upper()} SIGNAL | size={size} | stop={stop_price:.4f}')

        # Фиксируем планируемые значения, чтобы при исполнении выставить стоп
        self.entry_price = entry_price
        self.stop_loss_price = stop_price
        self.entry_isbuy = isbuy
        self.entry_dc = float(self.atf.dc[0])

        method = self.buy if isbuy else self.sell
        self.entry_order = method(
            size=size,
            exectype=bt.Order.Market,
            oargs={'name': side},
        )

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
        self.stop_order = stop_method(
            size=size,
            exectype=bt.Order.Stop,
            price=self.stop_loss_price,
            oargs={'name': 'stop_loss'},
        )
        self.log(f'STOP placed at {self.stop_loss_price:.4f}')

    def _close_position_by_signal(self, reason):
        """Закрытие позиции рыночным ордером, отмена защитного стопа."""
        if self.stop_order is not None and self.stop_order.alive():
            self.cancel(self.stop_order)
        self.log(f'CLOSE by {reason} at close={self.data.close[0]:.4f}')
        self.close()

    # -------------------------------------------------------------------------
    # Колбэки
    # -------------------------------------------------------------------------

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        order_name = getattr(order.info, 'name', None)

        if order.status == order.Completed:
            if order_name in ('long', 'short'):
                self.log(f'ENTRY {order_name.upper()} EXECUTED at {order.executed.price:.4f}')
                self.entry_price = order.executed.price
                self.entry_bar = len(self)
                # Выставляем защитный стоп
                self._submit_stop_after_fill(order.executed.price)
                self.entry_order = None
            elif order_name == 'stop_loss':
                self.log(f'STOP LOSS HIT at {order.executed.price:.4f}')
                self._reset_state()
            else:
                # Закрытие по сигналу (без явного name) — close()
                self.log(f'POSITION CLOSED at {order.executed.price:.4f}')
                self._reset_state()
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log(f'ORDER {order_name} FAILED: {order.getstatusname()}')
            if order_name in ('long', 'short'):
                self._reset_state()
            elif order_name == 'stop_loss':
                self.stop_order = None

    def next(self):
        # Если есть позиция — следим за условиями выхода
        if self.position:
            # Time-based выход
            if self.p.time_exit_dc_mult and self.entry_dc > 0:
                bars_held = len(self) - self.entry_bar
                max_bars = int(self.p.time_exit_dc_mult * self.entry_dc)
                if max_bars > 0 and bars_held >= max_bars:
                    self._close_position_by_signal(reason=f'time-exit ({bars_held} bars)')
                    return
            # Выход по противоположному сигналу AutoTune
            if self.p.exit_on_opposite_signal:
                if self.entry_isbuy is True and self.cross_down[0]:
                    self._close_position_by_signal(reason='opposite signal (cross_down)')
                    return
                if self.entry_isbuy is False and self.cross_up[0]:
                    self._close_position_by_signal(reason='opposite signal (cross_up)')
                    return
            return  # пока в позиции — новых входов не делаем

        # Нет позиции и нет ожидающих ордеров — проверяем сигналы на вход
        if self._is_busy():
            return

        if self._long_base[0] and self._phase_allows_long():
            self._submit_entry(isbuy=True)
        elif self.p.allow_short and self._short_base[0] and self._phase_allows_short():
            self._submit_entry(isbuy=False)


# =============================================================================
# Запуск (тот же скелет, что и в prod11)
# =============================================================================

def main(maxcpus=None):
    start_cash = 300000.0

    # Дефолтный набор: фильтры включены, ATR-стоп и выход по сигналу активны.
    # Для оптимизации — заменяйте отдельные значения на range/list.
    params = dict(  # CNY_1h - стартовый набор для prod12
        write_history=True,
        risk=5,
        window=28,
        bandwidth=0.46,
        thresh=-0.68,
        allow_short=True,
        printlog=False,

        # Фильтры из prod11 (нейтральные дефолты — выключены)
        min_dc=0,
        max_adx=999,

        # === Correlation Angle filter (prod12) ===
        use_corr_angle_filter=False,    # для A/B-сравнения можно [True, False]
        corr_max_period=80,
        corr_min_period=6,
        corr_trend_threshold=9.0,

        # === ATR-стоп и выход (prod12) ===
        stop_atr_period=14,
        stop_dc_mult=0.4,              # для оптимизации: [i/10 for i in range(3, 9)]
        time_exit_dc_mult=0,  #0.75,        # 0 отключает time-exit
        exit_on_opposite_signal=False,
    )

    tf = '1h'
    start_date = '2025-6-20'
    end_date = datetime.today()
    main_opt_metric = 'PROM'

    sec = 'CNY'
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
        cerebro.broker = bt.brokers.BackBroker()
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
