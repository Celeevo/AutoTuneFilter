from datetime import datetime
from moex_store import MoexStore
import math
import backtrader as bt

import math
import backtrader as bt

class AutoTuneFilterTV(bt.Indicator):
    """
    John Ehlers - AutoTune Filter
    Версия, приведённая к алгоритму текущего Pine-скрипта TradingView.

    Важно:
    Это НЕ "статья-в-статью" по оригинальному EasyLanguage.
    Это именно версия под текущую реализацию TradingView.

    Линии индикатора:
    - bp      : tuned band-pass filter (главный выход)
    - filt    : high-pass filter
    - mincorr : minimum rolling autocorrelation
    - dc      : dominant cycle

    Главные особенности этой версии:
    1. High-pass начинает считаться не сразу, а только с 5-го бара,
       как в текущем Pine-коде (bar_index >= 4).
    2. Band-pass начинает считаться с 4-го бара,
       как в Pine-коде (bar_index >= 3).
    3. Корреляция не считается по частичному окну.
       Если полного окна для данного lag ещё нет, corr = 1.0.
    4. Мы не подставляем "ближайшую цену" при нехватке истории.
       Это было в старой версии, но это не Pine-логика.
    """

    lines = ('bp', 'filt', 'mincorr', 'dc')

    params = (
        ('window', 20),
        ('bandwidth', 0.25),
        ('output', 'bp'),  # оставлен для совместимости, на расчёт не влияет
    )

    plotinfo = dict(subplot=True)

    plotlines = dict(
        bp=dict(_name='AutoTune BP'),
        filt=dict(_name='HighPass', _plotskip=True),
        mincorr=dict(_name='MinCorr', _plotskip=True),
        dc=dict(_name='DominantCycle', _plotskip=True),
    )

    # ------------------------------------------------------------
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ
    # ------------------------------------------------------------

    @staticmethod
    def _nz(value, default=0.0):
        """
        Аналог Pine-функции nz():
        если значение None или nan -> возвращаем default.
        """
        if value is None:
            return default
        try:
            if math.isnan(value):
                return default
        except TypeError:
            pass
        return value

    def _prev(self, line, bars_back=1, default=0.0):
        """
        Безопасный доступ к прошлым значениям собственной линии индикатора.

        Здесь защита нужна именно для рекурсивных формул:
        - filt использует filt[1] и filt[2]
        - bp использует bp[1] и bp[2]

        Если прошлой истории линии ещё нет, подставляем default.
        Это близко к логике Pine через nz(res[1]).
        """
        if len(self) > bars_back:
            return self._nz(line[-bars_back], default)
        return default

    # ------------------------------------------------------------
    # ЖИЗНЕННЫЙ ЦИКЛ ИНДИКАТОРА
    # ------------------------------------------------------------

    def prenext(self):
        self._step()

    def nextstart(self):
        self._step()

    def next(self):
        self._step()

    # ------------------------------------------------------------
    # ОСНОВНОЙ РАСЧЁТ
    # ------------------------------------------------------------

    def _step(self):
        window = int(self.p.window)
        bandwidth = float(self.p.bandwidth)

        # ============================================================
        # 1) HIGH-PASS FILTER
        # ============================================================
        #
        # Pine-код:
        #   if bar_index >= 4
        #       res := ...
        #
        # В Backtrader первый бар имеет len(self) == 1,
        # значит это эквивалентно:
        #   если len(self) < 5 -> filt = 0
        #
        # Это ВАЖНО:
        # здесь мы НЕ пытаемся "дотянуть" расчёт на ранних барах
        # через замену недостающих цен. В Pine этого нет.
        # ============================================================
        if len(self) < 5:
            filt = 0.0
        else:
            w = 1.414 * math.pi / window
            q = math.exp(-w)

            c1 = 2.0 * q * math.cos(w)
            c2 = q * q
            a0 = 0.25 * (1.0 + c1 + c2)

            filt = (
                a0 * (self.data[0] - 2.0 * self.data[-1] + self.data[-2])
                + c1 * self._prev(self.l.filt, 1, 0.0)
                - c2 * self._prev(self.l.filt, 2, 0.0)
            )

        self.l.filt[0] = filt

        # ============================================================
        # 2) ROLLING AUTOCORRELATION
        # ============================================================
        #
        # В статье раньше мы считали корреляцию по "частично доступному окну".
        # Но для версии TradingView это не лучшая идея.
        #
        # Здесь делаем так:
        # - для каждого lag от 1 до window
        # - если полного окна ещё нет -> corr = 1.0
        # - если окно есть -> считаем полную корреляцию длины window
        #
        # Почему именно 1.0?
        # Потому что в Pine-коде стоит:
        #   acf.set(i, nz(corr, 1))
        #
        # То есть если corr невалиден, в массив идёт 1.
        # ============================================================
        mincorr = 1.0
        best_lag = 1

        for lag in range(1, window + 1):
            # Чтобы посчитать корреляцию строго по полному окну длины window,
            # нам нужно иметь доступ к:
            #   filt[0], filt[1], ..., filt[window-1]
            # и
            #   filt[lag], filt[lag+1], ..., filt[lag+window-1]
            #
            # В терминах Backtrader это означает:
            #   len(self) >= window + lag
            #
            # Если истории ещё не хватает, используем corr = 1.0,
            # как в Pine через nz(corr, 1).
            if len(self) < window + lag:
                corr = 1.0
            else:
                sx = sy = sxx = sxy = syy = 0.0

                # Здесь считаем ту же корреляцию, что и в статье:
                # Corr = (Window*Sxy - Sx*Sy) /
                #        sqrt((Window*Sxx - Sx*Sx)*(Window*Syy - Sy*Sy))
                #
                # Но считаем её только тогда, когда окно полностью доступно.
                for j in range(window):
                    x = self.l.filt[-j]            # filt[j] в терминах статьи
                    y = self.l.filt[-(lag + j)]    # filt[lag + j]

                    sx += x
                    sy += y
                    sxx += x * x
                    sxy += x * y
                    syy += y * y

                cov = window * sxy - sx * sy
                vx = window * sxx - sx * sx
                vy = window * syy - sy * sy

                if vx > 0.0 and vy > 0.0:
                    corr = cov / math.sqrt(vx * vy)
                else:
                    # Pine делает nz(corr, 1)
                    # Значит при плохом знаменателе берём 1.0
                    corr = 1.0

            if corr < mincorr:
                mincorr = corr
                best_lag = lag

        self.l.mincorr[0] = mincorr

        # ============================================================
        # 3) DOMINANT CYCLE
        # ============================================================
        #
        # Как в статье и Pine:
        #   DC = 2 * lag с минимальной корреляцией
        #
        # Затем ограничиваем изменение dc не более чем на +/-2
        # относительно предыдущего бара.
        #
        # В Pine:
        #   dc := nz(min(max(dc, dc[1] - 2), dc[1] + 2), dc)
        #
        # Здесь делаем ту же логику явно.
        # ============================================================
        dc = 2.0 * best_lag

        if len(self) > 1:
            prev_dc = self._nz(self.l.dc[-1], dc)
            dc = min(max(dc, prev_dc - 2.0), prev_dc + 2.0)

        self.l.dc[0] = dc

        # ============================================================
        # 4) TUNED BAND-PASS FILTER
        # ============================================================
        #
        # Pine-код:
        #   if bar_index >= 3
        #       res := ...
        #
        # В Backtrader это означает:
        #   если len(self) < 4 -> bp = 0
        #
        # Это снова важно:
        # мы здесь НЕ пытаемся "считать хоть как-то" раньше времени.
        # Делаем именно так, как ведёт себя текущий Pine-код.
        # ============================================================
        if len(self) < 4:
            bp = 0.0
        else:
            w0 = 2.0 * math.pi / dc
            l1 = math.cos(w0)
            g1 = math.cos(w0 * bandwidth)

            # Эта защита не меняет алгоритм, а только спасает от редких
            # численных артефактов float-арифметики.
            if abs(g1) < 1e-12:
                bp = self._prev(self.l.bp, 1, 0.0)
            else:
                inner = 1.0 / (g1 * g1) - 1.0

                # Если inner стал чуть меньше нуля только из-за float-ошибки,
                # прижимаем к нулю.
                if inner < 0.0 and abs(inner) < 1e-12:
                    inner = 0.0

                if inner < 0.0:
                    bp = self._prev(self.l.bp, 1, 0.0)
                else:
                    s1 = 1.0 / g1 - math.sqrt(inner)

                    bp = (
                        0.5 * (1.0 - s1) * (self.data[0] - self.data[-2])
                        + l1 * (1.0 + s1) * self._prev(self.l.bp, 1, 0.0)
                        - s1 * self._prev(self.l.bp, 2, 0.0)
                    )

        self.l.bp[0] = bp

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

class AutoTuneFilter1(bt.Indicator):
    """
    John Ehlers - AutoTune Filter
    Исправленная реализация для Backtrader.

    Главный выход: bp
    Дополнительные линии:
    - filt    : high-pass filtered series
    - mincorr : minimum rolling correlation
    - dc      : dominant cycle
    """

    lines = ('bp', 'filt', 'mincorr', 'dc',)

    params = (
        ('window', 20),
        ('bandwidth', 0.25),
        ('output', 'bp'),
    )

    plotinfo = dict(subplot=True)

    plotlines = dict(
        bp=dict(_name='AutoTune BP', _plotskip=True),
        filt=dict(_name='HighPass'),
        mincorr=dict(_name='MinCorr', _plotskip=True),
        dc=dict(_name='DominantCycle', _plotskip=True),
    )

    def _nz(self, value, default=0.0):
        """Заменяет nan/None на default."""
        if value is None:
            return default
        try:
            if math.isnan(value):
                return default
        except TypeError:
            pass
        return value

    def _data(self, ago):
        """
        Безопасный доступ к цене.
        Если истории не хватает, возвращаем ближайшее доступное значение.
        """
        if ago == 0:
            return self.data[0]

        if ago == -1:
            return self.data[-1] if len(self) > 1 else self.data[0]

        if ago == -2:
            if len(self) > 2:
                return self.data[-2]
            elif len(self) > 1:
                return self.data[-1]
            return self.data[0]

        # Для более глубоких обращений в корреляции
        idx_needed = abs(ago)
        if len(self) > idx_needed:
            return self.data[ago]

        # Если глубокой истории нет — возвращаем самое старое доступное
        return self.data[-(len(self) - 1)] if len(self) > 1 else self.data[0]

    def _line(self, line, ago, default=0.0):
        """
        Безопасный доступ к предыдущим значениям своей линии.
        """
        idx_needed = abs(ago)
        if ago == 0:
            return self._nz(line[0], default)

        if len(self) > idx_needed:
            return self._nz(line[ago], default)

        return default

    def prenext(self):
        self._step()

    def nextstart(self):
        self._step()

    def next(self):
        self._step()

    def _step(self):
        window = int(self.p.window)
        bandwidth = float(self.p.bandwidth)

        # ------------------------------------------
        # 1) High-pass filter
        # ------------------------------------------
        a1 = math.exp(-1.414 * math.pi / window)
        b1 = 2.0 * a1 * math.cos(1.414 * math.pi / window)
        c2 = b1
        c3 = -(a1 * a1)
        c1 = (1.0 + c2 - c3) / 4.0

        price0 = self._data(0)
        price1 = self._data(-1)
        price2 = self._data(-2)

        filt1 = self._line(self.l.filt, -1, 0.0)
        filt2 = self._line(self.l.filt, -2, 0.0)

        filt = (
            c1 * (price0 - 2.0 * price1 + price2) +
            c2 * filt1 +
            c3 * filt2
        )
        self.l.filt[0] = filt

        # ------------------------------------------
        # 2) Rolling autocorrelation
        # ------------------------------------------
        mincorr = 1.0
        best_lag = 1

        max_lag = min(window, max(1, len(self) - 1))

        for lag in range(1, max_lag + 1):
            sx = sy = sxx = sxy = syy = 0.0
            n = 0

            max_j = min(window - 1, len(self) - lag - 1)
            if max_j < 0:
                continue

            for j in range(max_j + 1):
                x = self._line(self.l.filt, -j, 0.0)
                y = self._line(self.l.filt, -(lag + j), 0.0)

                sx += x
                sy += y
                sxx += x * x
                sxy += x * y
                syy += y * y
                n += 1

            if n < 2:
                continue

            denx = n * sxx - sx * sx
            deny = n * syy - sy * sy

            if denx > 0.0 and deny > 0.0:
                corr = (n * sxy - sx * sy) / math.sqrt(denx * deny)
            else:
                corr = 0.0

            if corr < mincorr:
                mincorr = corr
                best_lag = lag

        self.l.mincorr[0] = mincorr

        # ------------------------------------------
        # 3) Dominant cycle
        # ------------------------------------------
        dc = 2.0 * best_lag
        prev_dc = self._line(self.l.dc, -1, dc)

        if dc > prev_dc + 2.0:
            dc = prev_dc + 2.0
        elif dc < prev_dc - 2.0:
            dc = prev_dc - 2.0

        if dc < 2.0:
            dc = 2.0

        self.l.dc[0] = dc

        # ------------------------------------------
        # 4) Tuned band-pass filter
        # ------------------------------------------
        l1 = math.cos(2.0 * math.pi / dc)
        g1 = math.cos(bandwidth * 2.0 * math.pi / dc)

        if abs(g1) < 1e-12:
            self.l.bp[0] = self._line(self.l.bp, -1, 0.0)
            return

        inner = 1.0 / (g1 * g1) - 1.0
        if inner < 0.0 and abs(inner) < 1e-12:
            inner = 0.0
        elif inner < 0.0:
            self.l.bp[0] = self._line(self.l.bp, -1, 0.0)
            return

        s1 = 1.0 / g1 - math.sqrt(inner)

        bp1 = self._line(self.l.bp, -1, 0.0)
        bp2 = self._line(self.l.bp, -2, 0.0)

        bp = (
            0.5 * (1.0 - s1) * (price0 - price2) +
            l1 * (1.0 + s1) * bp1 -
            s1 * bp2
        )
        self.l.bp[0] = bp

class AutoTuneDemoStrategy(bt.Strategy):
    params = (
        ('window', 20),
    )

    def __init__(self):
        self.atf = AutoTuneFilter(
        # self.atf = AutoTuneFilterTV(
            self.data.close,
            window=self.p.window,
        )

    def next(self):
        dt = self.data.datetime.datetime(0)
        print(
            f'{dt} | close={self.data.close[0]:.2f} | '
            f'bp={self.atf.bp[0]:.6f} | '
            f'filt={self.atf.filt[0]:.6f} | '
            f'mincorr={self.atf.mincorr[0]:.6f} | '
            f'dc={self.atf.dc[0]:.2f}'
        )


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
    cerebro.addstrategy(AutoTuneDemoStrategy, window=20)

    results = cerebro.run()
    cerebro.plot(style='candle')