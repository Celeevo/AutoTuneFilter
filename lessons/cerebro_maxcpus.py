import os
import time

import backtrader as bt
import pandas as pd


class OneLinePerRunStrategy(bt.Strategy):
    params = (
        ('variant', 0),
        ('sleep_sec', 0),
    )

    def start(self):
        self._started_perf = time.perf_counter()

    def next(self):
        # Искусственно замедляем каждый бар,
        # чтобы разница между последовательным и параллельным запуском была заметна
        time.sleep(self.p.sleep_sec)

    def stop(self):
        finished_perf = time.perf_counter()
        duration = finished_perf - self._started_perf

        # PID (Process ID) — это идентификатор процесса Python.
        # Если PID один и тот же — прогоны шли последовательно в одном процессе.
        # Если PID разный — значит использовалось несколько процессов параллельно.
        pid = os.getpid()

        print(
            f'вариант={self.p.variant:03d} | '
            f'pid={pid} | '
            f'длительность={duration:.3f} сек',
        )


def run_case(maxcpus_value):
    print('=' * 90)
    print(f'ЗАПУСК: maxcpus={maxcpus_value}')
    print('=' * 90, flush=True)

    cerebro = bt.Cerebro()

    df = pd.DataFrame({
        'open':   range(100, 105),
        'high':   range(101, 106),
        'low':    range(99, 104),
        'close':  range(102, 107),
        'volume': range(1000, 1005),
    }, index=pd.date_range('1917-01-01', periods=5, freq='D'))

    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)

    # Несколько вариантов стратегии
    cerebro.optstrategy(
        OneLinePerRunStrategy,
        variant=range(20),
        sleep_sec=[0.05],
    )

    started = time.perf_counter()
    cerebro.run(maxcpus=maxcpus_value)
    total_duration = time.perf_counter() - started

    print(f'ИТОГ: maxcpus={maxcpus_value}, общее время={total_duration:.3f} сек')


if __name__ == '__main__':
    # os.cpu_count() показывает количество доступных логических ядер.
    # Именно на это число обычно ориентируются maxcpus при = None.
    print(f'Доступно логических ядер: {os.cpu_count()}')

    run_case(1)

    # В режиме maxcpus=None каждый процесс печатает в один и тот же терминал.
    # Из-за этого строки вывода могут приходить вперемешку:
    # один процесс ещё не закончил печатать свою строку, а другой уже начал.
    # Поэтому вывод может выглядеть "съехавшим" или неидеально упорядоченным.
    run_case(None)