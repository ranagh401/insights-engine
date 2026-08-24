"""
Database Connection Module
============================
Provides connection helpers for SQL Server (AdventureWorks) and
Databricks SQL Warehouse (RGM Gold Layer).

ALL credentials are loaded from environment variables via InsightsConfig.
No hardcoded secrets.
"""
import pyodbc
import pandas as pd
import requests
import time as _time
from typing import Optional

from ..config import get_settings


def _get_conn_str() -> str:
    return get_settings().sqlserver.conn_str


def _get_databricks_host() -> str:
    return get_settings().databricks.host


def _get_databricks_token() -> str:
    return get_settings().databricks.token


def _get_databricks_warehouse_id() -> str:
    return get_settings().databricks.warehouse_id


def _get_databricks_ns() -> str:
    s = get_settings().databricks
    return f"{s.catalog}.{s.schema_name}"


# Module-level accessors (used as constants by other modules)
DATABRICKS_NS = _get_databricks_ns()


# For backwards compatibility — these are functions, not constants
def get_databricks_ns() -> str:
    return _get_databricks_ns()


# ═══════════════════════════════════════════════════════════════════════════════
# Data Query Functions
# ═══════════════════════════════════════════════════════════════════════════════

def get_data(sql_query: str) -> pd.DataFrame:
    """Execute SQL query against SQL Server and return a DataFrame."""
    conn = pyodbc.connect(_get_conn_str())
    cursor = conn.cursor()
    cursor.execute(sql_query)

    if not cursor.description:
        conn.close()
        return pd.DataFrame()

    columns = [column[0] for column in cursor.description]
    rows = cursor.fetchall()
    df = pd.DataFrame.from_records(rows, columns=columns)

    conn.close()
    return df


def get_data_databricks(sql_query: str) -> pd.DataFrame:
    """Execute SQL query against Databricks SQL Warehouse via REST API."""
    host = _get_databricks_host()
    token = _get_databricks_token()
    warehouse_id = _get_databricks_warehouse_id()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    resp = requests.post(
        f"{host}/api/2.0/sql/statements",
        headers=headers,
        json={
            "statement": sql_query,
            "warehouse_id": warehouse_id,
            "wait_timeout": "50s",
        },
        timeout=120,
    ).json()

    statement_id = resp.get("statement_id")

    while resp.get("status", {}).get("state") in ("PENDING", "RUNNING"):
        _time.sleep(2)
        resp = requests.get(
            f"{host}/api/2.0/sql/statements/{statement_id}",
            headers=headers,
            timeout=60,
        ).json()

    state = resp.get("status", {}).get("state", "UNKNOWN")
    if state != "SUCCEEDED":
        error_msg = resp.get("status", {}).get("error", {}).get("message", str(resp))
        raise RuntimeError(f"Databricks query failed ({state}): {error_msg}")

    schema_cols = resp.get("manifest", {}).get("schema", {}).get("columns", [])
    columns = [col["name"] for col in schema_cols]
    data = resp.get("result", {}).get("data_array", [])

    df = pd.DataFrame(data, columns=columns)

    _NUMERIC_TYPES = {
        'BYTE', 'SHORT', 'INT', 'LONG', 'FLOAT', 'DOUBLE', 'DECIMAL',
    }
    for col_meta in schema_cols:
        col_name = col_meta["name"]
        type_name = col_meta.get("type_name", "").upper()
        if col_name not in df.columns:
            continue
        if type_name in _NUMERIC_TYPES:
            df[col_name] = pd.to_numeric(df[col_name], errors='coerce')
        elif type_name == 'BOOLEAN':
            df[col_name] = df[col_name].map({'true': True, 'false': False})

    return df


def get_data_for_backend(sql_query: str, backend: str = "sqlserver") -> pd.DataFrame:
    """Route a data query to the correct backend."""
    if backend == "databricks":
        return get_data_databricks(sql_query)
    return get_data(sql_query)


def format_currency(value):
    if pd.isna(value):
        return "$0.00"
    return f"${value:,.2f}"


def format_integer(value):
    if pd.isna(value):
        return "0"
    return f"{int(value):,}"


def format_decimal(value, decimals=2):
    if pd.isna(value):
        return "0.00"
    return f"{value:,.{decimals}f}"


def execute_ddl(sql: str) -> None:
    """Execute a DDL statement on SQL Server (errors silently ignored)."""
    try:
        conn = pyodbc.connect(_get_conn_str())
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute(sql)
        conn.close()
    except Exception:
        pass


def _safe_val(v):
    """Convert NaN/Inf/numpy scalars to Python-native values for pyodbc."""
    import math
    import numpy as np

    if v is None:
        return None
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, (float, np.floating)):
        if math.isnan(v) or math.isinf(v):
            return None
        return float(v)
    if isinstance(v, np.bool_):
        return bool(v)
    return v


def write_dataframe(df: pd.DataFrame, table_name: str, schema: str = 'Insights') -> int:
    """Bulk-insert a DataFrame into a SQL Server table using fast_executemany."""
    if df.empty:
        return 0

    conn = pyodbc.connect(_get_conn_str())
    conn.autocommit = False
    cursor = conn.cursor()
    cursor.fast_executemany = True

    cols = ', '.join(df.columns.tolist())
    placeholders = ', '.join(['?' for _ in df.columns])
    sql = f"INSERT INTO [{schema}].[{table_name}] ({cols}) VALUES ({placeholders})"

    rows = [
        tuple(_safe_val(v) for v in row)
        for row in df.itertuples(index=False, name=None)
    ]

    cursor.executemany(sql, rows)
    conn.commit()
    conn.close()
    return len(rows)


def execute_ddl_databricks(sql: str) -> None:
    """Execute a DDL statement on Databricks (errors silently ignored)."""
    try:
        get_data_databricks(sql)
    except Exception:
        pass


def _safe_val_db(v) -> str:
    """Convert a Python/numpy value to a Databricks SQL string literal or NULL."""
    import math
    import numpy as np

    if v is None:
        return "NULL"
    if isinstance(v, (np.bool_, bool)):
        return "true" if v else "false"
    if isinstance(v, np.integer):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        if math.isnan(v) or math.isinf(v):
            return "NULL"
        return str(float(v))
    return "'" + str(v).replace("'", "''") + "'"


def _get_table_columns_databricks(full_table: str) -> list[str]:
    """Return physical column names for a Databricks table."""
    try:
        df = get_data_databricks(f"DESCRIBE TABLE {full_table}")
    except Exception:
        return []
    if df.empty or "col_name" not in df.columns:
        return []

    cols: list[str] = []
    for raw in df["col_name"].tolist():
        name = str(raw).strip() if raw is not None else ""
        if not name or name.startswith("#"):
            continue
        # DESCRIBE TABLE appends partition/info sections after this marker.
        if name.lower().startswith("partition information"):
            break
        cols.append(name)
    return cols


def write_dataframe_databricks(df: pd.DataFrame, full_table: str) -> int:
    """Bulk-insert a DataFrame into a Databricks Delta table via REST API."""
    if df.empty:
        return 0

    table_columns = _get_table_columns_databricks(full_table)
    if table_columns:
        common_cols = [c for c in df.columns if c in table_columns]
        if not common_cols:
            raise RuntimeError(
                f"No matching target columns for insert into {full_table}. "
                f"DataFrame columns: {list(df.columns)}"
            )
        dropped_cols = [c for c in df.columns if c not in common_cols]
        if dropped_cols:
            print(
                f"    [write] dropping non-existent target columns for {full_table}: "
                f"{dropped_cols}"
            )
        df = df[common_cols].copy()

    batch_size = 100
    total = 0
    cols = ", ".join(df.columns.tolist())
    num_batches = (len(df) + batch_size - 1) // batch_size

    for batch_idx, start in enumerate(range(0, len(df), batch_size), 1):
        batch = df.iloc[start: start + batch_size]
        value_rows = [
            "(" + ", ".join(_safe_val_db(v) for v in row) + ")"
            for row in batch.itertuples(index=False, name=None)
        ]
        sql = f"INSERT INTO {full_table} ({cols}) VALUES {', '.join(value_rows)}"
        print(f"    [write] batch {batch_idx}/{num_batches} ({len(batch)} rows) ...")
        get_data_databricks(sql)
        total += len(batch)

    return total


def ensure_backends_ready(settings=None):
    """Verify that at least one backend is reachable (called at startup)."""
    if settings is None:
        settings = get_settings()

    # Try SQL Server
    try:
        conn = pyodbc.connect(settings.sqlserver.conn_str, timeout=5)
        conn.close()
        print("[startup] SQL Server connection OK")
    except Exception as exc:
        print(f"[startup] SQL Server connection failed: {exc}")

    # Try Databricks
    try:
        headers = {"Authorization": f"Bearer {settings.databricks.token}"}
        resp = requests.get(
            f"{settings.databricks.host}/api/2.0/sql/warehouses/{settings.databricks.warehouse_id}",
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            print("[startup] Databricks connection OK")
        else:
            print(f"[startup] Databricks check returned {resp.status_code}")
    except Exception as exc:
        print(f"[startup] Databricks connection failed: {exc}")
