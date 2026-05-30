from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

import backtrader as bt
import pandas as pd

FUTURE_TYPE = dict(     # Базовая ставка комиссии Биржи
    currency=0.00462,   # Валютные контракты
    percent=0.01650,    # Процентные контракты
    stock=0.01980,      # Фондовые контракты
    xindex=0.00660,     # Индексные контракты
    commodity=0.01320   # Товарные контракты
)

class FuturesCommission(bt.CommInfoBase):
    params = dict(moexcomm=0.0, cost_of_price_step=0)  # Базовая ставка комиссии Биржи

    def _getcommission(self, size, price, pseudoexec):
        brokers_pocket = abs(size) * self.p.commission
        moexs_pocket = abs(size) * price * self.p.mult * self.p.moexcomm / 100
        return brokers_pocket + moexs_pocket


# Класс для расчета комиссии при работе с Акциями
class StockCommission(bt.CommInfoBase):
    params = dict(
        # ВАЖНО: комиссии задаются десятичной долей, а не процентом.
        # Например, 0.03% = 0.0003.
        moexcomm=0.0003,
        brokercomm=0.0003,
        cost_of_price_step=0.01,
    )

    def _getcommission(self, size, price, pseudoexec):
        return abs(size) * price * (self.p.moexcomm + self.p.brokercomm)


stocks_comm = dict(
    # Универсальный дефолт для акций MOEX. При необходимости для конкретной
    # акции можно добавить отдельную запись с другим шагом цены или комиссией:
    # SBER=StockCommission(moexcomm=0.0003, brokercomm=0.0003, cost_of_price_step=0.01)
    DEFAULT=StockCommission(moexcomm=0.0003, brokercomm=0.0003, cost_of_price_step=0.01),
)


futures_comm = dict( # Комиссии для фьючерсов
    RTS=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=26358, # 27/04/25, 24700,  # ГО 05.12.2024 08-05-26
                          mult=14.92418/10,  # мультипликатор Стоимость шага цены/Шаг цены 07-05-26 - 15.04492
                          moexcomm=FUTURE_TYPE['xindex'],
                          cost_of_price_step=10),
    RTSM=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=2900,  # 27/04/25, 2470,  # ГО 05.12.2024
                          mult=8.26549/0.5,  # мультипликатор Стоимость шага цены/Шаг цены
                          moexcomm=FUTURE_TYPE['xindex']),
    NASD=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=2607,  # ГО 05.12.2024
                          mult=0.97966/1,  # мультипликатор Стоимость шага цены/Шаг цены
                          moexcomm=FUTURE_TYPE['xindex']),
    CNY=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=1050,  # ГО 27/04/25 27/04/26(!)
                          mult=1/0.001,  # мультипликатор
                          moexcomm=FUTURE_TYPE['currency'],
                          cost_of_price_step=0.001),
    Si=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=11779,  # ГО  05.12.2024 08-05-26
                          mult=1,  # мультипликатор
                          moexcomm=FUTURE_TYPE['currency'],
                          cost_of_price_step=1),
    Eu=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=16000,  # ГО
                          mult=1,  # мультипликатор
                          moexcomm=FUTURE_TYPE['currency']),
    NG=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=6300,  # ГО
                          mult=9.8/0.001,  # мультипликатор
                          moexcomm=FUTURE_TYPE['commodity']),
    GOLD=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=16600,  # ГО
                          mult=9.8/0.1,  # мультипликатор
                          moexcomm=FUTURE_TYPE['commodity']),
    SBRF=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=4878.91 ,  # ГО 11-05-26
                          mult=1,  # мультипликатор
                          moexcomm=FUTURE_TYPE['stock'],
                          cost_of_price_step=1),
    BR=FuturesCommission(commission=2.0,  # 2 руб за контракт
                         margin=10374,  # ГО 30-12-24
                         mult=10.167/ 0.01,  # мультипликатор
                         moexcomm=FUTURE_TYPE['commodity']),
    MIX=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=33000,  # - ГО 26-04-26
                          mult=25 / 25,  # мультипликатор Стоимость шага цены/Шаг цены
                          moexcomm=FUTURE_TYPE['xindex'],
                          cost_of_price_step=25),
    MXI=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=3500,  # - ГО 26-04-25, 3400 - ГО 07.02.2025
                          mult=0.5 / 0.05,  # мультипликатор Стоимость шага цены/Шаг цены
                          moexcomm=FUTURE_TYPE['xindex'],
                          cost_of_price_step=0.05),
    SPYF=FuturesCommission(commission=2.0,  # 2 руб за контракт
                          margin=4252.12 ,  # ГО 11-05-26
                          mult=0.83563 / 0.01,  # мультипликатор Стоимость шага цены/Шаг цены
                          moexcomm=FUTURE_TYPE['xindex'],
                          cost_of_price_step=0.01))

def round_to_nearest_price_step(step, value, isbuy):
    """
    Универсальное округление до ближайшего кратного шага цены.
    Работает с любыми положительными step, в т.ч. < 1.

    :param step: Шаг цены инструмента (>0)
    :param value: Значение, которое нужно округлить
    :param isbuy: True  -> округлить вниз  (покупка, long)
                   False -> округлить вверх (продажа, short)
    :return: Округлённое значение (float)
    """
    if step <= 0:
        raise ValueError('step должен быть > 0')

    # Переводим во "внутреннюю" точку Decimal, чтобы избежать погрешностей float
    step_d  = Decimal(str(step))
    value_d = Decimal(str(value))

    # Сколько шагов содержится в price?
    steps_cnt = value_d / step_d

    # Округляем количество шагов
    rounding_mode = ROUND_FLOOR if isbuy else ROUND_CEILING
    steps_cnt = steps_cnt.to_integral_value(rounding=rounding_mode)

    # Возвращаемся к цене и приводим к float
    return float(steps_cnt * step_d)


def normalize_instrument_type(instrument_type):
    """Приводит тип инструмента к одному из значений: futures / stocks."""
    value = str(instrument_type or 'futures').lower().strip()

    if value in ('future', 'futures', 'f'):
        return 'futures'

    if value in ('stock', 'stocks', 'share', 'shares', 's'):
        return 'stocks'

    raise ValueError("instrument_type должен быть 'futures' или 'stocks'")



def get_commission_info(sec, instrument_type):
    """Возвращает commission-info для фьючерса или акции."""
    instrument_type = normalize_instrument_type(instrument_type)

    if instrument_type == 'futures':
        if sec not in futures_comm:
            raise KeyError(
                f"Для фьючерса '{sec}' нет записи в futures_comm. "
                f"Добавьте ГО, мультипликатор, биржевую комиссию и шаг цены."
            )
        return futures_comm[sec]

    return stocks_comm.get(sec, stocks_comm['DEFAULT'])


def load_moex_datas(store, sec, instrument_type, start_date, end_date, tf):
    """
    Загружает данные MOEX.

    futures:
        sec = базовый код фьючерса, например SPYF / RTS / MIX.
        Скрипт сам находит серии через store.futures.contracts_between().

    stocks:
        sec = тикер одной акции, например 'SBER'.
        Данные загружаются напрямую, без логики контрактов и экспираций.
    """
    instrument_type = normalize_instrument_type(instrument_type)
    datas = []

    if instrument_type == 'futures':
        contracts = store.futures.contracts_between(sec, start_date, end_date)
        print(contracts)

        for contract in contracts:
            prevexpdate = pd.to_datetime(store.futures.prevexpdate(contract))
            expdate = pd.to_datetime(store.futures.expdate(contract))

            if contract == contracts[0]:
                fromdate = pd.to_datetime(start_date) - timedelta(days=5)
            else:
                fromdate = prevexpdate - timedelta(days=5)

            if contract == contracts[-1]:
                todate = pd.to_datetime(end_date) + timedelta(days=1)
            else:
                todate = expdate + timedelta(days=1)

            data = store.getdata(
                sec_id=contract,
                fromdate=fromdate,
                todate=todate,
                tf=tf,
                name=contract,
            )

            data.sec = sec
            datas.append(data)

        return datas

    data = store.getdata(
        sec_id=sec,
        fromdate=start_date,
        todate=end_date,
        tf=tf,
        name=sec,
    )

    data.sec = sec
    datas.append(data)

    return datas


