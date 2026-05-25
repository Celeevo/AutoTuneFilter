import os
from datetime import datetime

from runners import run_optimization_from_settings


# STRATEGY_PARAMS = dict(
#     write_history=False,
#     risk=range(4, 7),
#     window=range(65, 68),
#     bandwidth=[i / 100 for i in range(50, 61, 10)],
#     thresh=[-i / 100 for i in range(50, 66, 5)],
#     allow_short=True,
#     printlog=False,
#     tp_mult=[i / 10 for i in range(15, 19)],
#     min_dc=range(25, 46, 5),
# )

STRATEGY_PARAMS = dict(
    # Для большой оптимизации история сделок/ордеров по умолчанию не пишется,
    # чтобы не раздувать Excel-файл. Включайте True только для диагностики.
    write_history=False,
    risk=5,  #range(3, 6),
    window=range(25, 106, 10),
    bandwidth=[i / 100 for i in range(20, 71, 10)],
    thresh=[-i / 100 for i in range(40, 71, 5)],
    allow_short=True,
    printlog=False,
    tp_mult=[i / 10 for i in range(12, 19, 2)],
    min_dc=0, # range(25, 46, 5),
)

RUN_SETTINGS = dict(
    start_cash=300000.0,
    instrument_type='futures', # 'futures' 'stocks'
    capital_mode='fixed',
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
