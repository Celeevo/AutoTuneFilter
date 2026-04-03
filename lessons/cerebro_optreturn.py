import backtrader as bt
import pandas as pd


class OptResultDemoStrategy(bt.Strategy):
    params = (
        ('period', 2),
    )

    def next(self):
        # Логика здесь не важна.
        # Нам нужен именно результат optstrategy/run().
        pass


def run_demo(optreturn_value):
    print('=' * 60)
    print(f'Запуск с optreturn={optreturn_value}')
    print('=' * 60)

    df = pd.DataFrame({
        'open':   range(100, 105),
        'high':   range(101, 106),
        'low':    range(99, 104),
        'close':  range(102, 107),
        'volume': range(1000, 1005),
    }, index=pd.date_range('1917-01-01', periods=5, freq='D'))

    data = bt.feeds.PandasData(dataname=df)

    cerebro = bt.Cerebro()
    cerebro.adddata(data)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='dd')

    # Оптимизируем один параметр по трём значениям
    cerebro.optstrategy(OptResultDemoStrategy, period=[2, 3, 4])

    # maxcpus=1 нужен только для учебной ясности,
    # чтобы порядок вывода был предсказуемым
    results = cerebro.run(maxcpus=1, optreturn=optreturn_value)

    print(f'Тип results: {type(results)}')
    print(f'Длина results: {len(results)}')
    print()

    for i, run_result in enumerate(results):
        print(f'Элемент верхнего списка №{i}')
        print(f'  Тип run_result: {type(run_result)}')
        print(f'  Длина run_result: {len(run_result)}')

        obj = run_result[0]

        print(f'  Тип объекта внутри: {type(obj)}')
        print(f'  period = {obj.p.period}')
        print(f'  Есть ли analyzers: {hasattr(obj, "analyzers")}')
        print(f'  Есть ли analyzer dd: {hasattr(obj.analyzers, "dd")}')
        print(f'  Есть ли datas: {hasattr(obj, "datas")}')
        print(f'  Есть ли broker: {hasattr(obj, "broker")}')
        print('-' * 80)

    print('Читаем DrawDown из результата:')
    for i, run_result in enumerate(results):
        obj = run_result[0]
        dd = obj.analyzers.dd.get_analysis()
        print(f'  Вариант {i}: period={obj.p.period}, drawdown={dd["drawdown"]}')


if __name__ == '__main__':
    run_demo(True)
    run_demo(False)