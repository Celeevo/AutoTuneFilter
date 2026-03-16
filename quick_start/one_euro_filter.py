"""
One Euro Filter — Индикатор для Backtrader
==========================================
Оригинальный фильтр: Georges Casiez, Nicolas Roussel, Daniel Vogel (CHI 2012)
Адаптация для рынков: John F. Ehlers, TASC December 2025
                      "Low-Latency Smoothing — The One Euro Filter"

Концепция
---------
Адаптивный EMA-фильтр нижних частот, который динамически изменяет
коэффициент сглаживания (alpha) в зависимости от скорости изменения
входного ряда. В периоды быстрого движения фильтр уменьшает сглаживание
(меньше лага), в периоды боковика — увеличивает его (меньше шума).

Алгоритм (Ehlers EasyLanguage → Python)
----------------------------------------
1. AlphaDX  = 2π / (4π + PeriodDX)          # фиксированный α для EMA дельты
2. SmoothedDX = AlphaDX*(Price - Price[1])
               + (1 - AlphaDX)*SmoothedDX[1]  # EMA скорости изменения цены
3. Cutoff   = PeriodMin + Beta * |SmoothedDX| # адаптивный период среза
4. Alpha3   = 2π / (4π + Cutoff)             # адаптивный α
5. Smoothed = Alpha3 * Price
             + (1 - Alpha3) * Smoothed[1]     # результирующий сглаженный ряд

Параметры
---------
period_min : int   — минимальный период среза (по умолчанию 10)
beta       : float — коэффициент чувствительности к скорости изменения (0.2)
period_dx  : int   — период для EMA дельты (по умолчанию 10, как у Эхлерса)
"""

import math
import backtrader as bt


# ---------------------------------------------------------------------------
# Основной индикатор: One Euro Filter
# ---------------------------------------------------------------------------

class OneEuroFilter(bt.Indicator):
    """
    One Euro Filter (Ehlers, TASC December 2025).

    Линии:
        oef  — сглаженный ряд (основной выход фильтра)

    Параметры:
        period_min (int)   : минимальный период среза, default=10
        beta       (float) : отзывчивость фильтра, default=0.2
        period_dx  (int)   : период фиксированного EMA для дельты, default=10

    Пример использования в стратегии::

        self.oef = OneEuroFilter(self.data.close, period_min=10, beta=0.2)

    Доступ к значению::

        current_value = self.oef.oef[0]
        # или через псевдоним первой линии:
        current_value = self.oef[0]
    """

    lines = ("oef",)

    params = dict(
        period_min=15,   # минимальный период среза
        beta=0.2,        # коэффициент чувствительности
        period_dx=15,    # период для EMA скорости изменения
    )

    plotinfo = dict(
        subplot=False,          # рисуем поверх ценового графика
        plotname="One Euro Filter",
    )

    plotlines = dict(
        oef=dict(color="blue", linewidth=1.5),
    )

    def __init__(self):
        # Фиксированный alpha для EMA дельты цены
        self._alpha_dx = 2.0 * math.pi / (4.0 * math.pi + self.p.period_dx)

        # Внутренние переменные (инициализируются в next/once)
        self._smoothed_dx = 0.0
        self._smoothed = None          # будет инициализирован на первом баре

    def nextstart(self):
        """Инициализация на первом доступном баре."""
        self._smoothed = self.data[0]
        self._smoothed_dx = 0.0
        self.lines.oef[0] = self._smoothed

    def next(self):
        price = self.data[0]
        prev_price = self.data[-1]

        # Шаг 1-2: EMA скорости изменения цены
        self._smoothed_dx = (
            self._alpha_dx * (price - prev_price)
            + (1.0 - self._alpha_dx) * self._smoothed_dx
        )

        # Шаг 3: адаптивный период среза
        cutoff = self.p.period_min + self.p.beta * abs(self._smoothed_dx)

        # Шаг 4: адаптивный alpha
        alpha3 = 2.0 * math.pi / (4.0 * math.pi + cutoff)

        # Шаг 5: адаптивное сглаживание
        self._smoothed = alpha3 * price + (1.0 - alpha3) * self._smoothed

        self.lines.oef[0] = self._smoothed


# ---------------------------------------------------------------------------
# Вспомогательный индикатор: двухполюсной High-Pass Filter (Ehlers)
# ---------------------------------------------------------------------------

class EhlersHighPass(bt.Indicator):
    """
    Двухполюсной High-Pass Filter Эхлерса.

    Используется для создания осциллятора (применяется перед OneEuroFilter).
    Устраняет длинноволновые тренды, оставляя циклические компоненты.

    Параметры:
        period (int) : период фильтра, default=54
    """

    lines = ("hp",)

    params = dict(period=54)

    plotinfo = dict(
        subplot=True,
        plotname="Ehlers High-Pass",
    )

    plotlines = dict(
        hp=dict(color="gray", linewidth=1.0),
    )

    def __init__(self):
        p = self.p.period
        a1 = math.exp(-1.414 * math.pi / p)
        b1 = 2.0 * a1 * math.cos(math.radians(1.414 * 180.0 / p))
        self._c2 = b1
        self._c3 = -(a1 ** 2)
        self._c1 = (1.0 + self._c2 - self._c3) / 4.0

        # Требуем минимум 3 бара для корректного расчёта
        self.addminperiod(3)

    def next(self):
        price = self.data
        self.lines.hp[0] = (
            self._c1 * (price[0] - 2.0 * price[-1] + price[-2])
            + self._c2 * self.lines.hp[-1]
            + self._c3 * self.lines.hp[-2]
        )


# ---------------------------------------------------------------------------
# Составной индикатор: One Euro Oscillator
# ---------------------------------------------------------------------------

class OneEuroOscillator(bt.Indicator):
    """
    One Euro Oscillator — фильтр Эхлерса в режиме осциллятора.

    Применяет High-Pass Filter к ценовому ряду, затем сглаживает
    результат с помощью One Euro Filter. Удаляет тренд и шум одновременно.

    Линии:
        hp_raw  — сырой High-Pass (осциллятор до сглаживания)
        oef_osc — сглаженный осциллятор (основной выход)

    Параметры:
        hp_period  (int)   : период High-Pass фильтра, default=54
        period_min (int)   : минимальный период One Euro Filter, default=10
        beta       (float) : чувствительность One Euro Filter, default=0.2
        period_dx  (int)   : период EMA дельты в One Euro Filter, default=10

    Пример::

        self.osc = OneEuroOscillator(self.data.close,
                                     hp_period=54, period_min=10, beta=0.2)
        # сырой high-pass:
        hp_value  = self.osc.hp_raw[0]
        # сглаженный осциллятор:
        osc_value = self.osc.oef_osc[0]
    """

    lines = ("hp_raw", "oef_osc")

    params = dict(
        hp_period=54,
        period_min=10,
        beta=0.2,
        period_dx=10,
    )

    plotinfo = dict(subplot=True, plotname="One Euro Oscillator")

    plotlines = dict(
        hp_raw=dict(color="gray", linewidth=0.8, _name="HP Raw"),
        oef_osc=dict(color="red", linewidth=1.5, _name="OEF Oscillator"),
    )

    def __init__(self):
        self._hp = EhlersHighPass(self.data, period=self.p.hp_period)
        self._oef = OneEuroFilter(
            self._hp.hp,
            period_min=self.p.period_min,
            beta=self.p.beta,
            period_dx=self.p.period_dx,
        )
        self.lines.hp_raw = self._hp.lines.hp
        self.lines.oef_osc = self._oef.lines.oef


# ---------------------------------------------------------------------------
# Демонстрационная стратегия
# ---------------------------------------------------------------------------

class OneEuroFilterStrategy(bt.Strategy):
    """
    Демонстрационная стратегия на базе One Euro Filter.

    Логика сигналов (пример):
    - Покупка:  цена пересекает OEF снизу вверх (бычье пересечение)
    - Продажа:  цена пересекает OEF сверху вниз (медвежье пересечение)

    Параметры:
        period_min (int)   : минимальный период OEF, default=10
        beta       (float) : чувствительность OEF, default=0.2
        hp_period  (int)   : период High-Pass осциллятора, default=54
    """

    params = dict(
        period_min=10,
        beta=0.2,
        hp_period=54,
    )

    def __init__(self):
        self.oef = OneEuroFilter(
            self.data.close,
            period_min=self.p.period_min,
            beta=self.p.beta,
        )
        self.osc = OneEuroOscillator(
            self.data.close,
            hp_period=self.p.hp_period,
            period_min=self.p.period_min,
            beta=self.p.beta,
        )
        # Пересечение цены и фильтра
        self.cross = bt.indicators.CrossOver(self.data.close, self.oef.oef)

    def next(self):
        if not self.position:
            if self.cross[0] > 0:          # цена пересекла OEF снизу вверх
                self.buy()
        else:
            if self.cross[0] < 0:          # цена пересекла OEF сверху вниз
                self.sell()

    def log(self, msg):
        dt = self.datas[0].datetime.date(0)
        print(f"[{dt}] {msg}")

    def notify_order(self, order):
        if order.status == order.Completed:
            side = "BUY" if order.isbuy() else "SELL"
            self.log(f"{side} @ {order.executed.price:.2f}")


# ---------------------------------------------------------------------------
# Точка входа: быстрый тест с данными Yahoo Finance
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        import yfinance as yf
    except ImportError:
        raise SystemExit("Установите yfinance:  pip install yfinance")

    import backtrader as bt

    # --- Загрузка данных ---
    raw = yf.download("SPY", start="2023-01-01", end="2025-03-01",
                      auto_adjust=True, progress=False)

    # Приводим к нужной форме для PandasData
    raw.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                   for c in raw.columns]
    raw.index = raw.index.tz_localize(None)

    data = bt.feeds.PandasData(dataname=raw)

    # --- Cerebro ---
    cerebro = bt.Cerebro()
    cerebro.adddata(data)
    cerebro.addstrategy(
        OneEuroFilterStrategy,
        period_min=10,
        beta=0.2,
        hp_period=54,
    )
    cerebro.broker.setcash(100_000)
    cerebro.broker.setcommission(commission=0.001)

    print(f"Начальный капитал: ${cerebro.broker.getvalue():,.2f}")
    cerebro.run()
    print(f"Итоговый капитал:  ${cerebro.broker.getvalue():,.2f}")

    cerebro.plot(style="candlestick", volume=False)
