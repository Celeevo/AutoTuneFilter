from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import os
import re
import math
import time as _time
from datetime import datetime, time, timedelta
import backtrader as bt
from moex_store import MoexStore
import gc
from backtrader import Analyzer
from math import sqrt
import numpy as np
import pandas as pd
from itertools import chain
from statistics import mean, stdev
import xlsxwriter
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

# =============================================================================
# DONCHIAN-TREND PROD1
# =============================================================================
# Trend-following стратегия:
#   - Вход long  : пробой Donchian-максимума предыдущих N баров,
#                  EMA(fast) > EMA(slow), ATR/close >= min_atr_pct,
#                  (опционально) фильтр Correlation Angle: state == +1.
#   - Вход short : симметрично.
#   - Выход      : chandelier trailing-stop (от high_since_entry на K*ATR)
#                  + reverse Donchian (пробой минимума последних M баров).
#   - Размер     : risk% от cash, делённый на расстояние initial-стопа.
#
# Инфраструктура (комиссии, аналитика, оптимизатор с xlsx-выводом)
# наследует структуру семейства autotune-prod11 — те же FuturesCommission /
# SmartAnalyzer / compute_group_metrics / main(). Расчёт размера позиции
# вынесен в стратегию, AllInSizer не используется.
# =============================================================================

FUTURE_TYPE = dict(
    currency=0.00462,
    percent=0.01650,
    stock=0.01980,
    xindex=0.00660,
    commodity=0.01320,
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


futures_comm = dict(
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
# Аналитика — без изменений от семейства prod11
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
# Correlation Angle Indicator (Эрлерс, фиксированный period)
# =============================================================================

class CorrelationAngleIndicator(bt.Indicator):
    """
    John Ehlers - Correlation Angle Indicator.
    По статье "Correlation as a Cycle Indicator".

    Линии:
    - real      : корреляция цены с Cosine-волной выбранного периода
    - imag      : корреляция цены с Negative Sine-волной того же периода
    - angle     : фазовый угол Phasor, диапазон примерно -180..+180
    - state     : 0 = cycle mode, +1 = trend up, -1 = trend down
                  (trend mode = фаза «залипает», т.е. |dangle| < trend_threshold)

    Для trend-following полезен именно state: state==+1 разрешает long,
    state==-1 разрешает short, state==0 — рынок в циклическом режиме,
    trend-стратегия в этой ситуации торговать не должна.
    """

    lines = ('real', 'imag', 'angle', 'state')

    params = (
        ('period', 14),
        ('trend_threshold', 9.0),  # порог Эрлерса (9° = 40-баровый цикл)
        ('eps', 1e-12),
    )

    plotinfo = dict(subplot=True, plotname='CorrAngle')

    plotlines = dict(
        real=dict(_plotskip=True),
        imag=dict(_plotskip=True),
        angle=dict(_name='Phase angle'),
        state=dict(_name='State'),
    )

    def __init__(self):
        if self.p.period < 2:
            raise ValueError('CorrelationAngleIndicator: period должен быть >= 2')
        self.addminperiod(int(self.p.period))

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
        period = int(self.p.period)

        prev_real = self._safe(self.l.real[-1], 0.0) if len(self) > 1 else 0.0
        prev_imag = self._safe(self.l.imag[-1], 0.0) if len(self) > 1 else 0.0
        prev_angle = self._safe(self.l.angle[-1], 0.0) if len(self) > 1 else 0.0

        real = self._correlate_with_wave(
            period,
            lambda ago, T: math.cos(math.radians(360.0 * ago / T)),
            fallback=prev_real,
        )
        imag = self._correlate_with_wave(
            period,
            lambda ago, T: -math.sin(math.radians(360.0 * ago / T)),
            fallback=prev_imag,
        )

        if abs(imag) > self.p.eps:
            angle_raw = 90.0 + math.degrees(math.atan(real / imag))
            if imag > 0.0:
                angle_raw -= 180.0
        else:
            angle_raw = prev_angle

        angle = angle_raw
        if len(self) > 1:
            if (prev_angle - angle < 270.0) and (angle < prev_angle):
                angle = prev_angle

        state = 0.0
        if len(self) > 1 and abs(angle - prev_angle) < float(self.p.trend_threshold):
            state = -1.0 if angle < 0.0 else 1.0

        self.l.real[0] = real
        self.l.imag[0] = imag
        self.l.angle[0] = angle
        self.l.state[0] = state


# =============================================================================
# Стратегия
# =============================================================================

class DonchianTrendStrategy(bt.Strategy):
    """
    Trend-following:
      Entry long  : close > Donchian-high(donchian_entry, shift -1)
                  + EMA(fast) > EMA(slow)
                  + ATR/close >= min_atr_pct
                  + Correlation Angle filter (опц.)
      Exit  long  : chandelier trailing-stop = max_high_since_entry - K*ATR
                  ИЛИ reverse Donchian: close < Donchian-low(donchian_exit, shift -1)
      Размер      : risk% от cash / расстояние initial-стопа, ограничен margin.
    """

    params = dict(
        write_history=None,
        risk=2,                        # риск на сделку, % от cash

        # === Donchian ===
        donchian_entry=20,             # период пробоя для входа
        donchian_exit=10,              # период пробоя для выхода (обычно меньше)

        # === EMA trend filter ===
        ema_fast=20,
        ema_slow=50,

        # === ATR ===
        atr_period=14,
        min_atr_pct=0.005,             # минимальный ATR/close для входа (0.005 = 0.5%)
        chandelier_atr_mult=3.0,       # K для chandelier trailing-stop

        # === Correlation Angle filter ===
        # 'off'       — фильтр выключен;
        # 'state'     — вход разрешён только при state == +1 (long) / -1 (short);
        # 'direction' — формальный режим по знаку angle. ВНИМАНИЕ: для
        #               trend-following осмысленность direction-фильтра низкая,
        #               использовать только для экспериментов;
        # 'both'      — state и direction одновременно.
        corr_filter_mode='off',
        corr_period=14,                # фиксированный период Cosine (Эрлерс, SPY)
        corr_trend_threshold=9.0,

        allow_short=True,
        printlog=False,
    )

    def log(self, txt):
        if self.p.printlog:
            dt = self.data.datetime.datetime(0)
            print(f'{dt} | {txt}')

    # ------------------------------------------------------------------------
    # __init__
    # ------------------------------------------------------------------------

    def __init__(self):
        # Donchian-каналы. Период entry для пробоя, период exit для разворота.
        # Используем shift -1, чтобы пробой сравнивался с каналом ПРОШЛОГО бара,
        # а не текущего (иначе сегодняшний экстремум сам в себя «пробивает»).
        self.don_high_entry = bt.indicators.Highest(self.data.high,
                                                    period=int(self.p.donchian_entry))
        self.don_low_entry = bt.indicators.Lowest(self.data.low,
                                                  period=int(self.p.donchian_entry))
        self.don_high_exit = bt.indicators.Highest(self.data.high,
                                                   period=int(self.p.donchian_exit))
        self.don_low_exit = bt.indicators.Lowest(self.data.low,
                                                 period=int(self.p.donchian_exit))

        # EMA-фильтр направления тренда
        self.ema_fast = bt.indicators.EMA(self.data.close, period=int(self.p.ema_fast))
        self.ema_slow = bt.indicators.EMA(self.data.close, period=int(self.p.ema_slow))

        # ATR для размера стопа, chandelier и фильтра волатильности
        self.atr = bt.indicators.ATR(self.data, period=int(self.p.atr_period))

        # Correlation Angle (всегда считается; применяется только если режим != 'off')
        self.corr = CorrelationAngleIndicator(
            self.data.close,
            period=int(self.p.corr_period),
            trend_threshold=float(self.p.corr_trend_threshold),
        )

        # Состояние сделки
        self.entry_order = None
        self.stop_order = None
        self.close_order = None
        self.entry_price = 0.0
        self.stop_loss_price = 0.0
        self.entry_isbuy = None        # True/False/None
        self.high_since_entry = 0.0    # для chandelier long
        self.low_since_entry = 0.0     # для chandelier short

    # ------------------------------------------------------------------------
    # Фильтр Correlation Angle
    # ------------------------------------------------------------------------

    def _phase_allows_long(self):
        mode = self.p.corr_filter_mode
        if mode == 'off':
            return True
        state = int(round(self.corr.state[0]))
        # Для trend long: state == +1 (фаза «залипает» в верхней полусфере).
        state_ok = (state == 1)
        # Direction для trend-стратегии — формальный режим (см. docstring).
        direction_ok = self.corr.angle[0] >= 0.0
        if mode == 'state':
            return state_ok
        if mode == 'direction':
            return direction_ok
        if mode == 'both':
            return state_ok and direction_ok
        return True

    def _phase_allows_short(self):
        mode = self.p.corr_filter_mode
        if mode == 'off':
            return True
        state = int(round(self.corr.state[0]))
        state_ok = (state == -1)
        direction_ok = self.corr.angle[0] <= 0.0
        if mode == 'state':
            return state_ok
        if mode == 'direction':
            return direction_ok
        if mode == 'both':
            return state_ok and direction_ok
        return True

    # ------------------------------------------------------------------------
    # Сигналы входа
    # ------------------------------------------------------------------------

    def _check_long_signal(self):
        # Пробой максимума предыдущих donchian_entry баров
        breakout = self.data.close[0] > self.don_high_entry[-1]
        # Тренд по EMA
        trend = self.ema_fast[0] > self.ema_slow[0]
        # Сила волатильности (мёртвые периоды отсекаем)
        atr_ok = self.atr[0] >= self.p.min_atr_pct * self.data.close[0]
        # Correlation Angle
        ca_ok = self._phase_allows_long()
        return breakout and trend and atr_ok and ca_ok

    def _check_short_signal(self):
        breakout = self.data.close[0] < self.don_low_entry[-1]
        trend = self.ema_fast[0] < self.ema_slow[0]
        atr_ok = self.atr[0] >= self.p.min_atr_pct * self.data.close[0]
        ca_ok = self._phase_allows_short()
        return breakout and trend and atr_ok and ca_ok

    # ------------------------------------------------------------------------
    # Расчёт размера позиции и initial-стопа
    # ------------------------------------------------------------------------

    def _round_price(self, comminfo, price, isbuy):
        if comminfo.p.cost_of_price_step != 0:
            return round_to_nearest_price_step(comminfo.p.cost_of_price_step, price, isbuy)
        return price

    def _calc_entry_params(self, isbuy):
        """
        Возвращает (size, entry_price, stop_loss_price) или None, если вход
        невозможен (нулевой ATR, недостаточно cash и т.п.).
        """
        comminfo = self.broker.getcommissioninfo(self.data)
        cash = self.broker.getcash()
        entry_price = self.data.close[0]
        atr_now = float(self.atr[0])
        if entry_price <= 0 or atr_now <= 0:
            return None

        stop_distance = float(self.p.chandelier_atr_mult) * atr_now
        if stop_distance <= 0:
            return None

        risk_money = cash * (self.p.risk / 100.0)
        denom = stop_distance * comminfo.p.mult
        if denom <= 0:
            return None
        size = int(risk_money / denom)

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

    # ------------------------------------------------------------------------
    # Управление ордерами
    # ------------------------------------------------------------------------

    def _reset_state(self):
        self.entry_order = None
        self.stop_order = None
        self.close_order = None
        self.entry_price = 0.0
        self.stop_loss_price = 0.0
        self.entry_isbuy = None
        self.high_since_entry = 0.0
        self.low_since_entry = 0.0

    def _has_active_orders(self):
        for o in (self.entry_order, self.close_order):
            if o is not None and o.alive():
                return True
        return False

    def _submit_entry(self, isbuy, params):
        size, entry_price, stop_price = params
        side = 'long' if isbuy else 'short'
        self.log(f'{side.upper()} SIGNAL | size={size} | initial stop={stop_price:.4f}')

        self.entry_price = entry_price
        self.stop_loss_price = stop_price
        self.entry_isbuy = isbuy

        method = self.buy if isbuy else self.sell
        order = method(size=size, exectype=bt.Order.Market)
        order.addinfo(name=side)
        self.entry_order = order

    def _place_stop_after_fill(self, executed_price):
        """После исполнения входа выставляем initial chandelier-стоп."""
        comminfo = self.broker.getcommissioninfo(self.data)
        isbuy = self.entry_isbuy
        atr_now = float(self.atr[0])
        if atr_now <= 0:
            self.log('WARNING: atr <= 0 на исполнении, stop не выставлен')
            return

        K = float(self.p.chandelier_atr_mult)
        direction = 1 if isbuy else -1
        raw_stop = executed_price - direction * K * atr_now
        self.stop_loss_price = self._round_price(comminfo, raw_stop, isbuy)

        size = abs(self.position.size)
        method = self.sell if isbuy else self.buy
        order = method(size=size, exectype=bt.Order.Stop, price=self.stop_loss_price)
        order.addinfo(name='stop')
        self.stop_order = order
        self.log(f'STOP placed at {self.stop_loss_price:.4f}')

        # Инициализируем экстремум для chandelier — отталкиваемся от цены входа
        if isbuy:
            self.high_since_entry = executed_price
        else:
            self.low_since_entry = executed_price

    def _update_trailing_stop(self):
        """Пересчитывает chandelier-стоп; двигает его только в сторону прибыли."""
        if self.stop_order is None or not self.stop_order.alive():
            return
        comminfo = self.broker.getcommissioninfo(self.data)
        atr_now = float(self.atr[0])
        if atr_now <= 0:
            return
        K = float(self.p.chandelier_atr_mult)

        if self.entry_isbuy:
            # Обновляем максимум с момента входа
            if self.data.high[0] > self.high_since_entry:
                self.high_since_entry = float(self.data.high[0])
            candidate = self.high_since_entry - K * atr_now
            new_stop = self._round_price(comminfo, candidate, isbuy=True)
            # Стоп двигаем ТОЛЬКО ВВЕРХ
            if new_stop > self.stop_loss_price:
                self._move_stop_to(new_stop)
        else:
            if self.data.low[0] < self.low_since_entry:
                self.low_since_entry = float(self.data.low[0])
            candidate = self.low_since_entry + K * atr_now
            new_stop = self._round_price(comminfo, candidate, isbuy=False)
            # Стоп двигаем ТОЛЬКО ВНИЗ
            if new_stop < self.stop_loss_price:
                self._move_stop_to(new_stop)

    def _move_stop_to(self, new_stop_price):
        """Отменяет текущий стоп и выставляет новый."""
        if self.stop_order is not None and self.stop_order.alive():
            self.cancel(self.stop_order)
        size = abs(self.position.size)
        method = self.sell if self.entry_isbuy else self.buy
        order = method(size=size, exectype=bt.Order.Stop, price=new_stop_price)
        order.addinfo(name='stop')
        self.stop_order = order
        self.stop_loss_price = new_stop_price
        self.log(f'STOP moved to {new_stop_price:.4f}')

    def _close_by_donchian(self):
        """Закрытие позиции при reverse-Donchian-пробое."""
        if self.position.size == 0:
            return
        if self.stop_order is not None and self.stop_order.alive():
            self.cancel(self.stop_order)
        size = abs(self.position.size)
        method = self.sell if self.position.size > 0 else self.buy
        order = method(size=size, exectype=bt.Order.Market)
        order.addinfo(name='close_donchian')
        self.close_order = order
        self.log(f'CLOSE by reverse Donchian at close={self.data.close[0]:.4f}')

    # ------------------------------------------------------------------------
    # Колбэки
    # ------------------------------------------------------------------------

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        order_name = getattr(order.info, 'name', None) or '?'
        is_entry = (order is self.entry_order)
        is_stop = (order is self.stop_order)
        is_close = (order is self.close_order)

        if order.status == order.Completed:
            if is_entry:
                self.log(f'ENTRY {order_name.upper()} EXECUTED at {order.executed.price:.4f}')
                self.entry_price = order.executed.price
                self.entry_order = None
                self._place_stop_after_fill(order.executed.price)
            elif is_stop:
                self.log(f'STOP HIT at {order.executed.price:.4f}')
                self._reset_state()
            elif is_close:
                self.log(f'CLOSE EXECUTED at {order.executed.price:.4f}')
                self._reset_state()
            else:
                self.log(f'WARNING: completed UNKNOWN order at '
                         f'{order.executed.price:.4f} (name={order_name!r})')
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log(f'ORDER {order_name} FAILED: {order.getstatusname()}')
            if is_entry:
                self._reset_state()
            elif is_stop:
                # Cancel ожидаем при перевыставлении стопа или при close_donchian.
                self.stop_order = None
            elif is_close:
                # Позиция осталась открытой — на следующем баре попробуем снова.
                self.close_order = None

    # ------------------------------------------------------------------------
    # next
    # ------------------------------------------------------------------------

    def next(self):
        # Позиция открыта: трейлим стоп и проверяем reverse Donchian
        if self.position:
            # Reverse Donchian exit имеет приоритет: если он сработал —
            # двигать стоп уже бессмысленно.
            isbuy = self.entry_isbuy
            if isbuy and self.data.close[0] < self.don_low_exit[-1]:
                self._close_by_donchian()
                return
            if (not isbuy) and self.data.close[0] > self.don_high_exit[-1]:
                self._close_by_donchian()
                return
            # Иначе подтягиваем chandelier-стоп
            self._update_trailing_stop()
            return

        # Позиции нет: ищем сигналы
        if self._has_active_orders():
            return

        if self._check_long_signal():
            params = self._calc_entry_params(isbuy=True)
            if params is not None:
                self._submit_entry(isbuy=True, params=params)
        elif self.p.allow_short and self._check_short_signal():
            params = self._calc_entry_params(isbuy=False)
            if params is not None:
                self._submit_entry(isbuy=False, params=params)


# =============================================================================
# main()
# =============================================================================

def main(maxcpus=None):
    start_cash = 300000.0

    # Стартовый набор. Скорее всего лучшие инструменты для trend-following —
    # это Si, Br, NG, RTS, GOLD; CNY как mean-reversion-инструмент сюда менее
    # пригоден, но оставлен для отправной точки.

    params = dict(  # MIX
        write_history=False,
        risk=5,
        donchian_entry=[20, 25, 30, 35],  #20,         # для оптимизации: [10, 15, 20, 25, 30, 40, 55]
        donchian_exit=[8, 10, 12, 14], #10,          # для оптимизации: [5, 7, 10, 14, 20]
        ema_fast=10,
        ema_slow=40,
        atr_period=[10, 12,14],
        min_atr_pct=[0.001, 0.00125, 0.0015, 0.00175, 0.002, 0.004], #[i/2000 for i in range(2, 11)], #0.005,         # для оптимизации: [i/1000 for i in range(2, 11)]
        chandelier_atr_mult=[i/4 for i in range(1, 8)], #3.0,   # для оптимизации: [i/2 for i in range(4, 9)]

        # === Correlation Angle filter ===
        # Для A/B-сравнения: corr_filter_mode=['off', 'state', 'both']
        corr_filter_mode='off',
        corr_period=14, #range(26,43,4), #14 # для оптимизации: [10, 14, 20, 28]

        allow_short=True,
        printlog=False,
    )
    # params = dict(  # RTS
    #     write_history=False,
    #     risk=5,
    #     donchian_entry=[45, 50, 55, 60, 65],  #20,         # для оптимизации: [10, 15, 20, 25, 30, 40, 55]
    #     donchian_exit=[6, 8, 10, 12], #10,          # для оптимизации: [5, 7, 10, 14, 20]
    #     ema_fast=10,
    #     ema_slow=40,
    #     atr_period=[8, 10, 12, 14, 16],
    #     min_atr_pct=[i/2000 for i in range(1, 5)], #0.005,         # для оптимизации: [i/1000 for i in range(2, 11)]
    #     chandelier_atr_mult=[i/2 for i in range(1, 13)], #3.0,   # для оптимизации: [i/2 for i in range(4, 9)]
    #
    #     # === Correlation Angle filter ===
    #     # Для A/B-сравнения: corr_filter_mode=['off', 'state', 'both']
    #     corr_filter_mode='off',
    #     corr_period=14,  #range(26,43,4), #14 # для оптимизации: [10, 14, 20, 28]
    #
    #     allow_short=True,
    #     printlog=False,
    # )

    tf = '1h'                      # для trend-following лучше старшие таймфреймы
    start_date = '2023-01-01'
    end_date = datetime.today()
    main_opt_metric = 'PROM'

    sec = 'MIX'#'RTS'#'Si'#'MIX'#
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
        # checksubmit=False — закрывающие ордера (стопы, trailing-перевыставление,
        # close_donchian) не отвергаются BackBroker'ом с ложным Margin-статусом.
        cerebro.broker = bt.brokers.BackBroker(checksubmit=False)
        cerebro.broker.setcash(start_cash)
        cerebro.broker.addcommissioninfo(futures_comm[data.sec], name=data.p.name)
        cerebro.addanalyzer(SmartAnalyzer, _name='full', **analyzer_params)
        cerebro.adddata(data)

        cerebro.optstrategy(DonchianTrendStrategy, **params)
        runs = cerebro.run(stdstats=False, tradehistory=params['write_history'], maxcpus=maxcpus)

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
            f'Прогон {len(runs)} вариантов для контракта {data.p.name} за '
            f'{round(_time.time() - st_time, 2)} сек., '
            f'V = {round(len(runs) / (_time.time() - st_time), 2)} вар/сек, '
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

    timestamp = datetime.now().strftime('%d-%m-%y %H-%M')
    # Версия из имени скрипта (regex 'prod\d+'); если не найдено — берём
    # полное имя файла без расширения.
    script_name = os.path.splitext(os.path.basename(__file__))[0]
    m = re.search(r'prod\d+', script_name)
    version_tag = m.group(0) if m else script_name
    results_file = f'opt_results_{version_tag}_{sec}_{tf}_{timestamp}.xlsx'

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
'''
    params = dict(  # Si
        write_history=False,
        risk=5,
        donchian_entry=[30, 40, 45, 50, 55],  #20,         # для оптимизации: [10, 15, 20, 25, 30, 40, 55]
        donchian_exit=[10, 12, 14, 16, 20], #10,          # для оптимизации: [5, 7, 10, 14, 20]
        ema_fast=10,
        ema_slow=40,
        atr_period=[6,8,10,12],
        min_atr_pct=[i/2000 for i in range(1, 3)], #0.005,         # для оптимизации: [i/1000 for i in range(2, 11)]
        chandelier_atr_mult=[i/2 for i in range(4, 9)], #3.0,   # для оптимизации: [i/2 for i in range(4, 9)]

        # === Correlation Angle filter ===
        # Для A/B-сравнения: corr_filter_mode=['off', 'state', 'both']
        corr_filter_mode='state',
        corr_period=range(26,43,4), #14,            # для оптимизации: [10, 14, 20, 28]

        allow_short=True,
        printlog=False,
    )
    
        params = dict(  # MIX
        write_history=False,
        risk=5,
        donchian_entry=[20, 25, 30, 35],  #20,         # для оптимизации: [10, 15, 20, 25, 30, 40, 55]
        donchian_exit=[8, 10, 12, 14], #10,          # для оптимизации: [5, 7, 10, 14, 20]
        ema_fast=10,
        ema_slow=40,
        atr_period=[10, 12,14],
        min_atr_pct=[i/2000 for i in range(1, 3)], #0.005,         # для оптимизации: [i/1000 for i in range(2, 11)]
        chandelier_atr_mult=[i/4 for i in range(1, 8)], #3.0,   # для оптимизации: [i/2 for i in range(4, 9)]

        # === Correlation Angle filter ===
        # Для A/B-сравнения: corr_filter_mode=['off', 'state', 'both']
        corr_filter_mode='state',
        corr_period=range(26,43,4), #14 # для оптимизации: [10, 14, 20, 28]

        allow_short=True,
        printlog=False,
    )
'''