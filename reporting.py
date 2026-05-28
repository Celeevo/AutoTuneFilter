from itertools import chain
from math import sqrt
from statistics import mean

import backtrader as bt
from backtrader import Analyzer
import numpy as np
import pandas as pd


def compute_group_metrics(group, startingcash=1):
    """Собирает итоговые метрики по группе строк с одинаковым набором параметров."""
    pnls = list(chain.from_iterable(group['PNLs']))
    win_pnls = [value for value in pnls if value >= 0]
    loss_pnls = [value for value in pnls if value < 0]

    pnl = sum(pnls)
    win_count = len(win_pnls)
    loss_count = len(loss_pnls)
    sum_win = sum(win_pnls)
    sum_loss = sum(loss_pnls)
    avg_win = np.mean(win_pnls) if win_count else 0
    avg_loss = np.mean(loss_pnls) if loss_count else 0
    win_minus_loss = win_count - loss_count
    win_div_loss = win_count / loss_count if loss_count else 0
    stdev = np.std(group['PNL'], ddof=1) if len(group) > 1 else 0

    max_dd_pct = group['MaxDDPct'].max() if 'MaxDDPct' in group.columns else 0
    max_dd_money = group['MaxDDMoney'].max() if 'MaxDDMoney' in group.columns else 0
    max_dd_len = group['MaxDDLen'].max() if 'MaxDDLen' in group.columns else 0

    last_pnl = group['PNL'].iloc[-1]
    pre_last_pnl = group['PNL'].iloc[-2] if len(group) >= 2 else 0

    profit_factor = -sum_win / sum_loss if sum_loss else 0
    prom = (avg_win * (win_count - sqrt(win_count)) + avg_loss * (loss_count + sqrt(loss_count))) / startingcash

    neg_pnls = (group['PNL'] < 0).sum()
    last4neg = (group['PNL'].iloc[-4:] < 0).sum()

    return pd.Series({
        'PNL': pnl,
        'WinTr': win_count,
        'LossTr': loss_count,
        'SumWin': sum_win,
        'SumLoss': sum_loss,
        'W-L': win_minus_loss,
        'W/L': win_div_loss,
        'AvgWin': avg_win,
        'AvgLoss': avg_loss,
        'StdDev': stdev,
        'LastPNL': last_pnl,
        'PreLastPNL': pre_last_pnl,
        'MaxDDPct': max_dd_pct,
        'MaxDDMoney': max_dd_money,
        'MaxDDLen': max_dd_len,
        'PF': profit_factor,
        'PROM': prom,
        'NegPNLs': neg_pnls,
        'Last4Neg': last4neg,
    })


def aggregate_df(df, startingcash=1, sort_by='PROM', sort_by_second='PNL'):
    """Агрегирует строки по первому столбцу с параметрами и сортирует итоговую таблицу."""
    first_col = df.columns[0]
    metric_cols = ['PNLs', 'PNL']

    for col in ('MaxDDPct', 'MaxDDMoney', 'MaxDDLen'):
        if col in df.columns:
            metric_cols.append(col)

    aggr = (
        df.groupby(first_col, sort=False)[metric_cols]
        .apply(compute_group_metrics, startingcash=startingcash)
        .reset_index()
    )

    if sort_by not in aggr.columns:
        sort_by = sort_by_second if sort_by_second in aggr.columns else 'PNL'

    aggr = aggr.sort_values(sort_by, ascending=False)
    return aggr.round(2)


def add_drawdown_metrics(strategy, analysis):
    """Добавляет в строку результата просадку из bt.analyzers.DrawDown."""
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
    Возвращает основные метрики стратегии, trade-book и журнал финальных статусов ордеров.
    """

    params = dict(it_params=None, asset=None)

    def __init__(self):
        self.pt_arr = []
        self.lt_arr = []
        self.trades_details = []
        self.orders_details = []
        self.depos = self.strategy.broker.startingcash

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

    def notify_order(self, order):

        if not self.strategy.p.write_history:
            return

        if order.status in [order.Submitted, order.Accepted]:
            return

        order_name = getattr(order.info, 'name', '')
        parent = getattr(order, 'parent', None)
        parent_ref = getattr(parent, 'ref', '') if parent is not None else ''

        try:
            current_dt = self.strategy.data.datetime.datetime(0)
            current_dt_str = f'{current_dt:%d.%m.%y %H:%M}'
        except Exception:
            current_dt_str = ''

        try:
            position_size = self.strategy.getposition(order.data).size
        except Exception:
            position_size = 0

        try:
            data_name = order.data.p.name
        except Exception:
            data_name = ''

        self.orders_details.append(dict(
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
        ))

    def notify_trade(self, trade):
        if not trade.isclosed:
            return

        if trade.pnlcomm >= 0:
            self.pt_arr.append(trade.pnlcomm)
        else:
            self.lt_arr.append(trade.pnlcomm)

        finish_cash = float(self.strategy.broker.getcash())

        if self.strategy.p.write_history:
            entry_event = trade.history[0].event
            exit_event = trade.history[-1].event
            entry_order = entry_event.order
            exit_order = exit_event.order

            self.trades_details.append(dict(
                params=self._params_str(),
                sec=trade.getdataname(),
                entry_ref=entry_order.ref,
                entry_created_date=f'{bt.num2date(entry_order.created.dt):%d.%m.%y}',
                entry_created_time=f'{bt.num2date(entry_order.created.dt):%H:%M}',
                entry_executed_time=f'{bt.num2date(entry_order.executed.dt):%H:%M}',
                entry_requested_price=entry_order.created.price,
                entry_executed_price=entry_order.executed.price,
                stop_loss_price=getattr(
                    entry_order.info,
                    'planned_stop_loss_price',
                    getattr(self.strategy, 'stop_loss_price', 0),
                ),
                take_profit_price=getattr(
                    entry_order.info,
                    'planned_take_profit_price',
                    getattr(self.strategy, 'take_profit_price', 0),
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
                cash_before=self.depos,
                cash_after=finish_cash,
            ))

        self.depos = finish_cash

    def stop(self):
        st_params = self.strategy.p._getkwargs()
        params_head = '-'.join(str(k) for k in st_params.keys() if k in self.p.it_params) + '-asset'
        params_str = self._params_str()

        wt = len(self.pt_arr)
        aw = mean(self.pt_arr) if wt else 0
        lt = len(self.lt_arr)
        al = mean(self.lt_arr) if lt else 0
        swt = sum(self.pt_arr)
        slt = sum(self.lt_arr)
        pnl = int(sum(self.pt_arr + self.lt_arr))
        start_cash = float(getattr(self.strategy.broker, 'startingcash', self.depos))
        end_cash = float(self.strategy.broker.getcash())
        end_value = float(self.strategy.broker.getvalue())

        self.rets[params_head] = params_str
        self.rets['StartCash'] = start_cash
        self.rets['EndCash'] = end_cash
        self.rets['EndValue'] = end_value
        self.rets['ContractPNL'] = end_value - start_cash
        self.rets['PNL'] = pnl
        self.rets['WinTr'] = wt
        self.rets['LossTr'] = lt
        self.rets['SumWin'] = swt
        self.rets['SumLoss'] = slt
        self.rets['AvgWin'] = aw
        self.rets['AvgLoss'] = al

    def get_trades(self):
        return self.trades_details

    def get_orders(self):
        return self.orders_details

    def get_trades_pnl(self):
        return self.pt_arr + self.lt_arr
