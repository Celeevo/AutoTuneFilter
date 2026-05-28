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


# -------------------------- helpers --------------------------

def _script_version():
    """Версия запускаемого скрипта для имени файла с результатами."""
    script_name = os.path.splitext(os.path.basename(sys.argv[0]))[0]
    match = re.search(r'(prod\d+)', script_name, re.IGNORECASE)
    return match.group(1).lower() if match else script_name


def _data_name(data):
    return str(getattr(data.p, 'name', ''))


def _available_data_names(datas):
    return [_data_name(data) for data in datas]


def _strategy_cls(exit_mode):
    return AutoTuneFilterStrategy if exit_mode == 'bracket' else AutoTuneFilterEhlersStrategy


# Активный tqdm-прогрессбар оптимизации.
_opt_pbar = None


def _opt_progress_callback(_strats):
    """Top-level progress callback для cerebro.optcallback (Windows-safe pickle)."""
    pbar = _opt_pbar
    if pbar is not None:
        pbar.update(1)


def _build_cerebro(data, instrument_type, start_cash, analyzer_params):
    """
    Стандартная сборка cerebro: брокер, кэш, комиссия по data.sec, аналайзеры,
    добавленный data feed. Используется и в одиночном прогоне, и в обеих
    ветках оптимизации (fixed / cumulative).
    """
    cerebro = bt.Cerebro()
    cerebro.broker = bt.brokers.BackBroker()
    cerebro.broker.setcash(start_cash)
    cerebro.broker.addcommissioninfo(
        get_commission_info(data.sec, instrument_type),
        name=data.p.name,
    )
    cerebro.addanalyzer(SmartAnalyzer, _name='full', **analyzer_params)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='dd')
    cerebro.adddata(data)
    return cerebro


def _collect_run_results(strategy, data, capital_mode, write_history):
    """
    Собирает результат одной отработанной стратегии: analysis-словарь
    с метриками + готовые trades/orders detail-списки.
    Возвращает (analysis, trades, orders).
    """
    analyzer = strategy.analyzers.full
    analysis = dict(analyzer.get_analysis())
    add_drawdown_metrics(strategy, analysis)
    analysis['Data'] = data.p.name
    analysis['PNLs'] = analyzer.get_trades_pnl()
    analysis['CapitalMode'] = capital_mode

    trades = []
    orders = []
    if write_history:
        for row in analyzer.get_trades():
            row['capital_mode'] = capital_mode
            trades.append(row)
        for row in analyzer.get_orders():
            row['capital_mode'] = capital_mode
            orders.append(row)

    return analysis, trades, orders


# -------------------------- single plot --------------------------

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

    analyzer_params = dict(
        it_params=iterable_params(strategy_params),
        asset=data.sec,
    )
    cerebro = _build_cerebro(data, instrument_type, start_cash, analyzer_params)
    # Для одиночного прогона хотим стандартные обсерверы.
    cerebro.addstrategy(_strategy_cls(exit_mode), **strategy_params)

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


# -------------------------- optimization --------------------------

def _run_fixed_capital(datas, params, instrument_type, start_cash, exit_mode,
                       analyzer_params, capital_mode, variants, maxcpus):
    """
    Режим 'fixed': каждый контракт независимо тестируется на всей сетке
    через cerebro.optstrategy(). Стартовый капитал одинаковый для всех.
    """
    results, trades, orders = [], [], []
    write_history = params.get('write_history', False)
    strategy_cls = _strategy_cls(exit_mode)

    for data in datas:
        analyzer_params['asset'] = data.sec
        st_time = _time.time()

        cerebro = _build_cerebro(data, instrument_type, start_cash, analyzer_params)
        cerebro.optstrategy(strategy_cls, **params)

        pbar = None
        if tqdm is not None:
            pbar = tqdm(
                total=variants,
                desc=data.p.name,
                dynamic_ncols=True,
                unit='var',
                file=sys.stdout,
            )
            # Регистрируем pbar как module-level state и top-level callback.
            # Closure здесь нельзя: optcallback хранится в cerebro и пиклится
            # в worker'ы — local-функция ломает pickle на Windows.
            globals()['_opt_pbar'] = pbar
            cerebro.optcallback(_opt_progress_callback)

        try:
            runs = cerebro.run(stdstats=False, tradehistory=write_history, maxcpus=maxcpus)
        finally:
            if pbar is not None:
                pbar.close()
                globals()['_opt_pbar'] = None

        for run in runs:                  # все варианты по одному контракту
            for strategy in run:          # уникальные варианты по параметрам
                analysis, tr, ords = _collect_run_results(
                    strategy, data, capital_mode, write_history
                )
                results.append(analysis)
                trades.extend(tr)
                orders.extend(ords)

        elapsed = _time.time() - st_time
        print(
            f'Прогон {len(runs)} вариантов стратегии для контракта '
            f'{data.p.name} за {round(elapsed, 2)} сек., '
            f'{round(elapsed / 60, 2)} мин., '
            f'V (скорость) = {round(len(runs) / elapsed, 2)} вар/сек, '
            f'{str(datetime.now().time())[:5]}'
        )
        gc.collect()

    return results, trades, orders


def _run_cumulative_capital(datas, params, instrument_type, start_cash, exit_mode,
                            analyzer_params, capital_mode):
    """
    Кумулятивный режим: одна комбинация параметров последовательно проходит
    все контракты. Финальная стоимость счёта после контракта N становится
    стартовым капиталом для контракта N+1 той же комбинации параметров.
    """
    results, trades, orders = [], [], []
    write_history = params.get('write_history', False)
    strategy_cls = _strategy_cls(exit_mode)

    param_variants = list(expand_param_combinations(params))
    total_runs = len(param_variants) * len(datas)

    pbar = None
    if tqdm is not None:
        pbar = tqdm(
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

            cerebro = _build_cerebro(data, instrument_type, current_cash, analyzer_params)
            cerebro.addstrategy(strategy_cls, **strategy_params)

            runs = cerebro.run(stdstats=False, tradehistory=write_history)
            strategy = runs[0]

            analysis, tr, ords = _collect_run_results(
                strategy, data, capital_mode, write_history
            )
            results.append(analysis)
            trades.extend(tr)
            orders.extend(ords)

            # Финальная стоимость счёта становится стартом следующего контракта.
            # Позиция закрывается за DATA_END_EXIT_BARS баров до конца data feed,
            # поэтому EndValue не должен включать незакрытый mark-to-market хвост.
            current_cash = analysis.get('EndValue', cerebro.broker.getvalue())

            if pbar is not None:
                pbar.update(1)
                pbar.set_postfix_str(f'{variant_no}/{len(param_variants)} {data.p.name}')

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

    if pbar is not None:
        pbar.close()

    return results, trades, orders


def _write_optimization_excel(results_file, df1, df3, df4, df_trades, df_orders,
                              sheet_size, instrument_type, write_history):
    """Запись итоговых листов оптимизации в xlsx."""
    with pd.ExcelWriter(results_file, engine='xlsxwriter') as writer:
        if not sheet_size:
            if instrument_type == 'futures':
                df1.to_excel(writer, sheet_name='by Contracts', index=False)
            if write_history:
                df_trades.to_excel(writer, sheet_name='trades', index=False)
                if not df_orders.empty:
                    df_orders.to_excel(writer, sheet_name='orders', index=False)
        df3.to_excel(writer, sheet_name='results', index=False)
        df4.to_excel(writer, sheet_name='params', index=False)


def run_optimization_from_settings(settings, maxcpus=None):
    # Фильтр AutoTune https://financial-hacker.com/the-autotune-filter/
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
        results, trades, orders = _run_fixed_capital(
            datas=datas,
            params=params,
            instrument_type=instrument_type,
            start_cash=start_cash,
            exit_mode=exit_mode,
            analyzer_params=analyzer_params,
            capital_mode=capital_mode,
            variants=variants,
            maxcpus=maxcpus,
        )
    else:
        results, trades, orders = _run_cumulative_capital(
            datas=datas,
            params=params,
            instrument_type=instrument_type,
            start_cash=start_cash,
            exit_mode=exit_mode,
            analyzer_params=analyzer_params,
            capital_mode=capital_mode,
        )

    print(f'Весь прогон за {round(_time.time() - total_time, 2)} сек., '
          f'{round((_time.time() - total_time) / 3600, 2)} часов.')

    write_history = params.get('write_history', False)

    df1 = pd.DataFrame(results).round(2)
    df_trades = pd.DataFrame(trades).round(3) if write_history else pd.DataFrame()
    df_orders = pd.DataFrame(orders).round(3) if write_history else pd.DataFrame()
    df3 = aggregate_df(df1, start_cash, sort_by=main_opt_metric)

    results_drop_cols = ['MaxDDLen', 'Asset']
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
        columns=['Parameter', 'Value'],
    )

    for col in ('PNLs', 'Asset'):
        if col in df1.columns:
            del df1[col]

    timestamp = datetime.now().strftime("%d-%m-%y %H-%M")
    script_version = _script_version()
    results_file = f'opt_results_{script_version}_{sec}_{tf}_{timestamp}.xlsx'

    _write_optimization_excel(
        results_file=results_file,
        df1=df1,
        df3=df3,
        df4=df4,
        df_trades=df_trades,
        df_orders=df_orders,
        sheet_size=sheet_size,
        instrument_type=instrument_type,
        write_history=write_history,
    )

    print(f"Результаты успешно сохранены в файл '{results_file}'.")
    try:
        os.startfile(results_file)
    except AttributeError:
        pass