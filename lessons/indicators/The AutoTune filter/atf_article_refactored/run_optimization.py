import os
from datetime import datetime

from runners import run_optimization_from_settings


# STRATEGY_PARAMS = dict(  #SBER
#     write_history=False,
#     risk=5,
#     window=range(58, 73),
#     bandwidth=[i / 100 for i in range(12, 37,2)],
#     thresh=[-i / 100 for i in range(50, 71, 5)],
#     allow_short=False,
#     printlog=False,
#     tp_mult=[i / 100 for i in range(30, 110, 5)],
# )

STRATEGY_PARAMS = dict(  #SBRF
    # Для большой оптимизации история сделок/ордеров по умолчанию не пишется,
    # чтобы не раздувать Excel-файл. Включайте True только для диагностики.
    write_history=False,
    risk=5,
    window=range(50, 81, 5),
    bandwidth=[i / 100 for i in range(50, 71, 10)],
    thresh=[-i / 100 for i in range(40, 71, 5)],
    allow_short=True,
    printlog=False,
    tp_mult=[i / 10 for i in range(12, 21)],
)

RUN_SETTINGS = dict(
    start_cash=300000.0,
    instrument_type='futures', # 'futures' 'stocks'
    capital_mode='fixed',  # 'fixed' 'cumulative'
    exit_mode='bracket',
    params=STRATEGY_PARAMS,
    tf='1h',
    start_date='2023-6-20',
    end_date=datetime.today(),
    main_opt_metric='PROM',
    sec='SBRF', # 'SBER' 'SBRF'
)

if __name__ == '__main__':
    maxcpus = os.cpu_count()
    available_cpus = max(1, maxcpus - 2)
    print(f'Задействуем {available_cpus} потоков из {maxcpus} возможных.')
    run_optimization_from_settings(RUN_SETTINGS, maxcpus=available_cpus)
