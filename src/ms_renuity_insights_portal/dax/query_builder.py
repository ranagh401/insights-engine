"""DAX query builder for Power BI Execute Queries API.

Produces DEFINE / EVALUATE / SUMMARIZECOLUMNS queries with
TREATAS filters, exclusion filters, and ORDER BY clauses.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from ..config.models import DimensionRef


def _looks_like_iso_date_prefix(s: str) -> bool:
    """True if ``s`` begins with ``YYYY-MM-DD`` (calendar / timestamp strings from DB)."""
    if len(s) < 10:
        return False
    return (
        s[0:4].isdigit()
        and s[4] == "-"
        and s[5:7].isdigit()
        and s[7] == "-"
        and s[8:10].isdigit()
    )


def _member_value_to_treatas_literal(value: str) -> str:
    """Return a DAX scalar for a table constructor row (DATE(...) or quoted string).

    DB/API often store calendar keys as ISO-8601 text (e.g. ``2026-03-06T00:00:00``).
    ``TREATAS({{"..."}}, 'Cal'[Date])`` is typed as Text and fails when the column is Date;
    ``TREATAS({DATE(2026,3,6)}, ...)`` matches Date columns.
    """
    raw = str(value).strip()
    if not raw:
        return '""'
    if _looks_like_iso_date_prefix(raw):
        try:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
            return f"DATE({d.year},{d.month},{d.day})"
        except ValueError:
            pass
    escaped = raw.replace('"', '""')
    return f'"{escaped}"'


class DAXQueryBuilder:
    """Fluent, immutable builder that emits SUMMARIZECOLUMNS DAX queries."""

    def __init__(self) -> None:
        self._measure: Optional[str] = None
        self._measure_alias: str = "KPI Value"
        self._group_by: list[DimensionRef] = []
        self._treatas_filters: list[tuple] = []
        self._exclusion_filters: list[tuple[DimensionRef, list[str]]] = []
        self._having: list[tuple[str, float, Optional[float]]] = []

    def _clone(self) -> DAXQueryBuilder:
        new = DAXQueryBuilder()
        new._measure = self._measure
        new._measure_alias = self._measure_alias
        new._group_by = list(self._group_by)
        new._treatas_filters = list(self._treatas_filters)
        new._exclusion_filters = list(self._exclusion_filters)
        new._having = list(self._having)
        return new

    def with_kpi(self, measure_name: str, alias: str = "KPI Value") -> DAXQueryBuilder:
        c = self._clone()
        c._measure = measure_name
        c._measure_alias = alias
        return c

    def group_by(self, *dimensions: DimensionRef) -> DAXQueryBuilder:
        c = self._clone()
        c._group_by = list(dimensions)
        return c

    def add_date_filter(
        self,
        date_table: str,
        date_column: str,
        start_date: date,
        end_date: date,
    ) -> DAXQueryBuilder:
        c = self._clone()
        c._treatas_filters = c._treatas_filters + [
            ("date", date_table, date_column, start_date, end_date)
        ]
        return c

    def add_member_filter(
        self, dim: DimensionRef, values: list[str]
    ) -> DAXQueryBuilder:
        c = self._clone()
        c._treatas_filters = c._treatas_filters + [
            ("member", dim, tuple(values))
        ]
        return c

    def add_exclusion_filter(
        self, dim: DimensionRef, exclude_values: list[str]
    ) -> DAXQueryBuilder:
        c = self._clone()
        c._exclusion_filters = c._exclusion_filters + [
            (dim, list(exclude_values))
        ]
        return c

    def add_entity_pin_filter(
        self, dim: DimensionRef, entity_value: str
    ) -> DAXQueryBuilder:
        return self.add_member_filter(dim, [entity_value])

    def add_having(
        self, operator: str, value: float, value2: Optional[float] = None
    ) -> DAXQueryBuilder:
        """Filter on the computed measure value (a HAVING clause).

        ``operator`` is one of ``gt | lt | gte | lte | eq | ne | between``.
        ``between`` uses both ``value`` (low) and ``value2`` (high), inclusive.
        """
        c = self._clone()
        c._having = c._having + [(operator, value, value2)]
        return c

    _HAVING_OPS = {
        "gt": lambda m, v, v2: f"{m} > {v}",
        "lt": lambda m, v, v2: f"{m} < {v}",
        "gte": lambda m, v, v2: f"{m} >= {v}",
        "lte": lambda m, v, v2: f"{m} <= {v}",
        "eq": lambda m, v, v2: f"{m} = {v}",
        "ne": lambda m, v, v2: f"{m} <> {v}",
        "between": lambda m, v, v2: f"({m} >= {v} && {m} <= {v2})",
    }

    def _having_predicate(self) -> str:
        alias = f"[{self._measure_alias}]"
        parts: list[str] = []
        for op, v, v2 in self._having:
            fn = self._HAVING_OPS.get(op)
            if fn is None:
                raise ValueError(f"Unsupported having operator: {op!r}")
            if op == "between" and v2 is None:
                raise ValueError("'between' operator requires value2")
            parts.append(fn(alias, v, v2))
        return " && ".join(parts)

    def build(self) -> str:
        lines: list[str] = []
        var_names: list[str] = []
        counter = 0

        if self._treatas_filters:
            lines.append("DEFINE")
            for filt in self._treatas_filters:
                counter += 1
                var_name = f"__DS0FilterTable{counter}"
                var_names.append(var_name)

                if filt[0] == "member":
                    _, dim_ref, values = filt
                    ref = dim_ref.pbi_expression
                    inner = ", ".join(_member_value_to_treatas_literal(v) for v in values)
                    lines.append(f"    VAR {var_name} =")
                    lines.append(f"        TREATAS({{{inner}}}, {ref})")

                elif filt[0] == "date":
                    _, tbl, col, start_d, end_d = filt
                    ref = f"'{tbl}'[{col}]"
                    s = f"DATE({start_d.year},{start_d.month},{start_d.day})"
                    e = f"DATE({end_d.year},{end_d.month},{end_d.day})"
                    lines.append(f"    VAR {var_name} =")
                    lines.append("        TREATAS(")
                    lines.append(f"            CALENDAR({s}, {e}),")
                    lines.append(f"            {ref}")
                    lines.append("        )")

        group_exprs = [d.pbi_expression for d in self._group_by]

        sc_args: list[str] = list(group_exprs)
        sc_args.extend(var_names)

        for dim, exc_vals in self._exclusion_filters:
            vals_str = ", ".join(f'"{v}"' for v in exc_vals)
            ref = dim.pbi_expression
            sc_args.append(
                f"KEEPFILTERS(FILTER(ALL({ref}), NOT({ref} IN {{{vals_str}}})))"
            )

        sc_args.append(f'"{self._measure_alias}", {self._measure}')

        # Indent the SUMMARIZECOLUMNS block one extra level when it is wrapped
        # in FILTER(...) for a HAVING clause.
        having_pred = self._having_predicate() if self._having else ""
        pad = "        " if having_pred else "    "

        sc_lines: list[str] = [f"{pad[:-4]}SUMMARIZECOLUMNS("]
        for i, arg in enumerate(sc_args):
            comma = "," if i < len(sc_args) - 1 else ""
            sc_lines.append(f"{pad}{arg}{comma}")
        sc_lines.append(f"{pad[:-4]})")

        lines.append("EVALUATE")
        if having_pred:
            lines.append("FILTER(")
            sc_lines[-1] = sc_lines[-1] + ","
            lines.extend(sc_lines)
            lines.append(f"    {having_pred}")
            lines.append(")")
        else:
            lines.extend(sc_lines)

        if group_exprs:
            order_parts = ", ".join(f"{g} ASC" for g in group_exprs)
            lines.append(f"ORDER BY {order_parts}")

        return "\n".join(lines)


def build_union_why_query(
    measure: str,
    signal_dim_ref: DimensionRef,
    unique_entities: list[str],
    cross_dims: list[DimensionRef],
    dep_measures: list[tuple[str, str]],
    date_table: str,
    date_column: str,
    start_date: date,
    end_date: date,
    extra_filters: list[tuple[DimensionRef, list[str]]] | None = None,
) -> str:
    """Build a consolidated UNION DAX query for WHY analysis.

    Combines all cross-dimension (Route A) and dependency KPI (Route B)
    queries into a single UNION, sharing date/entity/extra filter VARs.

    Returns a DAX query with standardised output columns::

        DimensionName | SignalDimValue | DimensionValue | KPIValue

    Route A legs tag ``DimensionName`` with the cross-dimension name.
    Route B legs tag ``DimensionName`` with ``__dep__<kpi_name>``.

    Returns an empty string when there are no legs to build.
    """
    if not cross_dims and not dep_measures:
        return ""

    lines: list[str] = ["DEFINE"]
    var_names: list[str] = []
    counter = 0

    # ── Shared filter VARs ────────────────────────────────────────────────

    # Date filter
    counter += 1
    vn = f"__DS0FilterTable{counter}"
    var_names.append(vn)
    date_ref = f"'{date_table}'[{date_column}]"
    s = f"DATE({start_date.year},{start_date.month},{start_date.day})"
    e = f"DATE({end_date.year},{end_date.month},{end_date.day})"
    lines.append(f"    VAR {vn} =")
    lines.append("        TREATAS(")
    lines.append(f"            CALENDAR({s}, {e}),")
    lines.append(f"            {date_ref}")
    lines.append("        )")

    # Entity member filter
    counter += 1
    vn = f"__DS0FilterTable{counter}"
    var_names.append(vn)
    inner = ", ".join(_member_value_to_treatas_literal(v) for v in unique_entities)
    lines.append(f"    VAR {vn} =")
    lines.append(f"        TREATAS({{{inner}}}, {signal_dim_ref.pbi_expression})")

    # Extra filters (job filter_conditions)
    for dim_ref, vals in (extra_filters or []):
        counter += 1
        vn = f"__DS0FilterTable{counter}"
        var_names.append(vn)
        inner = ", ".join(_member_value_to_treatas_literal(v) for v in vals)
        lines.append(f"    VAR {vn} =")
        lines.append(f"        TREATAS({{{inner}}}, {dim_ref.pbi_expression})")

    filter_refs = ", ".join(var_names)
    sig_expr = signal_dim_ref.pbi_expression

    # ── UNION legs ────────────────────────────────────────────────────────

    legs: list[str] = []

    # Route A: one leg per cross-dimension (same base measure)
    for cdim in cross_dims:
        label = cdim.dimension_name.replace('"', '""')
        leg = "\n".join([
            "    SELECTCOLUMNS(",
            "        SUMMARIZECOLUMNS(",
            f"            {sig_expr}, {cdim.pbi_expression},",
            f"            {filter_refs},",
            f'            "__val", {measure}',
            "        ),",
            f'        "DimensionName", "{label}",',
            f'        "SignalDimValue", {sig_expr},',
            f'        "DimensionValue", {cdim.pbi_expression},',
            '        "KPIValue", [__val]',
            "    )",
        ])
        legs.append(leg)

    # Route B: one leg per dependency KPI (each has its own measure)
    for dep_name, dep_meas in dep_measures:
        label = f"__dep__{dep_name}".replace('"', '""')
        leg = "\n".join([
            "    SELECTCOLUMNS(",
            "        SUMMARIZECOLUMNS(",
            f"            {sig_expr},",
            f"            {filter_refs},",
            f'            "__val", {dep_meas}',
            "        ),",
            f'        "DimensionName", "{label}",',
            f'        "SignalDimValue", {sig_expr},',
            f'        "DimensionValue", {sig_expr},',
            '        "KPIValue", [__val]',
            "    )",
        ])
        legs.append(leg)

    # ── Assemble EVALUATE ─────────────────────────────────────────────────

    lines.append("EVALUATE")
    if len(legs) == 1:
        lines.append(legs[0])
    else:
        lines.append("UNION(")
        for i, leg in enumerate(legs):
            comma = "," if i < len(legs) - 1 else ""
            lines.append(f"{leg}{comma}")
        lines.append(")")

    return "\n".join(lines)
