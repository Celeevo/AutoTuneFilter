import os
from datetime import datetime

from runners import run


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
    write_history=False,
    risk=5,  #range(3, 6),
    window=range(25, 106, 10),
    bandwidth=[i / 100 for i in range(15, 41, 5)],
    thresh=[-i / 100 for i in range(40, 71, 5)],
    allow_short=False,
    printlog=False,
    tp_mult=[i / 10 for i in range(1, 14, 3)],
    min_dc=range(0, 41, 10)
)

RUN_SETTINGS = dict(
    start_cash=300000.0,
    instrument_type='stocks', # 'futures' 'stocks'
    run_mode='optimize',
    capital_mode='fixed',
    exit_mode='bracket',
    close_on_expiration=True,
    expiration_exit_bar=3,
    params=STRATEGY_PARAMS,
    tf='1h',
    start_date='2025-6-20',
    end_date=datetime.today(),
    main_opt_metric='PROM',
    sec='SBER', # 'SBER' 'SBRF'
)

if __name__ == '__main__':
    maxcpus = os.cpu_count()
    available_cpus = max(1, maxcpus - 2)
    print(f'Задействуем {available_cpus} потоков из {maxcpus} возможных.')
    run(RUN_SETTINGS, maxcpus=available_cpus)
