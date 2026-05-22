from itertools import product

def iterable_params(p:dict):
    '''
    Анализируем params (р), передаваемые в cerebro.optstrategy
    и возвращаем имена (ключи params) тех, которые оптимизируются (итерируются)
    Если нет итерируемых параметров (такое бывает) - возвращаем строку 'params'
    '''
    names = [k for k,v in p.items() if isinstance(v, (list, tuple, set, range))]
    return names if names else ['params']

def count_param_variants(params_dict):
    variants = 1
    for value in params_dict.values():
        if isinstance(value, (list, tuple, set, range)):
            variants *= len(value)
    return variants


def expand_param_combinations(params_dict):
    '''
    Разворачивает словарь params в последовательность конкретных наборов параметров.

    Нужно для capital_mode='cumulative': в этом режиме нельзя запускать один общий
    optstrategy на контракт, потому что стартовый капитал следующего контракта
    должен быть своим для каждой комбинации параметров.
    '''
    keys = list(params_dict.keys())
    values = []

    for value in params_dict.values():
        if isinstance(value, (list, tuple, set, range)):
            values.append(list(value))
        else:
            values.append([value])

    for combo in product(*values):
        yield dict(zip(keys, combo))


def to_single_strategy_params(params):
    """
    Для режима single_plot превращает словарь параметров в один конкретный набор.

    В этом режиме стратегия запускается через addstrategy(), поэтому каждый
    параметр должен быть одиночным значением. Если случайно передан список/range,
    берём первый элемент и печатаем предупреждение.
    """
    single_params = {}

    for key, value in params.items():
        if isinstance(value, range):
            value_list = list(value)
            if not value_list:
                raise ValueError(f"Параметр {key} задан пустым range")
            print(f"[single_plot] {key}: получен range, беру первое значение {value_list[0]}")
            single_params[key] = value_list[0]

        elif isinstance(value, (list, tuple, set)):
            value_list = list(value)
            if not value_list:
                raise ValueError(f"Параметр {key} задан пустым списком/кортежем/set")
            print(f"[single_plot] {key}: получен набор значений, беру первое значение {value_list[0]}")
            single_params[key] = value_list[0]

        else:
            single_params[key] = value

    return single_params


