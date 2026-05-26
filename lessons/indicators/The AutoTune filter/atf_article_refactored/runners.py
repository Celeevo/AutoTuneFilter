import gc
import os
import re
import sys
import time as _time
from datetime import datetime

import backtrader as bt
import pandas as pd
from moex_store import MoexStore
from tqdm import tqdm

from atf_strategy import DATA_END_EXIT_BARS, AutoTuneFilterEhlersStrategy, AutoTuneFilterStrategy
from moex_setup import get_commission_info, load_moex_datas, normalize_instrument_type
from params_tools import count_param_variants, expand_param_combinations, iterable_params, to_single_strategy_params
from reporting import SmartAnalyzer, add_drawdown_metrics, aggregate_df

_OPT_PBAR = None


def opt_progress_cb(_):
    global _OPT_PBAR
    if _OPT_PBAR is not None:
        _OPT_PBAR.update(1)


def _script_version():
    script_name = os.path.splitext(os.path.basename(sys.argv[0]))[0]
    match = re.search(r'(prod\d+)', script_name, re.IGNORECASE)
    return match.group(1).lower() if match else script_name


def _set_script_version():
    global SCRIPT_VERSION
    SCRIPT_VERSION = _script_version()

SCRIPT_VERSION = _script_version()


def _data_name(data):
    return str(getattr(data.p, 'name', ''))


def _available_data_names(datas):
    return [_data_name(data) for data in datas]


def _select_single_plot_data(settings, datas, instrument_type, sec):
    """
    Выбирает один data feed для run_single_plot.py.

    ВАЖНО: для фьючерсов режим single_plot строит график только по одному
    конкретному контракту. Это не склейка всей серии контрактов и не
    cumulative-прогон по нескольким контрактам.
    """
    if not datas:
        raise ValueError('Нет загруженных данных для single_plot')

    contract = settings.get('contract')
    names = _available_data_names(datas)

    if instrument_type == 'futures':
        if contract:
            selected = [data for data in datas if _data_name(data) == str(contract)]
            if not selected:
                raise ValueError(
                    f"contract='{contract}' не найден среди загруженных контрактов {sec}. "
                    f"Доступные контракты: {', '.join(names)}"
                )
            data = selected[0]
        else:
            if len(datas) > 1:
                raise ValueError(
                    f"Для run_single_plot.py по фьючерсу {sec} нужно явно задать "
                    f"RUN_SETTINGS['contract']. Между указанными датами найдено "
                    f"несколько контрактов: {', '.join(names)}.\n"
                    f"single_plot строит один график CerebroView по одному контракту, "
                    f"а не склейку всей фьючерсной серии."
                )
            data = datas[0]

        print(
            f"[single_plot] Фьючерс {sec}: для графика выбран контракт {_data_name(data)}. "
            f"Это один контракт, не склейка последовательных контрактов."
        )
        return data

    if contract:
        print(f"[single_plot] Параметр contract='{contract}' игнорируется для акций.")

    if len(datas) != 1:
        raise ValueError(
            f"Для акции {sec} ожидался один data feed, но загружено {len(datas)}: "
            f"{', '.join(names)}"
        )

    return datas[0]


def _write_single_result_excel(sec, tf, settings, data, instrument_type, strategy_params,
                               analysis, analyzer, start_cash, exit_mode):
    """Сохраняет результат одиночного прогона run_single_plot.py в Excel."""
    timestamp = datetime.now().strftime('%d-%m-%y %H-%M')
    results_file = f'single_run_{sec}_{tf}_{timestamp}.xlsx'

    result_row = dict(analysis)
    result_row['Data'] = _data_name(data)
    result_row['InstrumentType'] = instrument_type
    result_row['ExitMode'] = exit_mode

    result_df = pd.DataFrame([result_row]).round(2)
    for col in ('PNLs', 'Asset'):
        if col in result_df.columns:
            del result_df[col]

    trades_df = pd.DataFrame(analyzer.get_trades()).round(3)
    orders_df = pd.DataFrame(analyzer.get_orders()).round(3)

    params_rows = [
        ('start_cash', start_cash),
        ('instrument_type', instrument_type),
        ('sec', sec),
        ('data', _data_name(data)),
        ('tf', tf),
        ('start_date', settings.get('start_date')),
        ('end_date', settings.get('end_date')),
        ('exit_mode', exit_mode),
        ('data_end_exit_rule', f'close position {DATA_END_EXIT_BARS} bars before data end'),
    ]
    params_rows.extend(strategy_params.items())
    params_df = pd.DataFrame(params_rows, columns=['Parameter', 'Value'])

    with pd.ExcelWriter(results_file, engine='xlsxwriter') as writer:
        result_df.to_excel(writer, sheet_name='result', index=False)
        trades_df.to_excel(writer, sheet_name='trades', index=False)
        orders_df.to_excel(writer, sheet_name='orders', index=False)
        params_df.to_excel(writer, sheet_name='params', index=False)

    print(f"[single_plot] Результаты одиночного прогона сохранены в файл '{results_file}'.")

    try:
        os.startfile(results_file)
    except AttributeError:
        pass



def run_single_plot(settings, datas, instrument_type, params, start_cash, exit_mode, sec, tf):
    """
    Запускает один прогон через cerebro.addstrategy(), сохраняет Excel
    с результатом и показывает график через CerebroView.
    """
    data = _select_single_plot_data(settings, datas, instrument_type, sec)

    strategy_params = dict(
        to_single_strategy_params(params),
        write_history=True,       # single-прогон всегда сохраняет сделки/ордера в Excel
        show_equity_dd=True,
    )

    strategy_cls = AutoTuneFilterStrategy if exit_mode == 'bracket' else AutoTuneFilterEhlersStrategy

    cerebro = bt.Cerebro(stdstats=True, runonce=False)
    cerebro.broker = bt.brokers.BackBroker()
    cerebro.broker.setcash(start_cash)
    cerebro.broker.addcommissioninfo(
        get_commission_info(data.sec, instrument_type),
        name=data.p.name,
    )

    cerebro.adddata(data)
    cerebro.addstrategy(strategy_cls, **strategy_params)

    analyzer_params = dict(
        it_params=iterable_params(strategy_params),
        asset=data.sec,
    )
    cerebro.addanalyzer(SmartAnalyzer, _name='full', **analyzer_params)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='dd')

    print(
        f"[single_plot] Запуск {_data_name(data)} | instrument_type={instrument_type} | "
        f"exit_mode={exit_mode} | start_cash={start_cash:.2f}"
    )
    print(f"[single_plot] params={strategy_params}")

    results = cerebro.run(
        stdstats=True,
        runonce=False,
        tradehistory=True,
        maxcpus=1,
    )

    strategy = results[0]
    analyzer = strategy.analyzers.full
    analysis = dict(analyzer.get_analysis())
    add_drawdown_metrics(strategy, analysis)

    print(
        f"[single_plot] EndValue={analysis.get('EndValue', cerebro.broker.getvalue()):.2f} | "
        f"PNL={analysis.get('ContractPNL', 0):.2f} | "
        f"WinTr={analysis.get('WinTr', 0)} | LossTr={analysis.get('LossTr', 0)} | "
        f"MaxDDPct={analysis.get('MaxDDPct', 0):.2f} | "
        f"MaxDDMoney={analysis.get('MaxDDMoney', 0):.2f}"
    )

    _write_single_result_excel(
        sec=sec,
        tf=tf,
        settings=settings,
        data=data,
        instrument_type=instrument_type,
        strategy_params=strategy_params,
        analysis=analysis,
        analyzer=analyzer,
        start_cash=start_cash,
        exit_mode=exit_mode,
    )

    try:
        from cerebroview import plot as cerebroview_plot
    except ImportError as exc:
        raise ImportError(
            "Не удалось импортировать CerebroView. Убедитесь, что пакет/папка cerebroview "
            "доступна из текущего проекта. Для режима single_plot нужен импорт: "
            "from cerebroview import plot"
        ) from exc

    cerebroview_plot(cerebro)

    return results


def run_single_plot_from_settings(settings):
    """Публичный вход для run_single_plot.py."""
    _set_script_version()

    if settings is None:
        raise ValueError('settings must be provided')

    start_cash = float(settings.get('start_cash', 300000.0))
    instrument_type = normalize_instrument_type(settings.get('instrument_type', 'futures'))
    exit_mode = str(settings.get('exit_mode', 'bracket')).lower()
    params = dict(settings['params'])
    tf = settings.get('tf', '1h')
    start_date = settings.get('start_date', '2023-6-20')
    end_date = settings.get('end_date') or datetime.today()
    sec = settings.get('sec', 'SPYF')

    if exit_mode not in ('bracket', 'ehlers'):
        raise ValueError("exit_mode должен быть 'bracket' или 'ehlers'")

    store = MoexStore()
    datas = load_moex_datas(
        store=store,
        sec=sec,
        instrument_type=instrument_type,
        start_date=start_date,
        end_date=end_date,
        tf=tf,
    )

    return run_single_plot(
        settings=settings,
        datas=datas,
        instrument_type=instrument_type,
        params=params,
        start_cash=start_cash,
        exit_mode=exit_mode,
        sec=sec,
        tf=tf,
    )


def run_optimization_from_settings(settings, maxcpus=None):
    # Фильтр AutoTune https://financial-hacker.com/the-autotune-filter/
    global _OPT_PBAR
    _set_script_version()

    if settings is None:
        raise ValueError('settings must be provided')

    start_cash = float(settings.get('start_cash', 300000.0))
    instrument_type = normalize_instrument_type(settings.get('instrument_type', 'futures'))
    capital_mode = settings.get('capital_mode', 'fixed')
    exit_mode = settings.get('exit_mode', 'bracket')

    params = dict(settings['params'])

    tf = settings.get('tf', '1h')
    start_date = settings.get('start_date', '2023-6-20')
    end_date = settings.get('end_date') or datetime.today()
    main_opt_metric = settings.get('main_opt_metric', 'PROM')
    sec = settings.get('sec', 'SPYF')

    total_time = _time.time()
    store = MoexStore()

    datas = load_moex_datas(
        store=store,
        sec=sec,
        instrument_type=instrument_type,
        start_date=start_date,
        end_date=end_date,
        tf=tf,
    )

    variants = count_param_variants(params)

    sheet_size = (variants * len(datas)) > 1048576
    if sheet_size:
        print(f"Excel sheet is too large! Your sheet size is: {variants * len(datas)}, Max sheet size is: 1'048'576")

    item_name = 'контрактов' if instrument_type == 'futures' else 'инструментов'
    print(f'Рассчитываем {variants} вариантов стратегии для '
          f'каждого из {len(datas)} {item_name}. Итого '
          f'{variants * len(datas)} вариантов.')
    print(f'Время пошло, {datetime.now():%H:%M:%S}')

    results = []
    trades = []
    orders = []
    analyzer_params = dict(it_params=iterable_params(params))

    if instrument_type == 'stocks':
        if capital_mode != 'fixed':
            print('[optimization] Для акций capital_mode игнорируется; используется fixed.')
        capital_mode = 'fixed'

    if capital_mode not in ('fixed', 'cumulative'):
        raise ValueError("capital_mode должен быть 'fixed' или 'cumulative'")

    exit_mode = str(exit_mode).lower()
    if exit_mode not in ('bracket', 'ehlers'):
        raise ValueError("exit_mode должен быть 'bracket' или 'ehlers'")

    if capital_mode == 'fixed':
        # Старый режим: каждый контракт тестируется независимо и стартует
        # с одинакового депозита start_cash.
        for data in datas:
            analyzer_params['asset'] = data.sec
            st_time = _time.time()
            cerebro = bt.Cerebro()
            cerebro.broker = bt.brokers.BackBroker()
            cerebro.broker.setcash(start_cash)
            cerebro.broker.addcommissioninfo(get_commission_info(data.sec, instrument_type), name=data.p.name)
            cerebro.addanalyzer(SmartAnalyzer, _name='full', **analyzer_params)
            cerebro.addanalyzer(bt.analyzers.DrawDown, _name='dd')
            cerebro.adddata(data)

            strategy_params = dict(params)
            if exit_mode == 'bracket':
                cerebro.optstrategy(AutoTuneFilterStrategy, **strategy_params)
            else:
                cerebro.optstrategy(AutoTuneFilterEhlersStrategy, **strategy_params)
            if tqdm is not None:
                _OPT_PBAR = tqdm(
                    total=variants,
                    desc=data.p.name,
                    dynamic_ncols=True,
                    unit='var',
                    file=sys.stdout,
                )
                cerebro.optcallback(opt_progress_cb)

            runs = cerebro.run(stdstats=False, tradehistory=params.get('write_history', False), maxcpus=maxcpus)

            if _OPT_PBAR is not None:
                _OPT_PBAR.close()
                _OPT_PBAR = None

            for run in runs:  # тут все варианты для одного контракта
                for strategy in run:  # тут уникальные варианты по параметрам
                    analyzer = strategy.analyzers.full
                    analysis = dict()
                    analysis.update(analyzer.get_analysis())
                    add_drawdown_metrics(strategy, analysis)
                    analysis['Data'] = data.p.name
                    analysis['PNLs'] = analyzer.get_trades_pnl()
                    analysis['CapitalMode'] = capital_mode
                    results.append(analysis)

                    if params.get('write_history', False):
                        trades_data = analyzer.get_trades()
                        for tr in trades_data:
                            tr['capital_mode'] = capital_mode
                        trades.extend(trades_data)

                        orders_data = analyzer.get_orders()
                        for order_row in orders_data:
                            order_row['capital_mode'] = capital_mode
                        orders.extend(orders_data)

            print(
                f'Прогон {len(runs)} вариантов стратегии для контракта '
                f'{data.p.name} за {round(_time.time() - st_time, 2)} сек., '
                f'{round((_time.time() - st_time) / 60, 2)} мин., '
                f'V (скорость) = {round(len(runs) / (_time.time() - st_time), 2)} вар/сек, '
                f'{str(datetime.now().time())[:5]}'
            )
            gc.collect()

    else:
        # Кумулятивный режим: одна комбинация параметров последовательно проходит
        # все контракты. Финальная стоимость счёта после контракта N становится
        # стартовым капиталом для контракта N+1 той же комбинации параметров.
        param_variants = list(expand_param_combinations(params))
        total_runs = len(param_variants) * len(datas)

        if tqdm is not None:
            _OPT_PBAR = tqdm(
                total=total_runs,
                desc='cumulative',
                dynamic_ncols=True,
                unit='run',
                file=sys.stdout,
            )

        for variant_no, strategy_params in enumerate(param_variants, start=1):
            current_cash = start_cash
            variant_time = _time.time()

            for data in datas:
                analyzer_params['asset'] = data.sec
                st_time = _time.time()

                cerebro = bt.Cerebro()
                cerebro.broker = bt.brokers.BackBroker()
                cerebro.broker.setcash(current_cash)
                cerebro.broker.addcommissioninfo(get_commission_info(data.sec, instrument_type), name=data.p.name)
                cerebro.addanalyzer(SmartAnalyzer, _name='full', **analyzer_params)
                cerebro.addanalyzer(bt.analyzers.DrawDown, _name='dd')
                cerebro.adddata(data)
                run_strategy_params = dict(strategy_params)
                if exit_mode == 'bracket':
                    cerebro.addstrategy(AutoTuneFilterStrategy, **run_strategy_params)
                else:
                    cerebro.addstrategy(AutoTuneFilterEhlersStrategy, **run_strategy_params)

                runs = cerebro.run(stdstats=False, tradehistory=params.get('write_history', False))
                strategy = runs[0]
                analyzer = strategy.analyzers.full

                analysis = dict()
                analysis.update(analyzer.get_analysis())
                add_drawdown_metrics(strategy, analysis)
                analysis['Data'] = data.p.name
                analysis['PNLs'] = analyzer.get_trades_pnl()
                analysis['CapitalMode'] = capital_mode
                results.append(analysis)

                if params.get('write_history', False):
                    trades_data = analyzer.get_trades()
                    for tr in trades_data:
                        tr['capital_mode'] = capital_mode
                    trades.extend(trades_data)

                    orders_data = analyzer.get_orders()
                    for order_row in orders_data:
                        order_row['capital_mode'] = capital_mode
                    orders.extend(orders_data)

                # Для следующего контракта используем финальную стоимость счёта.
                # Позиция закрывается за DATA_END_EXIT_BARS баров до конца data feed,
                # поэтому EndValue не должен включать незакрытый mark-to-market хвост.
                current_cash = analysis.get('EndValue', cerebro.broker.getvalue())

                if _OPT_PBAR is not None:
                    _OPT_PBAR.update(1)
                    _OPT_PBAR.set_postfix_str(f'{variant_no}/{len(param_variants)} {data.p.name}')

                print(
                    f'Кумулятивный прогон {variant_no}/{len(param_variants)}, '
                    f'контракт {data.p.name}: start={analysis.get("StartCash", 0):.2f}, '
                    f'end={analysis.get("EndValue", 0):.2f}, '
                    f'PNL={analysis.get("ContractPNL", 0):.2f}, '
                    f'{round(_time.time() - st_time, 2)} сек., '
                    f'{str(datetime.now().time())[:5]}'
                )
                gc.collect()

            print(
                f'Комбинация {variant_no}/{len(param_variants)} прошла все контракты за '
                f'{round((_time.time() - variant_time) / 60, 2)} мин.'
            )

        if _OPT_PBAR is not None:
            _OPT_PBAR.close()
            _OPT_PBAR = None

    print(f'Весь прогон за {round(_time.time() - total_time, 2)} сек., '
          f'{round((_time.time() - total_time) / 3600, 2)} часов.')

    df1 = pd.DataFrame(results).round(2)
    if params.get('write_history', False):
        df2 = pd.DataFrame(trades).round(3)
        df_orders = pd.DataFrame(orders).round(3)
    df3 = aggregate_df(df1, start_cash, sort_by=main_opt_metric)

    # В results оставляем только основные метрики. MaxDDLen скрываем, потому что
    # для статьи достаточно MaxDDPct и MaxDDMoney.
    results_drop_cols = [
        'MaxDDLen',
        'Asset',
    ]
    df3 = df3.drop(columns=[col for col in results_drop_cols if col in df3.columns])
    df4 = pd.DataFrame(
        list(params.items()) + [
            ('start_cash', start_cash),
            ('instrument_type', instrument_type),
            ('capital_mode', capital_mode),
            ('exit_mode', exit_mode),
            ('data_end_exit_rule', f'close position {DATA_END_EXIT_BARS} bars before data end'),
            ('start_date', start_date),
            ('end_date', end_date),
        ],
        columns=['Parameter', 'Value']
    )
    for col in ('PNLs', 'Asset'):
        if col in df1.columns:
            del df1[col]

    # Сохраняем штамп времени для имени XLSX-файла с результатами
    timestamp = datetime.now().strftime("%d-%m-%y %H-%M")

    # Создаём имя XLSX-файла результатов. Версия берётся из имени
    # запускаемого скрипта: например, atf_strat_opt4_xlsx_prod12.py -> prod12.
    results_file = f'opt_results_{SCRIPT_VERSION}_{sec}_{tf}_{timestamp}.xlsx'

    # Записываем df в xlsx файл, xlsxwriter импортируем
    # отдельно pip install xlsxwriter
    with pd.ExcelWriter(results_file, engine='xlsxwriter') as writer:
        if not sheet_size:
            if instrument_type == 'futures':
                df1.to_excel(writer, sheet_name='by Contracts', index=False)
            if params.get('write_history', False):
                df2.to_excel(writer, sheet_name='trades', index=False)
                if not df_orders.empty:
                    df_orders.to_excel(writer, sheet_name='orders', index=False)
        df3.to_excel(writer, sheet_name='results', index=False)
        df4.to_excel(writer, sheet_name='params', index=False)

    print(f"Результаты успешно сохранены в файл '{results_file}'.")
    os.startfile(results_file)

