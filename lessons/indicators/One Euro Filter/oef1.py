
from datetime import datetime
from moex_store import MoexStore
import math
import backtrader as bt


def round_to_nearest_price_step(step, value, isbuy):
    """
    Округление цены к шагу цены.

    Для стопа:
    - long stop ниже входа: sell stop, лучше округлять вниз
    - short stop выше входа: buy stop, лучше округлять вверх

    Но твой исходный AllInSizer передаёт isbuy = направление входа.
    Поэтому:
    - если вход buy=True, стоп будет sell stop ниже входа -> округляем вниз
    - если вход buy=False, стоп будет buy stop выше входа -> округляем вверх
    """
    if step == 0:
        return value

    if isbuy:
        return math.floor(value / step) * step
    else:
        return math.ceil(value / step) * step


class AllInSizer(bt.Sizer):
    def _getsizing(self, comminfo, cash, data, isbuy):
        if comminfo.p.margin:  # работаем с фьючерсами?
            max_size = cash / comminfo.p.margin  # Кэш / ГО
        else:
            max_size = cash / self.strategy.entry_price  # Кэш / вход

        size = int(max_size) - 1

        if size <= 0:
            return 0

        direction = 2 * isbuy - 1  # 1 при входе в лонг, -1 при входе в шорт

        stop_loss_price = (
            self.strategy.entry_price
            - direction
            * cash
            * (self.strategy.p.risk / 100)
            / (size * comminfo.p.mult)
        )

        cost_of_price_step = getattr(comminfo.p, 'cost_of_price_step', 0)

        if cost_of_price_step != 0:
            self.strategy.stop_loss_price = round_to_nearest_price_step(
                cost_of_price_step,
                stop_loss_price,
                isbuy
            )
        else:
            self.strategy.stop_loss_price = stop_loss_price

        return size


class HighPass(bt.Indicator):
    """
    John F. Ehlers HighPass filter.

    EasyLanguage-логика из статьи:

    Q  = expvalue(-1.414 * pi / Period)
    c1 = 2 * Q * Cosine(1.414 * 180 / Period)
    c2 = Q * Q
    a0 = (1 + c1 + c2) / 4

    HighPass =
        a0 * (Price - 2 * Price[1] + Price[2])
        + c1 * HighPass[1]
        - c2 * HighPass[2]

    В Backtrader:
    Price[1] в EasyLanguage = data[-1]
    Price[2] в EasyLanguage = data[-2]
    """

    lines = ('hp',)

    params = (
        ('period', 54),
    )

    plotinfo = dict(subplot=True)

    plotlines = dict(
        hp=dict(_name='HighPass')
    )

    def __init__(self):
        q = math.exp(-1.414 * math.pi / self.p.period)
        c1 = 2.0 * q * math.cos(1.414 * math.pi / self.p.period)
        c2 = q * q
        a0 = (1.0 + c1 + c2) / 4.0

        self._a0 = a0
        self._c1 = c1
        self._c2 = c2

    def next(self):
        if len(self) < 4:
            self.lines.hp[0] = 0.0
            return

        price = self.data[0]
        price_1 = self.data[-1]
        price_2 = self.data[-2]

        self.lines.hp[0] = (
            self._a0 * (price - 2.0 * price_1 + price_2)
            + self._c1 * self.lines.hp[-1]
            - self._c2 * self.lines.hp[-2]
        )


class OneEuroFilter(bt.Indicator):
    """
    One Euro Filter по версии John F. Ehlers.

    Это адаптивная EMA.

    В статье:
    - сначала сглаживается однобарная разность цены;
    - затем Beta * abs(smoothed_dx) добавляется к PeriodMin;
    - через полученный Cutoff считается новый alpha;
    - этим alpha сглаживается Price.

    Важно:
    реализуем именно формулу Элерса из листинга, не оригинальную CHI-версию.
    """

    lines = (
        'filtered',
        'smoothed_dx',
        'cutoff',
        'alpha',
    )

    params = (
        ('period_min', 10),
        ('beta', 0.2),
        ('period_dx', 10),
    )

    plotinfo = dict(subplot=False)

    plotlines = dict(
        filtered=dict(_name='OneEuroFilter'),
        smoothed_dx=dict(_plotskip=True),
        cutoff=dict(_plotskip=True),
        alpha=dict(_plotskip=True),
    )

    def __init__(self):
        self._alpha_dx = 2.0 * math.pi / (4.0 * math.pi + self.p.period_dx)

    @staticmethod
    def _alpha_from_period(period):
        return 2.0 * math.pi / (4.0 * math.pi + period)

    def next(self):
        price = self.data[0]

        if len(self) == 1:
            self.lines.smoothed_dx[0] = 0.0
            self.lines.cutoff[0] = self.p.period_min
            self.lines.alpha[0] = self._alpha_from_period(self.p.period_min)
            self.lines.filtered[0] = price
            return

        dx = price - self.data[-1]

        smoothed_dx = (
            self._alpha_dx * dx
            + (1.0 - self._alpha_dx) * self.lines.smoothed_dx[-1]
        )

        cutoff = self.p.period_min + self.p.beta * abs(smoothed_dx)
        alpha = self._alpha_from_period(cutoff)

        filtered = (
            alpha * price
            + (1.0 - alpha) * self.lines.filtered[-1]
        )

        self.lines.smoothed_dx[0] = smoothed_dx
        self.lines.cutoff[0] = cutoff
        self.lines.alpha[0] = alpha
        self.lines.filtered[0] = filtered


class OneEuroOscillator(bt.Indicator):
    """
    Осцилляторная версия One Euro Filter:

        Close -> HighPass(period=54) -> OneEuroFilter

    Идея соответствует рекомендации Элерса:
    заменить Price = Close на Price = $Highpass(Close, 54).
    """

    lines = ('osc',)

    params = (
        ('hp_period', 54),
        ('period_min', 10),
        ('beta', 0.2),
        ('period_dx', 10),
    )

    plotinfo = dict(subplot=True)

    plotlines = dict(
        osc=dict(_name='OneEuroOsc')
    )

    def __init__(self):
        self.hp = HighPass(self.data, period=self.p.hp_period)

        self.oe = OneEuroFilter(
            self.hp,
            period_min=self.p.period_min,
            beta=self.p.beta,
            period_dx=self.p.period_dx,
        )

        self.lines.osc = self.oe.filtered


class OneEuroTrendOscStrategy(bt.Strategy):
    """
    Комбинированная система:

    Long entry:
        1. позиции нет
        2. Close > EMA
        3. EMA растёт
        4. OneEuroOscillator пересёк 0 снизу вверх

    Short entry:
        1. позиции нет
        2. Close < EMA
        3. EMA падает
        4. OneEuroOscillator пересёк 0 сверху вниз

    Long exit:
        1. OneEuroOscillator пересёк 0 сверху вниз
        или сработал stop-loss

    Short exit:
        1. OneEuroOscillator пересёк 0 снизу вверх
        или сработал stop-loss
    """

    params = (
        ('trend_period', 100),

        ('hp_period', 54),
        ('period_min', 10),
        ('beta', 0.2),
        ('period_dx', 10),

        ('risk', 5.0),  # риск на сделку в %, используется AllInSizer

        ('printlog', True),
    )

    def __init__(self):
        self.trend = bt.indicators.EMA(
            self.data.close,
            period=self.p.trend_period
        )

        self.osc = OneEuroOscillator(
            self.data.close,
            hp_period=self.p.hp_period,
            period_min=self.p.period_min,
            beta=self.p.beta,
            period_dx=self.p.period_dx,
        )

        self.entry_order = None
        self.stop_order = None
        self.exit_order = None

        self.entry_price = None
        self.stop_loss_price = None

    def log(self, txt):
        if self.p.printlog:
            dt = self.data.datetime.datetime(0)
            print(f'{dt} | {txt}')

    def _has_active_order(self):
        return (
            self.entry_order is not None
            or self.exit_order is not None
        )

    def _cancel_stop_if_exists(self):
        if self.stop_order is not None:
            self.cancel(self.stop_order)
            self.stop_order = None

    def _place_stop_after_entry(self, order):
        """
        Стоп ставим только после исполнения входного ордера.

        Цена stop_loss_price рассчитана в AllInSizer в момент создания входа.
        Размер берём по фактически исполненному размеру входного ордера.
        """

        size = abs(order.executed.size)

        if size <= 0:
            return

        if order.isbuy():
            # Вошли в long -> защитный sell stop
            self.stop_order = self.sell(
                size=size,
                exectype=bt.Order.Stop,
                price=self.stop_loss_price
            )
            self.log(f'STOP LOSS LONG выставлен: price={self.stop_loss_price}, size={size}')

        else:
            # Вошли в short -> защитный buy stop
            self.stop_order = self.buy(
                size=size,
                exectype=bt.Order.Stop,
                price=self.stop_loss_price
            )
            self.log(f'STOP LOSS SHORT выставлен: price={self.stop_loss_price}, size={size}')

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status == order.Completed:
            if order == self.entry_order:
                self.log(
                    f'ENTRY EXECUTED: '
                    f'{"BUY" if order.isbuy() else "SELL"}, '
                    f'price={order.executed.price}, '
                    f'size={order.executed.size}, '
                    f'comm={order.executed.comm}'
                )

                # Для информации сохраняем фактическую цену входа.
                # Но стоп уже рассчитан сайзером от self.entry_price,
                # заданной перед buy/sell.
                self.entry_price = order.executed.price

                self._place_stop_after_entry(order)
                self.entry_order = None
                return

            if order == self.exit_order:
                self.log(
                    f'EXIT EXECUTED by oscillator: '
                    f'{"BUY" if order.isbuy() else "SELL"}, '
                    f'price={order.executed.price}, '
                    f'size={order.executed.size}, '
                    f'comm={order.executed.comm}'
                )

                self.exit_order = None
                self._cancel_stop_if_exists()
                return

            if order == self.stop_order:
                self.log(
                    f'STOP EXECUTED: '
                    f'{"BUY" if order.isbuy() else "SELL"}, '
                    f'price={order.executed.price}, '
                    f'size={order.executed.size}, '
                    f'comm={order.executed.comm}'
                )

                self.stop_order = None
                self.entry_order = None
                self.exit_order = None
                return

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log(f'ORDER {order.ref} {order.getstatusname()}')

            if order == self.entry_order:
                self.entry_order = None

            elif order == self.exit_order:
                self.exit_order = None

            elif order == self.stop_order:
                self.stop_order = None

    def notify_trade(self, trade):
        if trade.isclosed:
            self.log(
                f'TRADE CLOSED: '
                f'pnl={trade.pnl:.2f}, '
                f'pnlcomm={trade.pnlcomm:.2f}'
            )

    def next(self):
        close = self.data.close[0]

        trend_up = (
            close > self.trend[0]
            and self.trend[0] > self.trend[-1]
        )

        trend_down = (
            close < self.trend[0]
            and self.trend[0] < self.trend[-1]
        )

        osc_cross_up = (
            self.osc[0] > 0
            and self.osc[-1] <= 0
        )

        osc_cross_down = (
            self.osc[0] < 0
            and self.osc[-1] >= 0
        )

        long_signal = (
            not self.position
            and trend_up
            and osc_cross_up
        )

        short_signal = (
            not self.position
            and trend_down
            and osc_cross_down
        )

        exit_long_signal = (
            self.position.size > 0
            and osc_cross_down
        )

        exit_short_signal = (
            self.position.size < 0
            and osc_cross_up
        )

        self.log(
            f'NEXT: close={close:.2f}, '
            f'trend={self.trend[0]:.2f}, '
            f'osc={self.osc[0]:.6f}, '
            f'pos={self.position.size}'
        )

        # Если есть активный вход или выход, не создаём новые рыночные ордера.
        # Stop-order может висеть отдельно как защитный.
        if self._has_active_order():
            return

        # Выход по зеркальному сигналу осциллятора.
        # Это не гарантированный take-profit в смысле фиксированной прибыли,
        # а сигнальный выход: он может закрыть сделку как в плюс, так и в минус.
        if exit_long_signal:
            self.log('EXIT LONG SIGNAL: osc пересёк 0 сверху вниз -> close()')
            self._cancel_stop_if_exists()
            self.exit_order = self.close()
            return

        if exit_short_signal:
            self.log('EXIT SHORT SIGNAL: osc пересёк 0 снизу вверх -> close()')
            self._cancel_stop_if_exists()
            self.exit_order = self.close()
            return

        # Вход в long.
        # entry_price нужен твоему AllInSizer до вызова buy().
        if long_signal:
            self.entry_price = close
            self.stop_loss_price = None

            self.log(
                f'LONG SIGNAL: buy(), '
                f'estimated_entry={self.entry_price:.2f}'
            )

            self.entry_order = self.buy()
            return

        # Вход в short.
        if short_signal:
            self.entry_price = close
            self.stop_loss_price = None

            self.log(
                f'SHORT SIGNAL: sell(), '
                f'estimated_entry={self.entry_price:.2f}'
            )

            self.entry_order = self.sell()
            return


if __name__ == '__main__':
    cerebro = bt.Cerebro()

    store = MoexStore(write_to_file=True, read_from_file=True)

    data = store.getdata(
        sec_id='MXM6',
        fromdate='2026-03-15',
        todate=datetime.today(),
        # tf='5m',
        tf='1h',
        name='MXM6'
    )

    cerebro.adddata(data)
    cerebro.addsizer(AllInSizer)
    cerebro.addstrategy(
        OneEuroTrendOscStrategy,
        trend_period=100,
        hp_period=54,
        period_min=10,
        beta=0.2,
        period_dx=10,
        risk=1.0,
        printlog=True,
    )

    results = cerebro.run()
    cerebro.plot(style='candle')