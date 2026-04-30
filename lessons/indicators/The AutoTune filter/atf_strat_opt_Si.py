from __future__ import absolute_import, division, print_function, unicode_literals

import os
import subprocess
import sys
import time as _time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from statistics import mean, stdev

import backtrader as bt
import gc
import pandas as pd
from backtrader import Analyzer
from moex_store import MoexStore

from atf import AutoTuneFilter


# ------------------------------------------------------------
# 1. Настройка комиссии и округления цены
# ------------------------------------------------------------

class FuturesCommission(bt.CommInfoBase):
    """Комиссия для фьючерсов MOEX."""
    params = dict(moexcomm=0.0, step_of_cost=0)

    def _getcommission(self, size, price, pseudoexec):
        broker_fee = abs(size) * self.p.commission
        exchange_fee = abs(size) * price * self.p.mult * self.p.moexcomm / 100
        return broker_fee + exchange_fee


futures_comm = FuturesCommission(
    commission=2.0,  # 2 руб за контракт
    margin=11934,  # ГО  30-04-26
    mult=1,  # мультипликатор
    moexcomm=0.00462,
    step_of_cost=1  # шаг цены)
)

def round_to_nearest_price_step(step, value, isbuy):
    """
    Округляет цену к шагу инструмента.

    Для покупок округляем вниз, для продаж — вверх.
    """
    if step <= 0:
        raise ValueError('step должен быть > 0')

    step_d = Decimal(str(step))
    value_d = Decimal(str(value))
    steps_cnt = value_d / step_d
    rounding_mode = ROUND_FLOOR if isbuy else ROUND_CEILING
    steps_cnt = steps_cnt.to_integral_value(rounding=rounding_mode)
    return float(steps_cnt * step_d)


# ------------------------------------------------------------
# 2. Сайзер: вход на максимум по ГО + расчёт стопа
# ------------------------------------------------------------

class AllInSizer(bt.Sizer):
    """
    Берёт максимально возможный размер позиции по ГО.

    Заодно считает цену стоп-лосса из заданного риска в %.
    """

    def _getsizing(self, comminfo, cash, data, isbuy):
        # Для фьючерсов размер позиции ограничен ГО.
        if comminfo.p.margin:
            max_size = cash / comminfo.p.margin
        else:
            max_size = cash / self.strategy.entry_price

        size = int(max_size) - 1
        if size <= 0:
            return 0

        # direction = 1 для long, -1 для short
        direction = 2 * isbuy - 1
        stop_loss_price = (
            self.strategy.entry_price
            - direction * cash * (self.strategy.p.risk / 100) / (size * comminfo.p.mult)
        )

        # Округляем цену стопа к шагу инструмента
        if comminfo.p.step_of_cost != 0:
            self.strategy.stop_loss_price = round_to_nearest_price_step(
                comminfo.p.step_of_cost,
                stop_loss_price,
                isbuy,
            )
        else:
            self.strategy.stop_loss_price = stop_loss_price

        return size


# ------------------------------------------------------------
# 3. Вспомогательные функции для оптимизации
# ------------------------------------------------------------

def iterable_params(params_dict):
    """Возвращает только те параметры, которые реально оптимизируются."""
    return [k for k, v in params_dict.items() if isinstance(v, (list, tuple, set, range))]


def count_param_variants(params_dict):
    variants = 1
    for value in params_dict.values():
        if isinstance(value, (list, tuple, set, range)):
            variants *= len(value)
    return variants


def get_strategy_params(params_dict, strategy_cls):
    strategy_param_names = strategy_cls.params._getkeys()
    return {name: value for name, value in params_dict.items() if name in strategy_param_names}


def calculate_sharpe_and_drawdown(trade_pnls, starting_cash):
    """
    Считает коэф-т Шарпа и max просадку по последовательности сделок.

    Sharpe:
    - берем доходность каждой сделки как pnl / equity_before_trade
    - используем безрисковую ставку = 0
    - это версия "по сделкам", а не годовая версия коэффициента Шарпа

    Max Drawdown:
    - строим кривую капитала
    - ищем максимальное падение от предыдущего пика
    - возвращаем просадку в процентах
    """
    if not trade_pnls:
        return 0.0, 0.0

    equity = starting_cash
    equity_curve = [equity]
    trade_returns = []

    for pnl in trade_pnls:
        equity_before_trade = equity

        if equity_before_trade != 0:
            trade_returns.append(pnl / equity_before_trade)

        equity += pnl
        equity_curve.append(equity)

    if len(trade_returns) >= 2:
        std_ret = stdev(trade_returns)
        sharpe = (mean(trade_returns) / std_ret) * (len(trade_returns) ** 0.5) if std_ret != 0 else 0.0
    else:
        sharpe = 0.0

    peak = equity_curve[0]
    max_drawdown = 0.0

    for equity_value in equity_curve:
        if equity_value > peak:
            peak = equity_value

        if peak > 0:
            drawdown = (peak - equity_value) / peak * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown

    return round(sharpe, 2), round(max_drawdown, 2)



def aggregate_results(df, starting_cash):
    """
    Агрегирует результаты по вариантам параметров.

    Каждая строка df — это результат одного прогона стратегии
    на одном фьючерсном контракте.

    Оптимизация запускается отдельно для каждого фьючерсного контракта,
    но набор параметров у стратегии один и тот же.
    Поэтому итог надо считать не по контрактам, а по варианту параметров.
    Здесь мы:
    1. группируем строки по варианту параметров,
    2. объединяем сделки из всех контрактов,
    3. считаем общую статистику по этому варианту.
    """
    params_col = df.columns[0]
    rows = []

    # groupby собирает вместе все строки с одинаковым вариантом параметров
    for params_value, group in df.groupby(params_col, sort=False):
        # Здесь соберём PnL всех сделок по всем контрактам
        # для одного варианта параметров
        all_trade_pnls = []

        # В каждой строке group['PNLs'] лежит список сделок
        # одного конкретного прогона / контракта
        for trade_list in group['PNLs']:
            all_trade_pnls.extend(trade_list)

        # Делим все сделки на прибыльные и убыточные
        win_trades = [p for p in all_trade_pnls if p >= 0]
        loss_trades = [p for p in all_trade_pnls if p < 0]

        # Общий итог по всем сделкам этого варианта параметров
        pnl = sum(all_trade_pnls)

        # Количество сделок
        win_count = len(win_trades)
        loss_count = len(loss_trades)

        # Суммы и средние
        sum_win = sum(win_trades)
        sum_loss = sum(loss_trades)
        avg_win = mean(win_trades) if win_count else 0.0
        avg_loss = mean(loss_trades) if loss_count else 0.0

        # Отношение прибыльных сделок к убыточным
        ratio = round(win_count / loss_count, 2) if loss_count else None

        # Профит-фактор = сумма прибыльных / модуль суммы убыточных
        pf = round(sum_win / abs(sum_loss), 2) if sum_loss else None

        # Коэфициент Шарпа и просадка
        sharpe, max_dd = calculate_sharpe_and_drawdown(all_trade_pnls, starting_cash)

        # Добавляем одну строку в итоговую таблицу
        rows.append({
            'Вариант параметров': params_value,
            'PnL': round(pnl, 2),
            'Приб. сделок': win_count,
            'Убыт. сделок': loss_count,
            'Сумма приб. сделок': round(sum_win, 2),
            'Сумма убыт. сделок': round(sum_loss, 2),
            'Отношение': ratio,
            'PF': pf,
            'Sharpe': sharpe,
            'Просадка %': max_dd,
            'Средн. приб. сделка': round(avg_win, 2),
            'Средн. убыточ. сделка': round(avg_loss, 2),
        })

    table = pd.DataFrame(rows)
    return table.sort_values(by='PnL', ascending=False).reset_index(drop=True)


def save_and_open_csv(table, sec, tf):
    """
    Сохраняет итоговую таблицу в CSV и пытается открыть файл
    в приложении, которое связано с CSV в операционной системе.
    """
    timestamp = datetime.now().strftime('%d-%m-%y_%H-%M-%S')
    csv_file = f'opt_results_{sec}_{tf}_{timestamp}.csv'

    # utf-8-sig помогает Excel корректно показать русские заголовки
    table.to_csv(
        csv_file,
        index=False,
        encoding='utf-8-sig',
        sep=';',
        decimal=','
    )
    abs_path = os.path.abspath(csv_file)
    print(f"\nРезультаты сохранены в CSV: {abs_path}")

    try:
        if os.name == 'nt':
            os.startfile(abs_path)
        elif sys.platform == 'darwin':
            subprocess.run(['open', abs_path], check=False)
        else:
            subprocess.run(['xdg-open', abs_path], check=False)
    except Exception as exc:
        print(f"Не удалось автоматически открыть файл: {exc}")
        print('Откройте CSV-файл вручную в Excel или другом табличном редакторе.')

    return abs_path


# ------------------------------------------------------------
# 4. Анализатор: сохраняем итоги одного прогона стратегии
# ------------------------------------------------------------

class SmartAnalyzer(Analyzer):
    """
    Сохраняет простые метрики одного прогона стратегии.

    На этом этапе мы ещё НЕ агрегируем результаты между контрактами.
    Мы просто запоминаем статистику для одного запуска стратегии.
    """

    params = dict(it_params=None)

    def __init__(self):
        self.win_pnls = []
        self.loss_pnls = []

    def notify_trade(self, trade):
        if trade.isclosed:
            if trade.pnlcomm >= 0:
                self.win_pnls.append(trade.pnlcomm)
            else:
                self.loss_pnls.append(trade.pnlcomm)

    def stop(self):
        strategy_params = self.strategy.p._getkwargs()
        if self.p.it_params:
            params_head = '-'.join(str(name) for name in self.p.it_params)
            params_str = '-'.join(str(strategy_params[name]) for name in self.p.it_params)
        else:
            params_head = 'params'
            params_str = 'default'

        self.rets[params_head] = params_str
        self.rets['PNL'] = int(sum(self.win_pnls + self.loss_pnls))
        self.rets['WinTr'] = len(self.win_pnls)
        self.rets['LossTr'] = len(self.loss_pnls)
        self.rets['SumWin'] = sum(self.win_pnls)
        self.rets['SumLoss'] = sum(self.loss_pnls)
        self.rets['AvgWin'] = mean(self.win_pnls) if self.win_pnls else 0.0
        self.rets['AvgLoss'] = mean(self.loss_pnls) if self.loss_pnls else 0.0

    def get_trades_pnl(self):
        return self.win_pnls + self.loss_pnls


# ------------------------------------------------------------
# 5. Стратегия
# ------------------------------------------------------------

class AutoTuneFilterStrategy(bt.Strategy):
    """
    Пример стратегии по статье Financial Hacker:
    - ROC = BP - BP[2]
    - Long  when ROC crosses above 0 and MinCorr < Thresh
    - Short when ROC crosses below 0 and MinCorr < Thresh and Filt > 0

    Выход:
    - по stop-loss
    - или по take-profit = tp_mult * R
    """

    params = dict(
        risk=None,
        window=26,
        bandwidth=0.22,
        thresh=-0.22,
        allow_short=True,
        tp_mult=2.0,
    )

    def __init__(self):
        self.atf = AutoTuneFilter(
            self.data.close,
            window=self.p.window,
            bandwidth=self.p.bandwidth,
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

    def _round_exit_price(self, price, isbuy):
        comminfo = self.broker.getcommissioninfo(self.data)
        if comminfo.p.step_of_cost == 0:
            return price

        return round_to_nearest_price_step(
            comminfo.p.step_of_cost,
            price,
            isbuy,
        )

    def _reset_exit_state(self):
        self.stop_order = None
        self.take_profit_order = None
        self.stop_loss_price = 0.0
        self.take_profit_price = 0.0
        self.entry_price = 0.0

    def _place_exit_orders(self, order):
        is_long_entry = order.isbuy()
        exit_method = self.sell if is_long_entry else self.buy
        exit_size = abs(order.executed.size)
        executed_entry = order.executed.price
        risk_points = abs(executed_entry - self.stop_loss_price)
        direction = 1 if is_long_entry else -1
        raw_take_profit = executed_entry + direction * self.p.tp_mult * risk_points

        self.take_profit_price = self._round_exit_price(raw_take_profit, is_long_entry)
        self.stop_order = exit_method(
            exectype=bt.Order.Stop,
            size=exit_size,
            price=self.stop_loss_price,
            name='stop_loss',
        )
        self.take_profit_order = exit_method(
            exectype=bt.Order.Limit,
            size=exit_size,
            price=self.take_profit_price,
            name='take_profit',
            oco=self.stop_order,
        )

    def notify_order(self, order):
        if order.status in (order.Submitted, order.Accepted):
            return

        if order.status == order.Completed:
            # После входа сразу ставим stop-loss и take-profit
            if order.info.name in ('long', 'short'):
                self._place_exit_orders(order)

            elif order.info.name in ('stop_loss', 'take_profit'):
                self._reset_exit_state()

        elif order.status in (order.Canceled, order.Margin, order.Rejected):
            if order == self.stop_order:
                self.stop_order = None
            elif order == self.take_profit_order:
                self.take_profit_order = None

        self.order = None

    def next(self):
        if self.order:
            return

        long_signal = self.long_signal[0]
        short_signal = self.short_signal[0]

        # Новые входы только если позиции нет
        if not self.position:
            if long_signal:
                self.entry_price = self.data.close[0]
                self.order = self.buy(name='long')
            elif self.p.allow_short and short_signal:
                self.entry_price = self.data.close[0]
                self.order = self.sell(name='short')


# ------------------------------------------------------------
# 6. Основная функция оптимизации
# ------------------------------------------------------------

def main(maxcpus=None):
    # Здесь задаём параметры оптимизации.
    start_cash = 300000.0
    params = dict(
        risk=5,
        window=range(42,45),
        bandwidth=[i / 100 for i in range(20, 37, 2)], #[0.4, 0.45, 0.45],
        thresh=[-i / 100 for i in range(50, 61, 2)], #[-0.45, -0.5, -0.55],
        allow_short=True,
        tp_mult=[i / 10 for i in range(21, 24)],  #[1.7, 1.8, 1.9, 2],
    )

    total_time = _time.time()
    tf = '1h'
    start_date = '2025-6-20'
    end_date = datetime.today()

    # В примере используем фьючерсы на индекс Московской биржи.
    sec = 'Si'
    store = MoexStore()
    datas = []

    # --------------------------------------------------------
    # Сначала собираем данные по всем контрактам фьючерса
    # --------------------------------------------------------
    contracts = store.futures.contracts_between(sec, start_date, end_date)
    print(contracts)

    variants = count_param_variants(params)

    print(f'Рассчитываем {variants} вариантов стратегии для '
          f'каждого из {len(contracts)} контрактов. Итого '
          f'{variants * len(contracts)} вариантов.')
    print(f'Время пошло, {str(datetime.now().time())[:8]}')

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
        datas.append(data)

    # --------------------------------------------------------
    # Теперь запускаем оптимизацию отдельно для каждого контракта
    # и собираем результаты в один список results
    # --------------------------------------------------------
    results = []
    analyzer_params = dict(it_params=iterable_params(params))
    strategy_params = get_strategy_params(params, AutoTuneFilterStrategy)

    for data in datas:
        st_time = _time.time()

        cerebro = bt.Cerebro()
        cerebro.broker = bt.brokers.BackBroker()
        cerebro.broker.setcash(start_cash)
        cerebro.broker.addcommissioninfo(futures_comm, name=data.p.name)
        cerebro.addsizer(AllInSizer)
        cerebro.addanalyzer(SmartAnalyzer, _name='full', **analyzer_params)
        cerebro.adddata(data)
        cerebro.optstrategy(AutoTuneFilterStrategy, **strategy_params)

        runs = cerebro.run(stdstats=False, tradehistory=False, maxcpus=maxcpus)

        for run in runs:
            for strategy in run:
                analyzer = strategy.analyzers.full
                analysis = dict(analyzer.get_analysis())
                analysis['PNLs'] = analyzer.get_trades_pnl()
                analysis['Data'] = data.p.name
                results.append(analysis)

        elapsed = _time.time() - st_time
        speed = round(len(runs) / elapsed, 2) if elapsed else 0.0
        print(
            f'Прогон {len(runs)} вариантов стратегии для контракта {data.p.name} '
            f'за {round(elapsed, 2)} сек., '
            f'V = {speed} вар/сек'
        )
        gc.collect()

    print(
        f'Весь прогон за {round(_time.time() - total_time, 2)} сек., '
        f'{round((_time.time() - total_time) / 3600, 2)} часов.'
    )

    # --------------------------------------------------------
    # Превращаем results в DataFrame, агрегируем по параметрам,
    # сохраняем в CSV и открываем файл в ассоциированном приложении
    # --------------------------------------------------------
    df = pd.DataFrame(results).round(2)
    table = aggregate_results(df, start_cash)
    save_and_open_csv(table, sec, tf)


if __name__ == '__main__':
    cpu_count = os.cpu_count() or 1
    maxcpus = max(1, cpu_count - 2)
    print(f'Задействуем {maxcpus} потоков из {cpu_count} возможных.')
    main(maxcpus)
