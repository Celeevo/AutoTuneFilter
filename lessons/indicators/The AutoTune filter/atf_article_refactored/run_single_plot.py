from datetime import datetime

from runners import run_single_plot_from_settings


STRATEGY_PARAMS = dict(
    # В single-прогоне write_history принудительно включается внутри runners.py,
    # чтобы Excel-файл всегда содержал результат, сделки и ордера.
    risk=5,
    window=75,
    bandwidth=0.2,
    thresh=-0.65,
    allow_short=False,
    printlog=False,
    tp_mult=0.45,
    min_dc=0,
)

RUN_SETTINGS = dict(
    start_cash=300000.0,
    instrument_type='stocks',   # 'stocks' / 'futures'
    exit_mode='bracket',        # 'bracket' / 'ehlers'
    # Позиция закрывается автоматически за 3 бара до конца data feed.
    # Для фьючерсов это конец выбранного контракта, для акций — конец истории.
    params=STRATEGY_PARAMS,
    tf='1h',
    start_date='2025-6-20',
    end_date=datetime.today(),
    sec='SBER',  # 'SBER' / 'SBRF'

    # ВАЖНО для futures:
    # run_single_plot.py строит график только по одному конкретному контракту.
    # Это не склейка всей фьючерсной серии и не cumulative-прогон по нескольким
    # последовательным контрактам. Если между датами найдено несколько контрактов,
    # contract нужно задать явно, иначе запуск остановится с понятной ошибкой.
    # Пример: contract='SRM6'  # укажите фактическое имя контракта из списка загрузки
    contract=None,
)

if __name__ == '__main__':
    run_single_plot_from_settings(RUN_SETTINGS)
