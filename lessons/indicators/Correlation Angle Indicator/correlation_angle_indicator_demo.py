from datetime import datetime
from moex_store import MoexStore
import math
import backtrader as bt
from cerebroview import plot


class CorrelationAngleIndicator(bt.Indicator):
    """
    John Ehlers - Correlation Angle Indicator
    По статье: "Correlation as a Cycle Indicator".

    Что выдаёт индикатор:
    - real      : корреляция цены с Cosine-волной выбранного периода
    - imag      : корреляция цены с Negative Sine-волной того же периода
    - angle_raw : сырой фазовый угол до anti-regression правила
    - angle     : итоговый фазовый угол Phasor Angle, диапазон примерно -180..+180
    - dangle    : изменение итогового angle относительно прошлого бара
    - state     : состояние рынка по логике статьи:
                  0  = cycle mode
                  1  = trend up
                 -1  = trend down
    - zero      : нулевая линия для графика
    - price     : входной ряд, который реально использовался в расчёте
                  close при input_period=0 или синусоида при input_period>0

    Важно:
    - Для реальных котировок ставь input_period=0.
    - Для проверки на синусоиде, как в статье, ставь input_period=20, 21, 30 и т.д.
    """

    lines = (
        'real',
        'imag',
        'angle_raw',
        'angle',
        'dangle',
        'state',
        'zero',
        'price',
    )

    params = (
        ('period', 20),
        ('input_period', 0),      # 0 = использовать реальную цену; >0 = тестовая синусоида
        ('trend_threshold', 9.0), # порог из статьи: phase rate-change < 9 degrees => trend
        ('eps', 1e-12),
    )

    plotinfo = dict(subplot=True)

    plotlines = dict(
        real=dict(_name='Real / CosCorr'), # , _plotskip=True
        imag=dict(_name='Imag / NegSineCorr'),
        angle_raw=dict(_name='Raw Angle'),
        angle=dict(_name='Correlation Angle'),
        dangle=dict(_name='Angle Delta'),
        state=dict(_name='State'),
        zero=dict(_name='Zero'),
        price=dict(_name='Synthetic/Input Price'),
    )

    def __init__(self):
        period = int(self.p.period)
        if period < 2:
            raise ValueError('period должен быть >= 2')

        # Для корреляции нужен полный период истории.
        self.addminperiod(period)

    @staticmethod
    def _safe(value, default=0.0):
        """
        Защита от None/nan.
        """
        if value is None:
            return default

        try:
            if math.isnan(value):
                return default
        except TypeError:
            pass

        return value

    def _prev(self, line, ago=-1, default=0.0):
        """
        Безопасный доступ к прошлым значениям собственной линии индикатора.
        """
        bars_back = -ago
        if len(self) > bars_back:
            return self._safe(line[ago], default)

        return default

    def _price_at(self, bars_ago):
        """
        Возвращает Price[count - 1] из EasyLanguage-кода Ehlers.

        При input_period=0 используется реальная цена self.data.
        При input_period>0 используется теоретическая синусоида:
            Sine(360 * CurrentBar / InputPeriod)
        """
        input_period = int(self.p.input_period)

        if input_period > 0:
            # EasyLanguage CurrentBar условно 1-based.
            # Для исторического значения bars_ago отступаем назад от текущего номера бара.
            current_bar = len(self)
            bar_no = current_bar - bars_ago
            return math.sin(math.radians(360.0 * bar_no / input_period))

        # bars_ago=0 -> self.data[0], bars_ago=1 -> self.data[-1], ...
        return self._safe(self.data[-bars_ago], 0.0)

    def _corr_with_wave(self, wave_func, fallback):
        """
        Pearson correlation между Price и заданной волной Y.
        Это прямой перенос блока:
            (Length*Sxy - Sx*Sy) / sqrt((Length*Sxx - Sx*Sx)*(Length*Syy - Sy*Sy))
        """
        length = int(self.p.period)

        sx = sy = sxx = sxy = syy = 0.0

        for count in range(1, length + 1):
            bars_ago = count - 1

            x = self._price_at(bars_ago)
            y = wave_func(bars_ago)

            sx += x
            sy += y
            sxx += x * x
            sxy += x * y
            syy += y * y

        vx = length * sxx - sx * sx
        vy = length * syy - sy * sy

        if vx <= self.p.eps or vy <= self.p.eps:
            return fallback

        corr = (length * sxy - sx * sy) / math.sqrt(vx * vy)
        return self._safe(corr, fallback)

    def next(self):
        period = float(self.p.period)

        # Текущий входной ряд — удобно печатать/сверять.
        self.l.price[0] = self._price_at(0)
        self.l.zero[0] = 0.0

        prev_real = self._prev(self.l.real, -1, 0.0)
        prev_imag = self._prev(self.l.imag, -1, 0.0)
        prev_angle = self._prev(self.l.angle, -1, 0.0)

        # 1) Correlate Price with Cosine wave having a fixed period
        real = self._corr_with_wave(
            wave_func=lambda bars_ago: math.cos(math.radians(360.0 * bars_ago / period)),
            fallback=prev_real,
        )

        # 2) Correlate Price with a Negative Sine wave having a fixed period
        imag = self._corr_with_wave(
            wave_func=lambda bars_ago: -math.sin(math.radians(360.0 * bars_ago / period)),
            fallback=prev_imag,
        )

        # 3) Compute the angle as an arctangent function and resolve ambiguity
        if abs(imag) > self.p.eps:
            angle_raw = 90.0 + math.degrees(math.atan(real / imag))
            if imag > 0.0:
                angle_raw -= 180.0
        else:
            # В EasyLanguage переменная Angle в таком случае сохраняла бы прошлое значение.
            angle_raw = prev_angle

        angle = angle_raw

        # 4) Do not allow the rate change of angle to go negative
        # Разрешаем только wraparound, когда переход около +180 -> -180.
        if len(self) > 1:
            if (prev_angle - angle < 270.0) and (angle < prev_angle):
                angle = prev_angle

        dangle = angle - prev_angle if len(self) > 1 else 0.0

        # 5) Compute market state
        state = 0.0
        if len(self) > 1 and abs(angle - prev_angle) < float(self.p.trend_threshold):
            if angle < 0.0:
                state = -1.0
            else:
                state = 1.0

        self.l.real[0] = real
        self.l.imag[0] = imag
        self.l.angle_raw[0] = angle_raw
        self.l.angle[0] = angle
        self.l.dangle[0] = dangle
        self.l.state[0] = state


class CorrelationAngleDemoStrategy(bt.Strategy):
    params = (
        ('period', 20),
        ('input_period', 0),
        ('trend_threshold', 9.0),
    )

    def __init__(self):
        self.corr_angle = CorrelationAngleIndicator(
            self.data.close,
            period=self.p.period,
            input_period=self.p.input_period,
            trend_threshold=self.p.trend_threshold,
        )

    @staticmethod
    def _state_name(state):
        state_int = int(round(state))
        if state_int == 1:
            return 'TREND_UP'
        if state_int == -1:
            return 'TREND_DOWN'
        return 'CYCLE'

    def next(self):
        dt = self.data.datetime.datetime(0)
        ind = self.corr_angle

        print(
            f'{dt} | close={self.data.close[0]:.2f} | '
            f'real={ind.real[0]: .6f} | '
            f'imag={ind.imag[0]: .6f} | '
            f'angle_raw={ind.angle_raw[0]: .2f} | '
            f'angle={ind.angle[0]: .2f} | '
            f'dangle={ind.dangle[0]: .2f} | '
            f'state={int(round(ind.state[0])):2d}({self._state_name(ind.state[0])})'
        )


if __name__ == '__main__':
    cerebro = bt.Cerebro(stdstats=False)

    store = MoexStore(write_to_file=True, read_from_file=True)
    data = store.getdata(
        sec_id='MXM6',
        fromdate='2026-03-15',
        todate=datetime.today(),
        tf='1h',
        name='MXM6'
    )

    cerebro.adddata(data)

    # Для сверки с TradingView на реальном графике:
    # - period должен совпадать с настройкой TV
    # - input_period=0 означает использование close, а не тестовой синусоиды
    cerebro.addstrategy(
        CorrelationAngleDemoStrategy,
        period=20,
        input_period=0,
        trend_threshold=9.0,
    )

    # runonce=False удобнее для пошаговой отладки рекурсивной логики angle/state.
    results = cerebro.run(runonce=False)
    cerebro.plot(style='candle')
    plot(cerebro)
