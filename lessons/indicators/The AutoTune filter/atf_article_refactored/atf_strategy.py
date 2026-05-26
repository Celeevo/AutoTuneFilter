import backtrader as bt

from atf_indicator import AutoTuneFilter
from moex_setup import round_to_nearest_price_step


# Внутреннее защитное правило: стратегия закрывает позицию за 3 бара
# до конца текущего data feed. Для фьючерса это конец контракта,
# для акции — конец выбранной истории. Это не параметр оптимизации.
DATA_END_EXIT_BARS = 3


class EquityDrawDownIndicator(bt.Indicator):
    """
    Equity PnL и Drawdown для вывода в CerebroView.

    Это не торговый индикатор и не участвует в сигналах стратегии.
    Он создаётся только в режиме run_single_plot.py и обновляется один раз
    на каждом баре основного источника данных. Поэтому значения Equity/DD
    идут в том же таймфрейме, что и основной график стратегии.

    Линии настроены как bar/histogram: equity_pnl показывает изменение
    стоимости счёта относительно стартового капитала, drawdown показывает
    текущую просадку от максимума стоимости счёта.
    """

    lines = ('equity_pnl', 'drawdown')

    params = (
        ('start_value', None),
    )

    plotinfo = dict(
        subplot=True,
        plotname='Equity / DD',
    )

    # _method='bar' — стандартная для Backtrader подсказка отрисовщику:
    # показывать линию не обычной кривой, а столбцами. CerebroView должен
    # читать эти plotline-настройки при auto-discovery индикаторов.
    plotlines = dict(
        equity_pnl=dict(_name='Equity PnL', _method='bar'),
        drawdown=dict(_name='Drawdown', _method='bar'),
    )

    def __init__(self):
        self._start_value = None
        self._max_value = None

    def next(self):
        value = float(self._owner.broker.getvalue())

        if self._start_value is None:
            if self.p.start_value is None:
                self._start_value = value
            else:
                self._start_value = float(self.p.start_value)

        if self._max_value is None:
            self._max_value = value
        else:
            self._max_value = max(self._max_value, value)

        self.lines.equity_pnl[0] = value - self._start_value
        self.lines.drawdown[0] = value - self._max_value


class AutoTuneFilterStrategy(bt.Strategy):
    """
    Оригинальная стратегия Элерса:
    - ROC = BP - BP[2]
    - Long  when ROC crosses above 0 and MinCorr < Thresh
    - Short when ROC crosses below 0 and MinCorr < Thresh and Filt > 0

    К оригинальным критериям Элерса добавлен фильтр min_dc:
    сделки не открываются, если dominant cycle ниже заданного порога.
    """

    params = dict(
        write_history=None,  # Записывать ли trade/order history в Excel
        risk=None,
        window=26,
        bandwidth=0.22,
        thresh=-0.22,
        allow_short=True,
        printlog=False,
        tp_mult=2.0,   # тейк-профит в R
        min_dc=0,      # минимальный dominant cycle AutoTune для входа, в барах
        show_equity_dd=False,  # в single_plot выводить Equity PnL / Drawdown как индикатор CerebroView
    )

    def log(self, txt):
        if self.p.printlog:
            dt = self.data.datetime.datetime(0)
            print(f'{dt} | {txt}')

    def __init__(self):
        self.atf = AutoTuneFilter(
            self.data.close,
            window=self.p.window,
            bandwidth=self.p.bandwidth,
        )

        if self.p.show_equity_dd:
            self.equity_dd = EquityDrawDownIndicator(
                self.data,
                start_value=self.broker.getvalue(),
            )

        self.roc = self.atf.bp - self.atf.bp(-2)
        self.cross_up = bt.indicators.CrossUp(self.roc, 0.0)
        self.cross_down = bt.indicators.CrossDown(self.roc, 0.0)

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

        self.stop_loss_price = 0.0
        self.entry_price = 0.0
        self.take_profit_price = 0.0
        self.order = None
        self.stop_order = None
        self.take_profit_order = None
        self.data_end_close_order = None

    def _reset_bracket_state(self):
        self.order = None
        self.stop_order = None
        self.take_profit_order = None
        self.data_end_close_order = None
        self.stop_loss_price = 0.0
        self.take_profit_price = 0.0
        self.entry_price = 0.0

    def _has_active_orders(self):
        return any(
            order is not None and order.alive()
            for order in (self.order, self.stop_order, self.take_profit_order, self.data_end_close_order)
        )

    def _round_exit_price(self, comminfo, price, isbuy):
        if comminfo.p.cost_of_price_step != 0:
            return round_to_nearest_price_step(comminfo.p.cost_of_price_step, price, isbuy)
        return price

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
        """
        size = int(size)

        while size > 0:
            _, _, total_required = self._estimate_entry_cost(comminfo, price, size)

            if total_required <= cash:
                return size

            size -= 1

        return 0

    def _data_bars_left(self):
        """
        Возвращает количество баров, оставшихся в текущем data feed после текущего бара.

        В историческом прогоне при preload=True Backtrader знает полную длину
        источника через buflen(). Поэтому выход перед завершением контракта/данных
        строится не по календарной дате экспирации, а по числу оставшихся баров.
        """
        try:
            return int(self.data.buflen()) - int(len(self.data))
        except Exception:
            return None

    def _is_data_end_exit_window(self):
        """True, если пора блокировать новые входы и закрывать позицию перед концом data feed."""
        bars_left = self._data_bars_left()
        return bars_left is not None and bars_left <= DATA_END_EXIT_BARS

    def _cancel_entry_order(self):
        """Отменяет ещё не исполненный входной ордер, если он есть."""
        if self.order is not None and self.order.alive():
            self.cancel(self.order)
        self.order = None

    def _cancel_bracket_children(self):
        """Отменяет защитные bracket-ордера перед принудительным закрытием."""
        for order in (self.stop_order, self.take_profit_order):
            if order is not None and order.alive():
                self.cancel(order)

        self.stop_order = None
        self.take_profit_order = None

    def _submit_data_end_close(self):
        """Закрывает открытую позицию перед завершением текущего data feed."""
        if not self.position:
            return

        if self.data_end_close_order is not None and self.data_end_close_order.alive():
            return

        self._cancel_bracket_children()
        bars_left = self._data_bars_left()
        self.log(
            f'DATA END EXIT -> close() | '
            f'date={self.data.datetime.date(0)} | bars_left={bars_left}'
        )
        self.data_end_close_order = self.close(name='data_end_close')

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
            elif order_name == 'data_end_close':
                self.log(f'DATA END CLOSE EXECUTED at {order.executed.price:.2f}')
                self._reset_bracket_state()

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log(f'ORDER {order_name} FAILED: {order.getstatusname()}')

            if order_name in ('long', 'short'):
                self._reset_bracket_state()
            elif order_name == 'stop_loss':
                self.stop_order = None
            elif order_name == 'take_profit':
                self.take_profit_order = None
            elif order_name == 'data_end_close':
                self.data_end_close_order = None

    def next(self):
        # Перед завершением текущего data feed не открываем новые сделки.
        # Если позиция ещё открыта, заранее отправляем рыночный close().
        # В обычном режиме Backtrader close() исполнится на следующем баре,
        # поэтому сигнал на закрытие нужно отправлять ДО последнего бара.
        if self._is_data_end_exit_window():
            if self.position:
                self._submit_data_end_close()
            else:
                self._cancel_entry_order()
                self._cancel_bracket_children()
            return

        if self.position or self._has_active_orders():
            return

        if self.long_signal[0]:
            self._submit_bracket(isbuy=True)
        elif self.p.allow_short and self.short_signal[0]:
            self._submit_bracket(isbuy=False)


class AutoTuneFilterEhlersStrategy(AutoTuneFilterStrategy):
    """
    Вариант стратегии с выходом/разворотом по оригинальной логике Элерса.

    В этом режиме нет bracket-ордера со stop-loss/take-profit.
    При обратном сигнале стратегия либо разворачивает позицию, либо закрывает long,
    если short-сделки отключены.
    """

    def _calc_ehlers_target_size(self):
        """
        Рассчитывает целевой размер позиции для always-in-the-market логики.
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
        Отправляет ордер к целевой позиции по логике Элерса.

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
        if self._is_data_end_exit_window():
            if self.position:
                self._submit_data_end_close()
            else:
                self._cancel_entry_order()
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
