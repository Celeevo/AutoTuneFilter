from datetime import datetime

from runners import run_single_plot_from_settings


STRATEGY_PARAMS = dict(
    # В single-прогоне write_history принудительно включается внутри runners.py,
    # чтобы Excel-файл всегда содержал результат, сделки и ордера.
    risk=5,
    window=71,
    bandwidth=0.36,
    thresh=-0.5,
    allow_short=False,
    printlog=False,
    tp_mult=0.45,
)

RUN_SETTINGS = dict(
    start_cash=300000.0,
    instrument_type='stocks',   # 'stocks' / 'futures'
    exit_mode='bracket',        # 'bracket' / 'ehlers'
    params=STRATEGY_PARAMS,
    tf='1h',
    start_date='2023-6-20',
    end_date=datetime.today(),
    sec='SBER',  # 'SBER' / 'SBRF'

    # ВАЖНО для futures:
    # run_single_plot.py строит график только по одному конкретному контракту.
    # Это не склейка всей фьючерсной серии и не cumulative-прогон по нескольким
    # последовательным контрактам. Если между датами найдено несколько контрактов,
    # contract нужно задать явно, иначе запуск остановится с понятной ошибкой.
    # Пример: contract='SRM6'  # укажите фактическое имя контракта из списка загрузки
    contract=None, # 'None'  'SRM6'
)

if __name__ == '__main__':
    run_single_plot_from_settings(RUN_SETTINGS)
