# AutoTune Filter для Backtrader

Реализация адаптивного фильтра Джона Элерса **AutoTune Filter** (TASC 05/2026) в [Backtrader](https://www.backtrader.com/) и торговая стратегия на его основе. Проект включает сам индикатор, стратегию с bracket-выходом, инфраструктуру для оптимизации параметров на данных MOEX и готовые результаты прогонов по акции SBER и фьючерсу SBRF.

Проект описан в [статье](https://www.backtrader.ru/blog/2026/05/29/autotunefilter/) на [backtarder.ru](https://www.backtarder.ru). Код не предназначен для торговли на реальном счёте без дополнительной проверки.

## Что внутри

| Файл | Назначение |
|---|---|
| `atf_indicator.py` | Индикатор `AutoTuneFilter` (линии `bp`, `filt`, `mincorr`, `dc`) + демо-стратегия для сверки значений с TradingView |
| `atf_strategy.py` | Две стратегии: `AutoTuneFilterStrategy` (bracket-выход) и `AutoTuneFilterEhlersStrategy` (оригинальная always-in-the-market логика Элерса) |
| `moex_setup.py` | Загрузка данных MOEX и расчёт комиссий для акций и фьючерсов |
| `params_tools.py` | Утилиты для подготовки сетки параметров под оптимизацию |
| `reporting.py` | Анализатор `SmartAnalyzer`, сбор метрик и агрегация результатов (PNL, PF, PROM и др.) |
| `runners.py` | Управляющий слой: сборка `Cerebro`, запуск одиночного прогона или оптимизации |
| `run_optimization.py` | Точка входа для оптимизации (перебор сетки параметров) |
| `run_single_plot.py` | Точка входа для одиночного прогона с графиком |
| `opt_results_*.xlsx` | Готовые результаты оптимизации (SBER и SBRF) для проверки цифр из статьи |

## Требования

Проект разрабатывался и тестировался на **Python 3.9**.

Зависимости:
- `backtrader` — движок бэктестинга
- `moex-store` — загрузка котировок MOEX ([PyPI](https://pypi.org/project/moex-store/))
- `pandas`, `numpy` — обработка данных
- `xlsxwriter` — запись Excel-отчётов
- `tqdm` — прогресс-бар оптимизации
- `cerebroview` — построение графика (нужен только для `run_single_plot.py`)

> **Про `cerebroview`:** это отдельный модуль для интерактивного графика. Он нужен только при запуске `run_single_plot.py`. Оптимизация (`run_optimization.py`) работает без него.

> **Про `backtrader` на Python 3.9:** официальный пакет с PyPI на свежих версиях Python может выдавать ошибку импорта (`collections` / `Iterable`). Если столкнётесь — используйте форк `backtrader2` (`pip install backtrader2`) либо более раннюю версию Python.

## Установка

Версия кода к статье — в релизе: **https://github.com/Celeevo/AutoTuneFilter/releases/tag/v1.0**
Архив можно скачать там же (раздел **Assets** → **Source code (zip)**) либо склонировать репозиторий целиком:

```bash
# 1. Склонировать репозиторий
git clone https://github.com/Celeevo/AutoTuneFilter.git
cd AutoTuneFilter

# 2. (рекомендуется) создать виртуальное окружение
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 3. Установить зависимости
pip install -r requirements.txt
```

## Запуск

**Одиночный прогон с графиком** — параметры задаются в словарях `STRATEGY_PARAMS` и `RUN_SETTINGS` внутри файла:

```bash
python run_single_plot.py
```

**Оптимизация** (перебор сетки параметров, результат в XLSX):

```bash
python run_optimization.py
```

Настройки прогона (инструмент, период, режим выхода, диапазоны параметров) меняются прямо в верхней части соответствующего скрипта.

## Лицензия

MIT
