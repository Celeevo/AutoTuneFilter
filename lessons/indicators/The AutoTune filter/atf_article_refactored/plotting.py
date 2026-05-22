"""
Дополнительная визуализация результатов оптимизации.

Файл намеренно вынесен отдельно от основного скрипта, чтобы не раздувать
торговую/оптимизационную логику кодом построения графиков.
"""

from __future__ import annotations

import os
import re
from typing import Optional

import matplotlib

# Нужен только для сохранения PNG-файла, без открытия интерактивного окна.
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd


def make_safe_filename(text_value: object, max_len: int = 150) -> str:
    """Делает строку безопасной для использования в имени файла."""
    text_value = str(text_value)
    text_value = re.sub(r'[\\/:*?"<>|]+', "_", text_value)
    text_value = re.sub(r"\s+", "_", text_value).strip("_")
    return text_value[:max_len] if text_value else "plot"


def build_closed_equity_series(
    trades_df: pd.DataFrame,
    start_cash: float,
    freq: Optional[str] = "M",
) -> pd.Series:
    """
    Строит временной ряд closed equity по закрытым сделкам.

    Источник данных:
    - первая точка = start_cash;
    - дальше cash_after после каждой закрытой сделки.

    Параметр freq задаёт агрегацию для графика:
    - "M" — месячные столбцы;
    - None — каждая закрытая сделка отдельной точкой.
    """
    if trades_df is None or trades_df.empty:
        return pd.Series(dtype=float)

    required_cols = {"exit_executed_date", "exit_executed_time", "cash_after"}
    if not required_cols.issubset(trades_df.columns):
        return pd.Series(dtype=float)

    df = trades_df.copy()

    dt_str = (
        df["exit_executed_date"].astype(str)
        + " "
        + df["exit_executed_time"].astype(str)
    )

    df["dt"] = pd.to_datetime(
        dt_str,
        format="%d.%m.%y %H:%M",
        errors="coerce",
    )

    df["cash_after"] = pd.to_numeric(df["cash_after"], errors="coerce")
    df = df.dropna(subset=["dt", "cash_after"]).sort_values("dt")

    if df.empty:
        return pd.Series(dtype=float)

    start_dt = df["dt"].iloc[0] - pd.Timedelta(seconds=1)

    base_row = pd.DataFrame({
        "dt": [start_dt],
        "cash_after": [float(start_cash)],
    })

    equity_points = pd.concat(
        [base_row, df[["dt", "cash_after"]]],
        ignore_index=True,
    ).sort_values("dt")

    equity_series = equity_points.set_index("dt")["cash_after"].astype(float)

    if freq:
        equity_series = equity_series.resample(freq).last().dropna()

    return equity_series


def save_equity_dd_plot(
    trades_df: pd.DataFrame,
    start_cash: float,
    output_path: str,
    title: Optional[str] = None,
    freq: Optional[str] = "M",
) -> Optional[str]:
    """
    Сохраняет PNG-график Equity / DD по closed equity.

    Equity — голубовато-серые столбцы.
    DD — красные отрицательные столбцы.

    Важно: график строится по закрытым сделкам, т.е. по cash_after.
    Он соответствует логике MaxClosedDD*, а не broker value DD,
    который учитывает плавающую просадку открытой позиции.
    """
    equity = build_closed_equity_series(
        trades_df=trades_df,
        start_cash=start_cash,
        freq=freq,
    )

    if equity.empty:
        return None

    running_max = equity.cummax()
    dd = equity - running_max

    fig, ax = plt.subplots(figsize=(12, 6))

    # Цвета заданы явно, потому что пользователь попросил конкретное оформление:
    # Equity — голубо-серый, DD — красный.
    ax.bar(
        equity.index,
        equity.values,
        width=20,
        color="#b9c4df",
        edgecolor="#b9c4df",
        label="Equity",
    )

    ax.bar(
        dd.index,
        dd.values,
        width=20,
        color="#ff4d4d",
        edgecolor="#ff4d4d",
        label="DD",
    )

    ax.axhline(0, color="gray", linewidth=1)
    ax.grid(True, axis="y", alpha=0.35)
    ax.set_ylabel("Money")

    if title:
        ax.set_title(title)

    ax.legend(loc="upper left", frameon=False)
    fig.autofmt_xdate()
    plt.tight_layout()

    output_path = str(output_path)
    plt.savefig(output_path, dpi=150)
    plt.close(fig)

    return output_path


def save_best_equity_dd_plot(
    trades_df: pd.DataFrame,
    results_df: pd.DataFrame,
    results_file: str,
    start_cash: float,
    freq: Optional[str] = "M",
) -> Optional[str]:
    """
    Сохраняет Equity/DD PNG для лучшего варианта из results_df.

    Основной сценарий:
    - первая колонка results_df содержит строку параметров;
    - trades_df содержит колонку params с тем же значением.

    Для stock-режима добавлены защитные fallback-сценарии:
    - сравнение params как строк с trim();
    - если params не совпал, но в results есть Asset, пробуем отобрать trades по sec == Asset;
    - если в trades всего один уникальный params, строим график по нему.
    """
    if trades_df is None or trades_df.empty:
        return None

    if results_df is None or results_df.empty:
        return None

    if "params" not in trades_df.columns:
        return None

    first_col = results_df.columns[0]
    best_param_value = results_df.iloc[0][first_col]
    best_param_str = str(best_param_value).strip()

    trades_work = trades_df.copy()
    trades_work["_params_norm"] = trades_work["params"].astype(str).str.strip()

    trades_subset = trades_work[trades_work["_params_norm"] == best_param_str].copy()
    title_value = best_param_value

    # Fallback для stock-режима: если params не совпал, пробуем Asset -> sec.
    if trades_subset.empty and "Asset" in results_df.columns and "sec" in trades_work.columns:
        best_asset = results_df.iloc[0].get("Asset")
        if pd.notna(best_asset):
            best_asset_str = str(best_asset).strip()
            trades_subset = trades_work[
                trades_work["sec"].astype(str).str.strip() == best_asset_str
            ].copy()
            title_value = best_asset

    # Fallback: если в trades только один params, строим график по нему.
    if trades_subset.empty:
        unique_params = trades_work["_params_norm"].dropna().unique()
        if len(unique_params) == 1:
            trades_subset = trades_work.copy()
            title_value = unique_params[0]

    if "_params_norm" in trades_subset.columns:
        trades_subset = trades_subset.drop(columns=["_params_norm"])

    if trades_subset.empty:
        return None

    plot_stem = os.path.splitext(str(results_file))[0]
    plot_path = f"{plot_stem}_equity_dd_best.png"

    title = f"Equity / DD | {title_value}"

    return save_equity_dd_plot(
        trades_df=trades_subset,
        start_cash=start_cash,
        output_path=plot_path,
        title=title,
        freq=freq,
    )
