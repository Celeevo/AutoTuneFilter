from itertools import chain
from math import sqrt
from statistics import mean

import backtrader as bt
from backtrader import Analyzer
import numpy as np
import pandas as pd

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


