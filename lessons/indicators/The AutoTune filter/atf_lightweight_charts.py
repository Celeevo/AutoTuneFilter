from datetime import datetime
from moex_store import MoexStore
import math
import backtrader as bt

import math
import backtrader as bt


class AutoTuneFilter(bt.Indicator):
    """
    John Ehlers - AutoTune Filter
    Упрощённая и более учебная реализация для Backtrader.

    Что выдаёт индикатор:
    - bp      : главный выход, tuned band-pass filter
    - filt    : high-pass filtered series
    - mincorr : минимальная rolling autocorrelation
    - dc      : dominant cycle

    Важно:
    Эта версия не меняет математику рабочей реализации.
    Мы только:
    - убираем лишнюю громоздкость,
    - оставляем только действительно нужные защиты,
    - делаем код понятным для чтения и объяснения.
    """

    lines = ('bp', 'filt', 'mincorr', 'dc')

    params = (
        ('window', 20),
        ('bandwidth', 0.25),
        # Параметр оставлен для совместимости с более ранним кодом
        # и с интерфейсной логикой вокруг индикатора.
        # На расчёт сам по себе он не влияет.
        ('output', 'bp'),
    )

    plotinfo = dict(subplot=True)

    plotlines = dict(
        bp=dict(_name='AutoTune BP'),
        filt=dict(_name='HighPass', _plotskip=True),
        mincorr=dict(_name='MinCorr', _plotskip=True),
        dc=dict(_name='DominantCycle', _plotskip=True),
    )

    # -----------------------------------------------------------------
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ
    # -----------------------------------------------------------------

    @staticmethod
    def _safe(value, default=0.0):
        """
        Мини-аналог Pine-функции nz().

        Если значение:
        - None
        - nan

        то возвращаем default.

        Зачем это нужно:
        В рекурсивных формулах первые значения линии часто ещё не определены.
        Pine спокойно решает это через nz(...).
        В Python / Backtrader это надо сделать явно.
        """
        if value is None:
            return default

        try:
            if math.isnan(value):
                return default
        except TypeError:
            # Если value вообще не float-like, просто возвращаем как есть.
            pass

        return value

    def _src(self, ago=0):
        """
        Безопасный доступ к цене self.data.

        Логика простая:
        - если нужный бар уже есть -> возвращаем его;
        - если истории ещё не хватает -> возвращаем самое старое
          реально доступное значение цены.

        Почему это допустимо:
        Эта защита нужна только на самых ранних барах.
        После накопления истории метод начинает работать как обычный
        self.data[ago].
        """
        if ago == 0:
            return self.data[0]

        bars_back = -ago
        if len(self) > bars_back:
            return self.data[ago]

        # Истории пока не хватает.
        # Возвращаем самый старый доступный бар.
        return self.data[-(len(self) - 1)] if len(self) > 1 else self.data[0]

    def _prev(self, line, ago=-1, default=0.0):
        """
        Безопасный доступ к прошлым значениям собственной линии индикатора.

        В отличие от цены, для своих линий мы НЕ тянем "самое старое доступное
        значение", а подставляем default.

        Почему:
        Для рекурсивных фильтров стартовое значение 0.0 обычно естественнее,
        чем попытка брать древнее значение линии, которого ещё по сути нет.

        Это как раз тот минимум защиты, который действительно нужен здесь.
        """
        bars_back = -ago
        if len(self) > bars_back:
            return self._safe(line[ago], default)

        return default

    # -----------------------------------------------------------------
    # ЖИЗНЕННЫЙ ЦИКЛ ИНДИКАТОРА
    # -----------------------------------------------------------------

    def prenext(self):
        """
        Ранняя стадия: полной истории ещё нет, но индикатор уже можно считать.
        """
        self._step()

    def nextstart(self):
        """
        Первый "полноценный" бар после достижения minimum period.
        Для нас логика расчёта не меняется.
        """
        self._step()

    def next(self):
        """
        Обычный рабочий режим индикатора.
        """
        self._step()

    # -----------------------------------------------------------------
    # ОСНОВНОЙ РАСЧЁТ
    # -----------------------------------------------------------------

    def _step(self):
        """
        Один шаг расчёта индикатора на текущем баре.
        """
        window = int(self.p.window)
        bandwidth = float(self.p.bandwidth)

        # =============================================================
        # 1) HIGH-PASS FILTER
        # =============================================================
        #
        # Это почти дословный перенос Pine-функции hpf(...):
        #
        #   w   = 1.414 * pi / period
        #   q   = exp(-w)
        #   c1  = 2 * q * cos(w)
        #   c2  = q^2
        #   a0  = 0.25 * (1 + c1 + c2)
        #
        #   filt = a0 * (src - 2*src[1] + src[2])
        #          + c1 * filt[1]
        #          - c2 * filt[2]
        #
        # Смысл:
        # мы убираем из цены слишком медленную, "длинную" компоненту
        # и оставляем более колебательную часть ряда.
        # =============================================================
        w = 1.414 * math.pi / window
        q = math.exp(-w)

        c1 = 2.0 * q * math.cos(w)
        c2 = q * q
        a0 = 0.25 * (1.0 + c1 + c2)

        src0 = self._src(0)
        src1 = self._src(-1)
        src2 = self._src(-2)

        filt = (
            a0 * (src0 - 2.0 * src1 + src2)
            + c1 * self._prev(self.l.filt, -1, 0.0)
            - c2 * self._prev(self.l.filt, -2, 0.0)
        )
        self.l.filt[0] = filt

        # =============================================================
        # 2) ROLLING AUTOCORRELATION
        # =============================================================
        #
        # Идея:
        # мы ищем такой lag, при котором текущий кусок filt
        # и тот же ряд, сдвинутый назад на lag, максимально "противофазны".
        #
        # Именно lag с самой маленькой корреляцией даёт нам основу
        # для dominant cycle.
        #
        # Здесь важная практическая деталь:
        # на ранних барах полной истории ещё нет, поэтому:
        # - lag нельзя брать глубже, чем доступно;
        # - внутренний цикл j тоже надо ограничивать доступной историей.
        # =============================================================
        mincorr = 1.0
        best_lag = 1

        # Дальше, чем позволяет реальная история, лезть нельзя.
        max_lag = min(window, max(1, len(self) - 1))

        for lag in range(1, max_lag + 1):
            sx = sy = sxx = sxy = syy = 0.0
            n = 0

            # Для каждой пары x/y нужно иметь:
            # x = filt[-j]
            # y = filt[-(lag + j)]
            #
            # Значит внутренний цикл тоже ограничиваем тем,
            # что реально уже накоплено.
            max_j = min(window - 1, len(self) - lag - 1)
            if max_j < 0:
                continue

            for j in range(max_j + 1):
                x = self._prev(self.l.filt, -j, 0.0)
                y = self._prev(self.l.filt, -(lag + j), 0.0)

                sx += x
                sy += y
                sxx += x * x
                sxy += x * y
                syy += y * y
                n += 1

            # Корреляцию на 0 или 1 точке считать бессмысленно.
            if n < 2:
                continue

            # Числитель и знаменатели корреляции Пирсона
            cov = n * sxy - sx * sy
            vx = n * sxx - sx * sx
            vy = n * syy - sy * sy

            # Если дисперсия одного из рядов нулевая,
            # этот lag просто пропускаем.
            if vx <= 0.0 or vy <= 0.0:
                continue

            corr = cov / math.sqrt(vx * vy)

            if corr < mincorr:
                mincorr = corr
                best_lag = lag

        self.l.mincorr[0] = mincorr

        # =============================================================
        # 3) DOMINANT CYCLE
        # =============================================================
        #
        # В статье:
        # dominant cycle = 2 * lag с минимальной корреляцией.
        #
        # Затем dc ограничивается:
        # не больше чем на +2 / -2 относительно прошлого значения.
        #
        # Это защита от слишком резких скачков dc между соседними барами.
        # =============================================================
        dc = 2.0 * best_lag
        prev_dc = self._prev(self.l.dc, -1, dc)

        if dc > prev_dc + 2.0:
            dc = prev_dc + 2.0
        elif dc < prev_dc - 2.0:
            dc = prev_dc - 2.0

        if dc < 2.0:
            dc = 2.0

        self.l.dc[0] = dc

        # =============================================================
        # 4) TUNED BAND-PASS FILTER
        # =============================================================
        #
        # Это уже "итоговый" фильтр, который подстраивается
        # под найденный dominant cycle.
        #
        # Pine-логика:
        #
        #   w0 = 2*pi / dc
        #   l1 = cos(w0)
        #   g1 = cos(w0 * bandwidth)
        #   s1 = 1/g1 - sqrt(1/g1^2 - 1)
        #
        #   bp = 0.5 * (1 - s1) * (src - src[2])
        #        + l1 * (1 + s1) * bp[1]
        #        - s1 * bp[2]
        #
        # Численные защиты здесь нужны ровно в одном месте:
        # выражение под корнем иногда может стать чуть меньше нуля
        # из-за погрешности float.
        # =============================================================
        w0 = 2.0 * math.pi / dc
        l1 = math.cos(w0)
        g1 = math.cos(w0 * bandwidth)

        # Защита от деления на почти ноль.
        if abs(g1) < 1e-12:
            self.l.bp[0] = self._prev(self.l.bp, -1, 0.0)
            return

        inner = 1.0 / (g1 * g1) - 1.0

        # Если inner стал крошечным отрицательным числом
        # только из-за численной ошибки, считаем его нулём.
        if inner < 0.0 and abs(inner) < 1e-12:
            inner = 0.0
        elif inner < 0.0:
            # Если inner действительно плохой, лучше сохранить
            # предыдущее значение bp, чем получить аварийный sqrt.
            self.l.bp[0] = self._prev(self.l.bp, -1, 0.0)
            return

        s1 = 1.0 / g1 - math.sqrt(inner)

        bp = (
            0.5 * (1.0 - s1) * (src0 - src2)
            + l1 * (1.0 + s1) * self._prev(self.l.bp, -1, 0.0)
            - s1 * self._prev(self.l.bp, -2, 0.0)
        )
        self.l.bp[0] = bp


import calendar
import json
import webbrowser
from pathlib import Path


class AutoTuneChartStrategy(bt.Strategy):
    """
    Стратегия для визуализации результата через TradingView Lightweight Charts.

    Важная идея:
    - Backtrader по-прежнему считает бары и индикатор.
    - Lightweight Charts только отображает уже рассчитанные данные.
    - Поэтому в next() мы не печатаем каждую строку, а сохраняем данные бара
      в self.chart_rows.
    """

    params = (
        ('window', 20),
    )

    def __init__(self):
        self.atf = AutoTuneFilter(
            self.data.close,
            window=self.p.window,
        )
        self.chart_rows = []

    @staticmethod
    def _num(value):
        """Готовит число для JSON: Lightweight Charts не любит NaN/Infinity."""
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None

        if math.isnan(value) or math.isinf(value):
            return None

        return value

    @staticmethod
    def _bt_datetime_to_utc_timestamp(dt):
        """
        Lightweight Charts для intraday-графиков удобно отдавать Unix time в секундах.

        datetime из Backtrader часто приходит без timezone. Для графика здесь
        считаем его UTC-временем, чтобы не получить случайный сдвиг от локальной
        timezone Python-процесса.
        """
        return int(calendar.timegm(dt.timetuple()))

    def next(self):
        dt = self.data.datetime.datetime(0)

        self.chart_rows.append({
            'time': self._bt_datetime_to_utc_timestamp(dt),
            'datetime': dt.strftime('%Y-%m-%d %H:%M:%S'),
            'open': self._num(self.data.open[0]),
            'high': self._num(self.data.high[0]),
            'low': self._num(self.data.low[0]),
            'close': self._num(self.data.close[0]),
            'volume': self._num(self.data.volume[0]),
            'bp': self._num(self.atf.bp[0]),
            'filt': self._num(self.atf.filt[0]),
            'mincorr': self._num(self.atf.mincorr[0]),
            'dc': self._num(self.atf.dc[0]),
        })


HTML_TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title}</title>
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        :root {{
            --bg: #f7f7f7;
            --panel: #ffffff;
            --text: #111111;
            --muted: #666666;
            --border: #e5e5e5;
        }}

        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            padding: 20px;
            background: var(--bg);
            color: var(--text);
            font-family: Arial, Helvetica, sans-serif;
        }}

        .page {{
            max-width: 1500px;
            margin: 0 auto;
        }}

        .header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-end;
            gap: 20px;
            margin-bottom: 14px;
        }}

        h1 {{
            margin: 0;
            font-size: 22px;
            line-height: 1.25;
            font-weight: 700;
        }}

        .subtitle {{
            margin-top: 4px;
            color: var(--muted);
            font-size: 13px;
        }}

        .legend {{
            min-height: 22px;
            color: var(--muted);
            font-size: 13px;
            text-align: right;
            white-space: nowrap;
        }}

        .card {{
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 10px;
            margin-bottom: 12px;
            box-shadow: 0 6px 20px rgba(0, 0, 0, 0.04);
        }}

        .chart-title {{
            padding: 2px 4px 8px 4px;
            color: var(--muted);
            font-size: 13px;
        }}

        .chart {{
            width: 100%;
        }}

        #price-chart {{ height: 470px; }}
        #bp-chart {{ height: 230px; }}
        #mincorr-chart {{ height: 190px; }}
        #dc-chart {{ height: 190px; }}

        .footer {{
            margin-top: 10px;
            color: var(--muted);
            font-size: 12px;
        }}
    </style>
</head>
<body>
<div class="page">
    <div class="header">
        <div>
            <h1>{title}</h1>
            <div class="subtitle">Backtrader → JSON → TradingView Lightweight Charts</div>
        </div>
        <div id="legend" class="legend"></div>
    </div>

    <div class="card">
        <div class="chart-title">Цена: свечи + объём</div>
        <div id="price-chart" class="chart"></div>
    </div>

    <div class="card">
        <div class="chart-title">AutoTuneFilter: BP и HighPass</div>
        <div id="bp-chart" class="chart"></div>
    </div>

    <div class="card">
        <div class="chart-title">AutoTuneFilter: минимальная autocorrelation</div>
        <div id="mincorr-chart" class="chart"></div>
    </div>

    <div class="card">
        <div class="chart-title">AutoTuneFilter: dominant cycle</div>
        <div id="dc-chart" class="chart"></div>
    </div>

    <div class="footer">
        Данные рассчитаны в Backtrader. Lightweight Charts используется только как внешний интерактивный визуализатор.
    </div>
</div>

<script>
const rows = {rows_json};

function addSeriesCompat(chart, seriesTypeName, options) {{
    // Lightweight Charts v5: chart.addSeries(LightweightCharts.CandlestickSeries, options)
    if (typeof chart.addSeries === 'function' && LightweightCharts[seriesTypeName]) {{
        return chart.addSeries(LightweightCharts[seriesTypeName], options || {{}});
    }}

    // Старый API v3/v4: chart.addCandlestickSeries(options)
    const legacyMethods = {{
        CandlestickSeries: 'addCandlestickSeries',
        HistogramSeries: 'addHistogramSeries',
        LineSeries: 'addLineSeries',
    }};

    const methodName = legacyMethods[seriesTypeName];
    if (methodName && typeof chart[methodName] === 'function') {{
        return chart[methodName](options || {{}});
    }}

    throw new Error('Не удалось создать series: ' + seriesTypeName);
}}

function makeChart(containerId, height) {{
    const el = document.getElementById(containerId);
    const chart = LightweightCharts.createChart(el, {{
        width: el.clientWidth,
        height,
        layout: {{
            background: {{ type: 'solid', color: '#ffffff' }},
            textColor: '#222222',
            fontSize: 12,
        }},
        grid: {{
            vertLines: {{ color: '#eeeeee' }},
            horzLines: {{ color: '#eeeeee' }},
        }},
        crosshair: {{
            mode: LightweightCharts.CrosshairMode.Normal,
        }},
        rightPriceScale: {{
            borderColor: '#dddddd',
        }},
        timeScale: {{
            borderColor: '#dddddd',
            timeVisible: true,
            secondsVisible: false,
        }},
    }});

    new ResizeObserver(entries => {{
        if (entries.length === 0 || entries[0].contentRect.width === 0) return;
        chart.applyOptions({{ width: entries[0].contentRect.width }});
    }}).observe(el);

    return chart;
}}

function numericRows(field) {{
    return rows
        .filter(r => r[field] !== null && Number.isFinite(r[field]))
        .map(r => ({{ time: r.time, value: r[field] }}));
}}

const candleData = rows
    .filter(r => [r.open, r.high, r.low, r.close].every(v => v !== null && Number.isFinite(v)))
    .map(r => ({{
        time: r.time,
        open: r.open,
        high: r.high,
        low: r.low,
        close: r.close,
    }}));

const volumeData = rows
    .filter(r => r.volume !== null && Number.isFinite(r.volume))
    .map(r => ({{
        time: r.time,
        value: r.volume,
        color: r.close >= r.open ? 'rgba(38, 166, 154, 0.35)' : 'rgba(239, 83, 80, 0.35)',
    }}));

const priceChart = makeChart('price-chart', 470);
const bpChart = makeChart('bp-chart', 230);
const mincorrChart = makeChart('mincorr-chart', 190);
const dcChart = makeChart('dc-chart', 190);

const candles = addSeriesCompat(priceChart, 'CandlestickSeries', {{
    upColor: '#26a69a',
    downColor: '#ef5350',
    borderVisible: false,
    wickUpColor: '#26a69a',
    wickDownColor: '#ef5350',
}});
candles.setData(candleData);

const volume = addSeriesCompat(priceChart, 'HistogramSeries', {{
    priceFormat: {{ type: 'volume' }},
    priceScaleId: '',
}});
volume.setData(volumeData);
volume.priceScale().applyOptions({{
    scaleMargins: {{ top: 0.82, bottom: 0 }},
}});

const bpLine = addSeriesCompat(bpChart, 'LineSeries', {{
    title: 'BP',
    lineWidth: 2,
    color: '#2962ff',
}});
bpLine.setData(numericRows('bp'));

const filtLine = addSeriesCompat(bpChart, 'LineSeries', {{
    title: 'HighPass',
    lineWidth: 1,
    color: '#f57c00',
}});
filtLine.setData(numericRows('filt'));

const mincorrLine = addSeriesCompat(mincorrChart, 'LineSeries', {{
    title: 'MinCorr',
    lineWidth: 2,
    color: '#7b1fa2',
}});
mincorrLine.setData(numericRows('mincorr'));

const dcLine = addSeriesCompat(dcChart, 'LineSeries', {{
    title: 'DominantCycle',
    lineWidth: 2,
    color: '#00897b',
}});
dcLine.setData(numericRows('dc'));

for (const chart of [priceChart, bpChart, mincorrChart, dcChart]) {{
    chart.timeScale().fitContent();
}}

// Синхронизация горизонтального масштаба между отдельными графиками.
const charts = [priceChart, bpChart, mincorrChart, dcChart];
let syncing = false;
for (const sourceChart of charts) {{
    sourceChart.timeScale().subscribeVisibleLogicalRangeChange(range => {{
        if (syncing || range === null) return;
        syncing = true;
        for (const targetChart of charts) {{
            if (targetChart !== sourceChart) {{
                targetChart.timeScale().setVisibleLogicalRange(range);
            }}
        }}
        syncing = false;
    }});
}}

const legend = document.getElementById('legend');
priceChart.subscribeCrosshairMove(param => {{
    if (!param || !param.time || !param.seriesData) {{
        legend.textContent = '';
        return;
    }}

    const row = rows.find(r => r.time === param.time);
    if (!row) {{
        legend.textContent = '';
        return;
    }}

    legend.textContent = `${{row.datetime}} | O=${{row.open?.toFixed(2)}} H=${{row.high?.toFixed(2)}} L=${{row.low?.toFixed(2)}} C=${{row.close?.toFixed(2)}} | BP=${{row.bp?.toFixed(4)}} | MinCorr=${{row.mincorr?.toFixed(4)}} | DC=${{row.dc?.toFixed(2)}}`;
}});
</script>
</body>
</html>
"""


def render_lightweight_chart(rows, output_file, title='MXM6 / AutoTuneFilter'):
    """Сохраняет standalone HTML-график на TradingView Lightweight Charts."""
    output_path = Path(output_file).resolve()

    html = HTML_TEMPLATE.format(
        title=title,
        rows_json=json.dumps(rows, ensure_ascii=False, separators=(',', ':')),
    )

    output_path.write_text(html, encoding='utf-8')
    return output_path


if __name__ == '__main__':
    cerebro = bt.Cerebro(stdstats=False)

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
    cerebro.addstrategy(AutoTuneChartStrategy, window=20)

    results = cerebro.run()
    strat = results[0]

    output_path = render_lightweight_chart(
        strat.chart_rows,
        Path(__file__).with_name('atf_lightweight_chart.html'),
        title='MXM6 / AutoTuneFilter / Lightweight Charts'
    )

    print(f'Собрано баров для графика: {len(strat.chart_rows)}')
    print(f'HTML-график сохранён: {output_path}')

    webbrowser.open(output_path.as_uri())
