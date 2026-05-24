import os
from datetime import datetime

from runners import run

STRATEGY_PARAMS = dict(
    write_history=False,
    risk=5,
    window=67,
    bandwidth=0.5,
    thresh=-0.55,
    allow_short=True,
    printlog=False,
    tp_mult=1.5,
    min_dc=35,
)

# STRATEGY_PARAMS = dict(
#     write_history=False,
#     risk=5,
#     window=range(50, 96, 5),
#     bandwidth=[i / 100 for i in range(10, 71, 10)],
#     thresh=[-i / 100 for i in range(10, 71, 10)],
#     allow_short=False,
#     printlog=False,
#     tp_mult=[i / 10 for i in range(1, 21, 5)],
#     min_dc=0,
# )

RUN_SETTINGS = dict(
    start_cash=300000.0,
    instrument_type='futures', # 'stocks' 'futures'
    run_mode='optimize', # 'optimize' 'single_plot'
    plot_data_index=0,
    plot_data_name=None,
    cerebroview_plot_kwargs={},
    # fixed      — каждый контракт/инструмент запускается с одинакового start_cash.
    # cumulative — капитал переносится от контракта к контракту отдельно
    #              для каждой комбинации параметров.
    capital_mode='cumulative',
    exit_mode='bracket',
    close_on_expiration=True,
    expiration_exit_bar=3,
    params=STRATEGY_PARAMS,
    tf='1h',
    start_date='2023-6-20',
    end_date=datetime.today(),
    main_opt_metric='PROM',
    sec='SBRF',  #  'SBRF' 'SBER'
)

if __name__ == '__main__':
    maxcpus = os.cpu_count()
    available_cpus = max(1, maxcpus - 2)
    print(f'Задействуем {available_cpus} потоков из {maxcpus} возможных.')
    run(RUN_SETTINGS, maxcpus=available_cpus)
