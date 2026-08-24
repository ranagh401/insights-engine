"""
Feature Definitions Module
============================
Feature metadata (label, description, format, default params) is now stored in
the database (Insights.ConfigFeatures) and managed via the FastAPI endpoints.

Manage Features:
  GET    /api/features
  POST   /api/features
  PUT    /api/features/{feature_name}
  DELETE /api/features/{feature_name}

This file retains the actual Python calculator functions and the
FEATURE_FUNCTION_REGISTRY that maps function-name strings (stored in DB)
to the real callables the engine needs at runtime.

To add a new feature calculator:
1. Implement a function with signature:
       f(df, kpi_column, sort_columns, **kwargs) -> pd.Series
2. Add it to FEATURE_FUNCTION_REGISTRY below.
3. Create the feature metadata via POST /api/features with
   function_name = '<your_function_name>'.
"""

import pandas as pd
import numpy as np
from typing import Callable, Dict, Any, Optional, List, Tuple, Union

from ..engine.metric_display import (
    kpi_storage_to_percent_point_scale,
    kpi_uses_percentage_point_difference,
)


def _resolve_year_col_for_strict_growth(df: pd.DataFrame, kwargs: dict) -> Optional[str]:
    c = kwargs.get("calendar_year_column")
    if c and c in df.columns:
        return c
    if "OrderYear" in df.columns:
        return "OrderYear"
    if "year" in df.columns:
        return "year"
    return None


def _resolve_month_cols_for_strict_growth(df: pd.DataFrame, kwargs: dict) -> Tuple[Optional[str], Optional[str]]:
    """(year_col, month_col) for calendar MoM / multi-month comparisons."""
    yc = _resolve_year_col_for_strict_growth(df, kwargs)
    mc = kwargs.get("calendar_month_column")
    if mc and mc in df.columns and yc:
        return yc, mc
    if "OrderYear" in df.columns and "OrderMonth" in df.columns:
        return "OrderYear", "OrderMonth"
    if yc and "month" in df.columns:
        return yc, "month"
    return None, None


def _resolve_quarter_cols_for_strict_growth(df: pd.DataFrame, kwargs: dict) -> Tuple[Optional[str], Optional[str]]:
    """(year_col, quarter_col) for calendar QoQ."""
    yc = kwargs.get("calendar_year_column")
    if yc and yc not in df.columns:
        yc = None
    if not yc:
        yc = _resolve_year_col_for_strict_growth(df, kwargs)
    qc = kwargs.get("calendar_quarter_column")
    if qc and qc in df.columns and yc:
        return yc, qc
    for c in ("quarter", "Quarter", "OrderQuarter"):
        if c in df.columns and yc:
            return yc, c
    return None, None


def _parse_year_int(v: Any) -> int:
    return int(float(v))


def _parse_month_int(v: Any) -> int:
    m = int(float(v))
    if not 1 <= m <= 12:
        raise ValueError("month out of range")
    return m


def _parse_quarter_int(v: Any) -> int:
    if isinstance(v, str):
        s = v.strip().upper()
        if s.startswith("Q") and len(s) > 1:
            return int(s[1:])
    q = int(float(v))
    if not 1 <= q <= 4:
        raise ValueError("quarter out of range")
    return q


def _subtract_n_calendar_months(y: int, m: int, n: int) -> Tuple[int, int]:
    """``n`` calendar months before ``(y, m)``. ``n >= 1``."""
    idx = y * 12 + (m - 1) - n
    if idx < 0:
        return -1, -1
    py = idx // 12
    pm = idx % 12 + 1
    return py, pm


def _subtract_n_calendar_quarters(y: int, q: int, n: int) -> Tuple[int, int]:
    """``n`` calendar quarters before ``(y, q)``. ``n >= 1``."""
    idx = y * 4 + (q - 1) - n
    if idx < 0:
        return -1, -1
    py = idx // 4
    pq = idx % 4 + 1
    return py, pq


def _growth_use_percentage_points(kwargs: dict) -> bool:
    """True for rate KPIs where growth is **percentage-point** change, not relative %."""
    kn = str(kwargs.get("kpi_name") or "").strip()
    return bool(kn) and kpi_uses_percentage_point_difference(kn)


def _series_to_percent_point_scale(s: pd.Series) -> pd.Series:
    """Vectorized ``kpi_storage_to_percent_point_scale`` for a KPI column."""
    v = pd.to_numeric(s, errors="coerce")
    out = v.copy()
    m = v.notna() & (v.abs() <= 1.0000001)
    out.loc[m] = out.loc[m] * 100.0
    return out


def _pct_change_or_pp_points(
    df_sorted: pd.DataFrame,
    kpi_column: str,
    gb: Optional[List[str]],
    periods: int,
    use_pp: bool,
) -> pd.Series:
    """Period-over-period growth: relative ``pct_change * 100`` or PP delta for rate KPIs."""

    def _pp_delta(s: pd.Series) -> pd.Series:
        scaled = _series_to_percent_point_scale(s)
        return scaled - scaled.shift(periods)

    if use_pp:
        if gb:
            return df_sorted.groupby(gb, sort=False)[kpi_column].transform(_pp_delta)
        return _pp_delta(df_sorted[kpi_column])
    if gb:
        return df_sorted.groupby(gb, sort=False)[kpi_column].transform(
            lambda s: s.pct_change(periods=periods) * 100
        )
    return df_sorted[kpi_column].pct_change(periods=periods) * 100


def _strict_calendar_period_compare(
    df: pd.DataFrame,
    kpi_column: str,
    sort_columns: list,
    kwargs: dict,
    grain: str,
    as_pct: bool,
    *,
    as_percentage_points: bool = False,
) -> pd.Series:
    """
    Compare each row to the **calendar** prior period (not the previous sorted row).

    * year: prior period is ``year - periods`` (typically ``periods=1`` → YoY).
    * month: prior is ``n`` calendar months back (``periods``).
    * quarter: prior is ``n`` calendar quarters back (``periods``).
    """
    periods = int(kwargs.get("periods", 1))
    if periods < 1:
        periods = 1

    gb = _groupby_cols(df, kwargs)
    df_sorted = _sorted_frame(df, sort_columns)
    out = pd.Series(np.nan, index=df_sorted.index, dtype=float)

    def _one_group(sub: pd.DataFrame) -> None:
        mp: Dict[Union[int, Tuple[int, int]], Any] = {}
        if grain == "year":
            ycol = _resolve_year_col_for_strict_growth(sub, kwargs)
            if not ycol or ycol not in sub.columns:
                return
            for _, r in sub.iterrows():
                try:
                    y = _parse_year_int(r[ycol])
                except (TypeError, ValueError):
                    continue
                mp[y] = r[kpi_column]
            for idx, row in sub.iterrows():
                try:
                    y = _parse_year_int(row[ycol])
                except (TypeError, ValueError):
                    continue
                py = y - periods
                if py not in mp:
                    continue
                prev, cur = mp[py], row[kpi_column]
                if pd.isna(prev) or pd.isna(cur):
                    continue
                fv_prev, fv_cur = float(prev), float(cur)
                if as_percentage_points:
                    out.loc[idx] = kpi_storage_to_percent_point_scale(
                        fv_cur
                    ) - kpi_storage_to_percent_point_scale(fv_prev)
                elif as_pct:
                    if fv_prev == 0:
                        continue
                    out.loc[idx] = (fv_cur - fv_prev) / fv_prev * 100
                else:
                    out.loc[idx] = fv_cur - fv_prev

        elif grain == "month":
            yc, mc = _resolve_month_cols_for_strict_growth(sub, kwargs)
            if not yc or not mc or yc not in sub.columns or mc not in sub.columns:
                return
            for _, r in sub.iterrows():
                try:
                    y = _parse_year_int(r[yc])
                    m = _parse_month_int(r[mc])
                except (TypeError, ValueError):
                    continue
                mp[(y, m)] = r[kpi_column]
            for idx, row in sub.iterrows():
                try:
                    y = _parse_year_int(row[yc])
                    m = _parse_month_int(row[mc])
                except (TypeError, ValueError):
                    continue
                py, pm = _subtract_n_calendar_months(y, m, periods)
                if py < 0 or (py, pm) not in mp:
                    continue
                prev, cur = mp[(py, pm)], row[kpi_column]
                if pd.isna(prev) or pd.isna(cur):
                    continue
                fv_prev, fv_cur = float(prev), float(cur)
                if as_percentage_points:
                    out.loc[idx] = kpi_storage_to_percent_point_scale(
                        fv_cur
                    ) - kpi_storage_to_percent_point_scale(fv_prev)
                elif as_pct:
                    if fv_prev == 0:
                        continue
                    out.loc[idx] = (fv_cur - fv_prev) / fv_prev * 100
                else:
                    out.loc[idx] = fv_cur - fv_prev

        elif grain == "quarter":
            yc, qc = _resolve_quarter_cols_for_strict_growth(sub, kwargs)
            if not yc or not qc or yc not in sub.columns or qc not in sub.columns:
                return
            for _, r in sub.iterrows():
                try:
                    y = _parse_year_int(r[yc])
                    q = _parse_quarter_int(r[qc])
                except (TypeError, ValueError):
                    continue
                mp[(y, q)] = r[kpi_column]
            for idx, row in sub.iterrows():
                try:
                    y = _parse_year_int(row[yc])
                    q = _parse_quarter_int(row[qc])
                except (TypeError, ValueError):
                    continue
                py, pq = _subtract_n_calendar_quarters(y, q, periods)
                if py < 0 or (py, pq) not in mp:
                    continue
                prev, cur = mp[(py, pq)], row[kpi_column]
                if pd.isna(prev) or pd.isna(cur):
                    continue
                fv_prev, fv_cur = float(prev), float(cur)
                if as_percentage_points:
                    out.loc[idx] = kpi_storage_to_percent_point_scale(
                        fv_cur
                    ) - kpi_storage_to_percent_point_scale(fv_prev)
                elif as_pct:
                    if fv_prev == 0:
                        continue
                    out.loc[idx] = (fv_cur - fv_prev) / fv_prev * 100
                else:
                    out.loc[idx] = fv_cur - fv_prev

    if gb:
        for _, sub in df_sorted.groupby(gb, sort=False):
            _one_group(sub)
    else:
        _one_group(df_sorted)

    return _sanitize_ts(out).reindex(df.index)


def _strict_prior_calendar_year_growth_pct(
    df: pd.DataFrame,
    kpi_column: str,
    sort_columns: list,
    year_col: str,
    kwargs: dict,
) -> pd.Series:
    """YoY using calendar years — relative % or PP for configured rate KPIs."""
    kw = dict(kwargs)
    if year_col:
        kw["calendar_year_column"] = year_col
    use_pp = _growth_use_percentage_points(kw)
    return _strict_calendar_period_compare(
        df,
        kpi_column,
        sort_columns,
        kw,
        grain="year",
        as_pct=True,
        as_percentage_points=use_pp,
    )


def _any_strict_calendar_growth(kwargs: dict) -> bool:
    return bool(
        kwargs.get("strict_calendar_year_growth")
        or kwargs.get("strict_calendar_month_growth")
        or kwargs.get("strict_calendar_quarter_growth")
    )


def _groupby_cols(df: pd.DataFrame, kwargs: dict) -> Optional[List[str]]:
    """Entity keys for per-series time ops (customer, region, …). Omit for global series."""
    raw = kwargs.get("groupby_columns") or []
    if not raw:
        return None
    out = [c for c in raw if c in df.columns]
    return out if out else None


def _sorted_frame(df: pd.DataFrame, sort_columns: list) -> pd.DataFrame:
    if sort_columns:
        return df.sort_values(by=sort_columns).copy()
    return df.copy()


def _sanitize_ts(s: pd.Series) -> pd.Series:
    """Replace ±inf so signal engine skips rows (treats as no valid feature)."""
    return s.replace([np.inf, -np.inf], np.nan)


def _sanitize_kpi_series(s: pd.Series) -> pd.Series:
    """KPI input cleanup before rank / ratios."""
    return s.replace([np.inf, -np.inf], np.nan)


def _rank_group_cols(df: pd.DataFrame, kwargs: dict) -> Optional[List[str]]:
    """When set (time bucket columns), rank / rank_pct are within each period only."""
    raw = kwargs.get("rank_groupby_columns") or []
    out = [c for c in raw if c in df.columns]
    return out if out else None


def _rank_pct_series(s: pd.Series, ascending: bool) -> pd.Series:
    """Percentile rank only when ≥2 finite KPI values in the bucket (else NaN → no signal)."""
    cl = _sanitize_kpi_series(s)
    if cl.notna().sum() < 2:
        return pd.Series(np.nan, index=s.index)
    return cl.rank(ascending=ascending, method="min", pct=True) * 100


def _rank_abs_series(s: pd.Series, ascending: bool) -> pd.Series:
    """Absolute rank; single-row bucket → NaN (ill-defined vs peers)."""
    cl = _sanitize_kpi_series(s)
    if cl.notna().sum() < 2:
        return pd.Series(np.nan, index=s.index)
    return cl.rank(ascending=ascending, method="min")


# Feature Calculator Functions
def calculate_growth_pct(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate period-over-period growth percentage.

    Growth is NaN when there is no prior period in the series (e.g. first year for a customer).
    ±inf from divide-by-zero is sanitized to NaN so no signal fires without a valid baseline.

    When ``strict_calendar_year_growth`` / ``strict_calendar_month_growth`` /
    ``strict_calendar_quarter_growth`` is set, growth compares to the **calendar** prior
    period (y−n years, m−n months, q−n quarters), not the previous sorted row — so missing
    buckets do not compare to the wrong period.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column to calculate growth for
        sort_columns: Columns to sort by (e.g., ['OrderDate'])
        **kwargs: Additional parameters (periods: number of periods to shift, default=1)

    Returns:
        Series with growth percentage values aligned to df.index
    """
    if kwargs.get("strict_calendar_year_growth"):
        yc = _resolve_year_col_for_strict_growth(df, kwargs)
        if yc:
            return _strict_prior_calendar_year_growth_pct(
                df, kpi_column, sort_columns, yc, kwargs
            )
    if kwargs.get("strict_calendar_quarter_growth"):
        ycq, _qcol = _resolve_quarter_cols_for_strict_growth(df, kwargs)
        if ycq and _qcol:
            use_pp = _growth_use_percentage_points(kwargs)
            return _strict_calendar_period_compare(
                df,
                kpi_column,
                sort_columns,
                kwargs,
                grain="quarter",
                as_pct=True,
                as_percentage_points=use_pp,
            )
    if kwargs.get("strict_calendar_month_growth"):
        ycm, _mcol = _resolve_month_cols_for_strict_growth(df, kwargs)
        if ycm and _mcol:
            use_pp = _growth_use_percentage_points(kwargs)
            return _strict_calendar_period_compare(
                df,
                kpi_column,
                sort_columns,
                kwargs,
                grain="month",
                as_pct=True,
                as_percentage_points=use_pp,
            )
    periods = int(kwargs.get("periods", 1))
    gb = _groupby_cols(df, kwargs)
    df_sorted = _sorted_frame(df, sort_columns)
    use_pp = _growth_use_percentage_points(kwargs)
    growth = _pct_change_or_pp_points(df_sorted, kpi_column, gb, periods, use_pp)
    out = _sanitize_ts(growth).reindex(df.index)
    return out


def calculate_yoy_growth_pct(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate year-over-year growth percentage.

    Thin wrapper around calculate_growth_pct with periods=12 hardcoded.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column to calculate growth for
        sort_columns: Columns to sort by (e.g., ['OrderDate'])
        **kwargs: Additional parameters
    Returns:
        Series with growth percentage values aligned to df.index
    """
    kw = {k: v for k, v in kwargs.items() if k != "periods"}
    periods = int(kwargs.get("periods", 12))
    return calculate_growth_pct(df, kpi_column, sort_columns, periods=periods, **kw)

def calculate_qoq_growth_pct(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate quarter-over-quarter growth percentage.

    Thin wrapper around calculate_growth_pct with periods=3 hardcoded.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column to calculate growth for
        sort_columns: Columns to sort by (e.g., ['OrderDate'])
        **kwargs: Additional parameters
    Returns:
        Series with growth percentage values aligned to df.index
    """
    kw = {k: v for k, v in kwargs.items() if k != "periods"}
    periods = int(kwargs.get("periods", 3))
    return calculate_growth_pct(df, kpi_column, sort_columns, periods=periods, **kw)


def calculate_wow_growth_pct(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate week-over-week growth percentage.

    Thin wrapper around calculate_growth_pct with periods=1 hardcoded.
    Assumes the incoming DataFrame is already at weekly grain, for example
    ``brand, week_of_month, gross_revenue``.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column to calculate growth for
        sort_columns: Columns to sort by
        **kwargs: Additional parameters

    Returns:
        Series with growth percentage values aligned to df.index
    """
    kw = {k: v for k, v in kwargs.items() if k != "periods"}
    periods = int(kwargs.get("periods", 1))
    return calculate_growth_pct(df, kpi_column, sort_columns, periods=periods, **kw)


def calculate_self_kpi_value(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Portfolio-level raw KPI value (no dimension drill, no period comparison).

    Used with signal jobs whose ``dimension_name`` is ``SELF``. The engine fetches
    one KPI total for the job date window; signal thresholds (e.g. ``gt`` 500)
    are evaluated against this value in ``config_signalsclientportal``.
    """
    return pd.to_numeric(df[kpi_column], errors="coerce")


def calculate_mom_growth_pct(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate month-over-month growth percentage.

    Thin wrapper around calculate_growth_pct with periods=1 hardcoded.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column to calculate growth for
        sort_columns: Columns to sort by
        **kwargs: Additional parameters

    Returns:
        Series with growth percentage values aligned to df.index
    """
    kw = {k: v for k, v in kwargs.items() if k != "periods"}
    periods = int(kwargs.get("periods", 1))
    return calculate_growth_pct(df, kpi_column, sort_columns, periods=periods, **kw)



def calculate_rolling_avg(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate rolling average for a KPI column using a calendar-month window.
    Groups data by sort_columns instead of group_columns.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns used for grouping and sorting (date column should be included)
        **kwargs:
            window_months: Rolling window length in months (default=3)
            date_col: Date column name (default='Date')
            window_days/window: Legacy aliases kept for backward compatibility

    Returns:
        Series with rolling average values aligned to df.index
    """
    # Determine rolling window in months
    window_months = kwargs.get('window_months')
    if window_months is None:
        legacy_days = kwargs.get('window_days', kwargs.get('window', None))
        if legacy_days is None:
            window_months = 3
        else:
            window_months = max(1, int(round(float(legacy_days) / 30.0)))

    date_col = kwargs.get('date_col', 'Date')

    # Fallback: the time-series DataFrame produced by _fetch_time_series uses
    # '_period_start' (already a pd.Timestamp) rather than a raw 'Date' column.
    if date_col not in df.columns:
        if '_period_start' in df.columns:
            date_col = '_period_start'
        else:
            raise KeyError(
                f"Date column '{date_col}' not found in DataFrame. "
                f"Available columns: {list(df.columns)}"
            )

    df_copy = df.copy()

    # Parse dates only when the column is not already datetime.
    if not pd.api.types.is_datetime64_any_dtype(df_copy[date_col]):
        df_copy[date_col] = pd.to_datetime(df_copy[date_col], format="%d-%m-%Y", errors="coerce")
        if df_copy[date_col].isna().any():
            df_copy[date_col] = pd.to_datetime(df_copy[date_col], errors="coerce")
        if df_copy[date_col].isna().any():
            raise ValueError(f"Could not parse all values in date column '{date_col}'")

    # Sort by sort_columns
    if sort_columns:
        df_copy = df_copy.sort_values(sort_columns)
    else:
        df_copy = df_copy.sort_values(date_col)

    # Function to calculate rolling average per group
    def calc_group(g: pd.DataFrame) -> pd.Series:
        dates = g[date_col]
        values = g[kpi_column]
        rolling_vals = [
            values[(dates >= d - pd.DateOffset(months=window_months)) & (dates <= d)].mean()
            for d in dates
        ]
        return pd.Series(rolling_vals, index=g.index)

    # Use sort_columns as grouping columns if provided
    if sort_columns:
        result = df_copy.groupby(sort_columns, group_keys=False).apply(calc_group)
    else:
        result = calc_group(df_copy)

    return result.reindex(df.index)


def calculate_rolling_avg_6m(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate rolling average over ~6 months.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by
        **kwargs: Additional parameters (supports date_col/group_columns)

    Returns:
        Series with rolling average values aligned to df.index
    """
    window = kwargs.get('window', 3)
    gb = _groupby_cols(df, kwargs)
    df_sorted = _sorted_frame(df, sort_columns)
    if gb:
        rolling_avg = df_sorted.groupby(gb, sort=False)[kpi_column].transform(
            lambda s: s.rolling(window=window, min_periods=1).mean()
        )
    else:
        rolling_avg = df_sorted[kpi_column].rolling(window=window, min_periods=1).mean()
    return _sanitize_ts(rolling_avg).reindex(df.index)


def calculate_rolling_avg_12m(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate rolling average over ~12 months.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by
        **kwargs: Additional parameters (supports date_col/group_columns)

    Returns:
        Series with rolling average values aligned to df.index
    """
    params = kwargs.copy()
    params['window_days'] = kwargs.get('window_days', 365)
    return calculate_rolling_avg(df, kpi_column, sort_columns, **params)


def calculate_std_dev(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate rolling standard deviation.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by
        **kwargs: Additional parameters (window: number of periods, default=3)

    Returns:
        Series with standard deviation values aligned to df.index
    """
    window = kwargs.get('window', 3)
    gb = _groupby_cols(df, kwargs)
    df_sorted = _sorted_frame(df, sort_columns)
    if gb:
        std_dev = df_sorted.groupby(gb, sort=False)[kpi_column].transform(
            lambda s: s.rolling(window=window, min_periods=2).std()
        )
    else:
        std_dev = df_sorted[kpi_column].rolling(window=window, min_periods=2).std()
    return _sanitize_ts(std_dev).reindex(df.index)


def calculate_benchmark_variance(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate variance from a benchmark value.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by (unused for this feature but kept for API consistency)
        **kwargs:
            benchmark     : benchmark value (required)
            as_percentage : return as percentage (default=True)

    Returns:
        Series with variance from benchmark aligned to df.index
    """
    benchmark = kwargs.get('benchmark')
    if benchmark is None:
        # No benchmark configured — return NaN so other features can still run
        return pd.Series(np.nan, index=df.index)
    as_percentage = kwargs.get('as_percentage', True)
    if as_percentage:
        try:
            b = float(benchmark)
        except (TypeError, ValueError):
            return pd.Series(np.nan, index=df.index)
        if b == 0 or pd.isna(b):
            return pd.Series(np.nan, index=df.index)
        variance = ((df[kpi_column] - benchmark) / benchmark) * 100
    else:
        variance = df[kpi_column] - benchmark
    return _sanitize_ts(variance)


def calculate_cumulative_sum(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate cumulative sum over periods.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by

    Returns:
        Series with cumulative sum values aligned to df.index
    """
    gb = _groupby_cols(df, kwargs)
    df_sorted = _sorted_frame(df, sort_columns)
    if gb:
        cumsum = df_sorted.groupby(gb, sort=False)[kpi_column].transform(lambda s: s.cumsum())
    else:
        cumsum = df_sorted[kpi_column].cumsum()
    return _sanitize_ts(cumsum).reindex(df.index)


def calculate_rank(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate rank based on KPI value.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by (unused for this feature)
        **kwargs: Additional parameters (ascending: rank order, default=False)

    Returns:
        Series with rank values aligned to df.index
    """
    ascending = kwargs.get('ascending', False)
    rg = _rank_group_cols(df, kwargs)
    if rg:
        return df.groupby(rg, sort=False)[kpi_column].transform(
            lambda s: _rank_abs_series(s, ascending)
        )
    v = _sanitize_kpi_series(df[kpi_column])
    if v.notna().sum() < 2:
        return pd.Series(np.nan, index=df.index)
    return v.rank(ascending=ascending, method="min")


def calculate_rank_pct(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate percentile rank (0-100) based on KPI value.

    A value of 100 means worst performer (lowest KPI value),
    a value of 0 means best performer (highest KPI value).
    This scales with dimension cardinality, unlike absolute rank.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by (unused for this feature)
        **kwargs: Additional parameters (ascending: rank order, default=False)

    Returns:
        Series with percentile rank values (0-100) aligned to df.index
    """
    ascending = kwargs.get('ascending', False)
    rg = _rank_group_cols(df, kwargs)
    if rg:
        return df.groupby(rg, sort=False)[kpi_column].transform(
            lambda s: _rank_pct_series(s, ascending)
        )
    v = _sanitize_kpi_series(df[kpi_column])
    return _rank_pct_series(v, ascending)


# ═══════════════════════════════════════════════════════════════════════════════
# RGM Feature Calculator Functions
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_z_score(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate Z-score: (value - mean) / std_dev.

    Measures how many standard deviations a KPI value is from the group mean.
    Useful for anomaly detection across dimensions (e.g. outlier SKUs).

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by
        **kwargs: Additional parameters
            window: rolling window size (default=None → uses full-series mean/std)

    Returns:
        Series with Z-score values aligned to df.index
    """
    window = kwargs.get('window', None)
    values = df[kpi_column]

    if window:
        gb = _groupby_cols(df, kwargs)
        df_sorted = _sorted_frame(df, sort_columns) if sort_columns else df.copy()
        if gb:
            rolling_mean = df_sorted.groupby(gb, sort=False)[kpi_column].transform(
                lambda s: s.rolling(window=window, min_periods=2).mean()
            )
            rolling_std = df_sorted.groupby(gb, sort=False)[kpi_column].transform(
                lambda s: s.rolling(window=window, min_periods=2).std()
            )
        else:
            rolling_mean = df_sorted[kpi_column].rolling(window=window, min_periods=2).mean()
            rolling_std = df_sorted[kpi_column].rolling(window=window, min_periods=2).std()
        z_scores = (df_sorted[kpi_column] - rolling_mean) / rolling_std.replace(0, np.nan)
        return _sanitize_ts(z_scores).reindex(df.index)
    else:
        mean = values.mean()
        std = values.std()
        if std == 0 or pd.isna(std):
            return pd.Series(np.nan, index=df.index)
        z = (values - mean) / std
        return _sanitize_ts(z)


def calculate_seasonality_index(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Seasonality Index — passthrough feature.

    Reads the pre-computed 'seasonality_index' column from the DataFrame
    if it exists (populated by ML pipeline in FactTradePromotions).
    Falls back to computing a simple ratio-to-moving-average if the column
    is not present.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by
        **kwargs:
            source_column: column name to read (default='seasonality_index')
            window: fallback moving-average window (default=12)

    Returns:
        Series with seasonality index values aligned to df.index
    """
    source_col = kwargs.get('source_column', 'seasonality_index')

    # Passthrough: use pre-computed column if available
    if source_col in df.columns:
        return _sanitize_ts(pd.to_numeric(df[source_col], errors="coerce"))

    # Fallback: ratio-to-moving-average
    window = kwargs.get('window', 12)
    gb = _groupby_cols(df, kwargs)
    df_sorted = _sorted_frame(df, sort_columns) if sort_columns else df.copy()
    if gb:
        moving_avg = df_sorted.groupby(gb, sort=False)[kpi_column].transform(
            lambda s: s.rolling(window=window, min_periods=1).mean()
        )
    else:
        moving_avg = df_sorted[kpi_column].rolling(window=window, min_periods=1).mean()
    seasonal_idx = df_sorted[kpi_column] / moving_avg.replace(0, np.nan)
    return _sanitize_ts(seasonal_idx).reindex(df.index)


def calculate_sku_mix(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    SKU Mix — proportion of each row's KPI value within the total.

    Computes (row_value / total_value) × 100 to show the share (%)
    of each dimension member (typically SKU) in the overall KPI.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by (unused)
        **kwargs: Additional parameters

    Returns:
        Series with mix percentage values (0-100) aligned to df.index
    """
    total = df[kpi_column].sum()
    if total == 0 or pd.isna(total):
        return pd.Series(np.nan, index=df.index)
    return _sanitize_ts((df[kpi_column] / total) * 100)


def calculate_channel_mix(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Channel Mix — proportion of each channel's KPI value within the total.

    Identical math to SKU mix but semantically represents channel share.
    Produces (row_value / total_value) × 100.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by (unused)
        **kwargs: Additional parameters

    Returns:
        Series with channel mix percentage values (0-100) aligned to df.index
    """
    total = df[kpi_column].sum()
    if total == 0 or pd.isna(total):
        return pd.Series(np.nan, index=df.index)
    return _sanitize_ts((df[kpi_column] / total) * 100)


def calculate_region_mix(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Region Mix — proportion of each region's KPI value within the total.

    Identical math to SKU/channel mix but semantically represents
    geographic share.  Produces (row_value / total_value) × 100.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by (unused)
        **kwargs: Additional parameters

    Returns:
        Series with region mix percentage values (0-100) aligned to df.index
    """
    total = df[kpi_column].sum()
    if total == 0 or pd.isna(total):
        return pd.Series(np.nan, index=df.index)
    return _sanitize_ts((df[kpi_column] / total) * 100)


def calculate_discount_depth(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Discount Depth — passthrough feature.

    Reads the pre-computed 'discount_depth' column from the DataFrame
    if it exists (populated by ML pipeline in FactTradePromotions).
    Falls back to NaN if the column is not present.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by
        **kwargs:
            source_column: column name to read (default='discount_depth')

    Returns:
        Series with discount depth values aligned to df.index
    """
    source_col = kwargs.get('source_column', 'discount_depth')

    # Passthrough: use pre-computed column if available
    if source_col in df.columns:
        return _sanitize_ts(pd.to_numeric(df[source_col], errors="coerce"))

    # Column not yet available — return NaN
    return pd.Series(np.nan, index=df.index)


def calculate_std_dev_3m(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Standard Deviation (3 months) — rolling standard deviation with a
    fixed 3-period window.

    Thin wrapper around calculate_std_dev with window=3 hardcoded.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by

    Returns:
        Series with 3-period rolling std dev values aligned to df.index
    """
    return calculate_std_dev(df, kpi_column, sort_columns, window=3)


def calculate_kpi_vs_rolling_avg_pct(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    KPI deviation from rolling average — expressed as a percentage.

    Returns ((kpi_value / rolling_avg) - 1) * 100.

    Why this exists: rolling_avg itself is in the same absolute units as the KPI
    (e.g. 1M, 50K), so a fixed threshold like "10 000" would only work for one KPI.
    Expressing the deviation as a % makes the threshold scale-independent:
      * +10 means KPI is 10% ABOVE the rolling average
      *  -25 means KPI is 25% BELOW the rolling average

    Pair with these operators for signals:
      * lt  0            → KPI below rolling average (any amount)
      * gt  50           → KPI more than 50% above rolling average  (critical)
      * between  25  50  → KPI 25–50% above rolling average         (warning)
      * between  10  25  → KPI 10–25% above rolling average         (info)
      * lt -50           → KPI more than 50% below rolling average  (critical)
      * between -50 -25  → KPI 25–50% below rolling average         (warning)
      * between -25 -10  → KPI 10–25% below rolling average         (info)

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by (e.g., ['period'])
        **kwargs:
            window: rolling window in periods (default=3)

    Returns:
        Series with % deviation from rolling average, aligned to df.index
    """
    window = kwargs.get('window', 3)
    gb = _groupby_cols(df, kwargs)
    df_sorted = _sorted_frame(df, sort_columns) if sort_columns else df.copy()
    if gb:
        rolling_avg = df_sorted.groupby(gb, sort=False)[kpi_column].transform(
            lambda s: s.rolling(window=window, min_periods=1).mean()
        )
    else:
        rolling_avg = df_sorted[kpi_column].rolling(window=window, min_periods=1).mean()
    deviation_pct = (df_sorted[kpi_column] / rolling_avg.replace(0, np.nan) - 1) * 100
    return _sanitize_ts(deviation_pct).reindex(df.index)


def calculate_acceleration(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate acceleration — rate of change in growth (second derivative).

    Acceleration = current period growth% - previous period growth%.
    Positive = growth is accelerating, Negative = growth is decelerating.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by (e.g., date/period columns)
        **kwargs: Additional parameters
            periods: pct_change lookback (default=1, i.e. MoM growth base)

    Returns:
        Series with acceleration values (percentage points) aligned to df.index
    """
    # periods = kwargs.get('periods', 1)

    #### Convert sort columns to datetime if they look like dates (dd-mm-yyyy)
    # for col in sort_columns:
    #     if df[col].dtype == object:
    #         try:
    #             df[col] = pd.to_datetime(df[col], format='%d-%m-%Y')
    #         except:
    #             pass  # keep as is if conversion fails

    # df_sorted = df.sort_values(by=sort_columns).copy()
    # growth = df_sorted[kpi_column].pct_change(periods=periods)
    # acceleration = growth.diff()
    # return acceleration.reindex(df.index)

    periods = int(kwargs.get("periods", 1))
    gb = _groupby_cols(df, kwargs)
    df_sorted = _sorted_frame(df, sort_columns)
    use_pp = _growth_use_percentage_points(kwargs)
    if _any_strict_calendar_growth(kwargs):
        growth = calculate_growth_pct(df, kpi_column, sort_columns, **kwargs)
        growth = growth.reindex(df_sorted.index)
    else:
        growth = _pct_change_or_pp_points(df_sorted, kpi_column, gb, periods, use_pp)
    growth = _sanitize_ts(growth)
    dft = df_sorted.copy()
    dft["_growth_pct_tmp"] = growth
    if gb:
        acceleration = dft.groupby(gb, sort=False)["_growth_pct_tmp"].diff()
    else:
        acceleration = dft["_growth_pct_tmp"].diff()
    return _sanitize_ts(acceleration).reindex(df.index)


def calculate_growth_abs(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate absolute period-over-period change in KPI value.

    Returns current_value - previous_value (raw KPI units, not %).
    Use this feature when signal thresholds should be in absolute KPI units
    (e.g. "fire when revenue drops by more than 1,000,000") rather than %.

    Pair with format='absolute' on the signal definition.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column to calculate change for
        sort_columns: Columns to sort by (e.g., ['period'])
        **kwargs:
            periods: number of periods to look back (default=1)

    Returns:
        Series with absolute change values in raw KPI units aligned to df.index
    """
    use_pp = _growth_use_percentage_points(kwargs)
    if kwargs.get("strict_calendar_year_growth"):
        yc = _resolve_year_col_for_strict_growth(df, kwargs)
        if yc:
            kw = dict(kwargs)
            kw["calendar_year_column"] = yc
            return _strict_calendar_period_compare(
                df,
                kpi_column,
                sort_columns,
                kw,
                grain="year",
                as_pct=False,
                as_percentage_points=use_pp,
            )
    if kwargs.get("strict_calendar_quarter_growth"):
        ycq, _qcol = _resolve_quarter_cols_for_strict_growth(df, kwargs)
        if ycq and _qcol:
            return _strict_calendar_period_compare(
                df,
                kpi_column,
                sort_columns,
                kwargs,
                grain="quarter",
                as_pct=False,
                as_percentage_points=use_pp,
            )
    if kwargs.get("strict_calendar_month_growth"):
        ycm, _mcol = _resolve_month_cols_for_strict_growth(df, kwargs)
        if ycm and _mcol:
            return _strict_calendar_period_compare(
                df,
                kpi_column,
                sort_columns,
                kwargs,
                grain="month",
                as_pct=False,
                as_percentage_points=use_pp,
            )
    periods = int(kwargs.get("periods", 1))
    gb = _groupby_cols(df, kwargs)
    df_sorted = _sorted_frame(df, sort_columns)
    if use_pp:

        def _pp_diff(s: pd.Series) -> pd.Series:
            scaled = _series_to_percent_point_scale(s)
            return scaled - scaled.shift(periods)

        if gb:
            change = df_sorted.groupby(gb, sort=False)[kpi_column].transform(_pp_diff)
        else:
            change = _pp_diff(df_sorted[kpi_column])
    elif gb:
        change = df_sorted.groupby(gb, sort=False)[kpi_column].transform(
            lambda s: s.diff(periods=periods)
        )
    else:
        change = df_sorted[kpi_column].diff(periods=periods)
    return _sanitize_ts(change).reindex(df.index)


def calculate_acceleration_abs(df: pd.DataFrame, kpi_column: str, sort_columns: list, **kwargs) -> pd.Series:
    """
    Calculate absolute acceleration — change in absolute period-over-period diff.

    Returns diff(diff(kpi_value)) in raw KPI units.
    Use when you want to detect momentum in absolute value terms rather than % points.

    Pair with format='absolute' on the signal definition.

    Args:
        df: DataFrame with KPI values
        kpi_column: Name of the KPI column
        sort_columns: Columns to sort by
        **kwargs:
            periods: lookback for the first diff (default=1)

    Returns:
        Series with absolute acceleration values (raw KPI units) aligned to df.index
    """
    periods = int(kwargs.get("periods", 1))
    gb = _groupby_cols(df, kwargs)
    df_sorted = _sorted_frame(df, sort_columns)
    use_pp = _growth_use_percentage_points(kwargs)
    if _any_strict_calendar_growth(kwargs):
        change = calculate_growth_abs(df, kpi_column, sort_columns, **kwargs)
        change = change.reindex(df_sorted.index)
    elif use_pp:

        def _pp_diff(s: pd.Series) -> pd.Series:
            scaled = _series_to_percent_point_scale(s)
            return scaled - scaled.shift(periods)

        if gb:
            change = df_sorted.groupby(gb, sort=False)[kpi_column].transform(_pp_diff)
        else:
            change = _pp_diff(df_sorted[kpi_column])
    elif gb:
        change = df_sorted.groupby(gb, sort=False)[kpi_column].transform(
            lambda s: s.diff(periods=periods)
        )
    else:
        change = df_sorted[kpi_column].diff(periods=periods)
    dft = df_sorted.copy()
    dft["_chg_tmp"] = change
    if gb:
        acceleration = dft.groupby(gb, sort=False)["_chg_tmp"].diff()
    else:
        acceleration = dft["_chg_tmp"].diff()
    return _sanitize_ts(acceleration).reindex(df.index)


# ---------------------------------------------------------------------------
# Feature Function Registry
# Maps function name strings (stored in DB) → actual Python callables.
# Used by config_db.py when loading feature definitions from the database.
# ---------------------------------------------------------------------------
FEATURE_FUNCTION_REGISTRY = {
    # --- Original (AdventureWorks) features ---
    'calculate_growth_pct':          calculate_growth_pct,
    'calculate_rolling_avg':         calculate_rolling_avg,
    'calculate_std_dev':             calculate_std_dev,
    'calculate_benchmark_variance':  calculate_benchmark_variance,
    'calculate_cumulative_sum':      calculate_cumulative_sum,
    'calculate_rank':                calculate_rank,
    'calculate_rank_pct':            calculate_rank_pct,
    # --- RGM features (Pratham) ---
    'calculate_z_score':             calculate_z_score,
    'calculate_kpi_vs_rolling_avg_pct': calculate_kpi_vs_rolling_avg_pct,
    # 'calculate_seasonality_index' is intentionally excluded — data pending from ML team.
    'calculate_sku_mix':             calculate_sku_mix,
    'calculate_channel_mix':         calculate_channel_mix,
    'calculate_region_mix':          calculate_region_mix,
    'calculate_discount_depth':      calculate_discount_depth,
    'calculate_std_dev_3m':          calculate_std_dev_3m,
    # --- RGM features (Aman) ---
    'calculate_acceleration':        calculate_acceleration,
    'calculate_yoy_growth_pct':      calculate_yoy_growth_pct,
    'calculate_qoq_growth_pct':      calculate_qoq_growth_pct,
    'calculate_mom_growth_pct':      calculate_mom_growth_pct,
    'calculate_wow_growth_pct':     calculate_wow_growth_pct,
    'calculate_self_kpi_value':       calculate_self_kpi_value,
    'calculate_rolling_avg_6m':      calculate_rolling_avg_6m,
    'calculate_rolling_avg_12m':     calculate_rolling_avg_12m,
    # --- Absolute-format variants (used when signal format='absolute') ---
    'calculate_growth_abs':          calculate_growth_abs,
    'calculate_acceleration_abs':    calculate_acceleration_abs,
}
