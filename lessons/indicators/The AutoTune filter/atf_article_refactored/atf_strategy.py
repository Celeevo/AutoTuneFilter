import backtrader as bt
import numpy as np
import pandas as pd

from atf_indicator import AutoTuneFilter
from moex_setup import round_to_nearest_price_step

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

    def _data_bars_left(self):
        """
        Возвращает количество баров, оставшихся в текущем источнике данных
        после текущего бара.

        В историческом прогоне при preload=True Backtrader знает полную длину
        data feed через buflen(). Это надёжнее, чем привязываться к календарной
        дате экспирации: по отдельным контрактам последний доступный бар может
        быть раньше/позже формальной даты.
        """
        try:
            return int(self.data.buflen()) - int(len(self.data))
        except Exception:
            return None

    def _is_data_end_exit_window(self):
        """True, если пора блокировать новые входы и закрывать позицию перед концом data feed."""
        if not self.p.close_on_expiration:
            return False

        bars_left = self._data_bars_left()

        if bars_left is None:
            return self._is_after_expiration_exit_bar()

        return bars_left <= int(self.p.expiration_exit_bar)

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

    def _submit_expiration_close(self):
        """Закрывает открытую позицию перед завершением текущего data feed."""
        if not self.position:
            return

        if self.expiration_close_order is not None and self.expiration_close_order.alive():
            return

        self._cancel_bracket_children()
        bars_left = self._data_bars_left()
        self.log(
            f'DATA END EXIT -> close() | '
            f'date={self.data.datetime.date(0)} | bars_left={bars_left}'
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

        # Перед завершением текущего data feed не открываем новые сделки.
        # Если позиция ещё открыта, заранее отправляем рыночный close().
        # В обычном режиме Backtrader close() исполнится на следующем баре,
        # поэтому сигнал на закрытие нужно отправлять ДО последнего бара.
        if self._is_data_end_exit_window():
            if long_now or short_now:
                self._record_signal(
                    decision='blocked_by_data_end',
                    long_signal=long_now,
                    short_signal=short_now,
                )

            if self.position:
                self._submit_expiration_close()
            else:
                self._cancel_entry_order()
                self._cancel_bracket_children()

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



