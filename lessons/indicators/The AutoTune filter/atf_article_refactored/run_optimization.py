import os
from datetime import datetime

from runners import run


STRATEGY_PARAMS = dict(
    write_history=False,
    risk=range(4, 7),
    window=range(65, 68),
    bandwidth=[i / 100 for i in range(50, 61, 10)],
    thresh=[-i / 100 for i in range(50, 66, 5)],
    allow_short=True,
    printlog=False,
    tp_mult=[i / 10 for i in range(15, 19)],
    min_dc=range(25, 46, 5),
)

RUN_SETTINGS = dict(
    start_cash=300000.0,
    instrument_type='futures',
    run_mode='optimize',
    capital_mode='fixed',
    exit_mode='bracket',
    close_on_expiration=True,
    expiration_exit_bar=3,
    stock_moexcomm=0.0003,
    stock_brokercomm=0.0003,
    stock_price_step=0.01,
    params=STRATEGY_PARAMS,
    tf='1h',
    start_date='2023-6-20',
    end_date=datetime.today(),
    main_opt_metric='PROM',
    sec='SBRF',
    save_equity_dd_plot=False,
    equity_dd_plot_freq='M',
)

if __name__ == '__main__':
    maxcpus = os.cpu_count()
    available_cpus = max(1, maxcpus - 2)
    print(f'Задействуем {available_cpus} потоков из {maxcpus} возможных.')
    run(RUN_SETTINGS, maxcpus=available_cpus)
