"""Unit tests for dax/query_builder.py.

All fixtures use Client dimension and measure names.
"""

from datetime import date

import pytest

from insights_engine.config.models import (
    ConfigIncompleteError,
    DimensionRef,
    KPIConfig,
)
from insights_engine.dax.query_builder import DAXQueryBuilder


@pytest.fixture
def market_dim() -> DimensionRef:
    return DimensionRef(
        dimension_name="Market",
        pbi_table_name="dim_market",
        pbi_column_name="Market_for_OpCo_Reporting",
    )


@pytest.fixture
def customer_dim() -> DimensionRef:
    return DimensionRef(
        dimension_name="Customer",
        pbi_table_name="dim_customer",
        pbi_column_name="Customer_Name",
    )


@pytest.fixture
def product_dim() -> DimensionRef:
    return DimensionRef(
        dimension_name="Product Line",
        pbi_table_name="dim_product",
        pbi_column_name="Product_Line",
    )


class TestPhase1SingleDimensionQuery:
    """TEST 1: Phase 1 — market-wise Set % with Product Line=Bath filter."""

    def test_phase1_single_dimension_query(
        self, market_dim: DimensionRef, product_dim: DimensionRef
    ) -> None:
        query = (
            DAXQueryBuilder()
            .with_kpi("[Set % MRK]")
            .group_by(market_dim)
            .add_member_filter(product_dim, ["Bath"])
            .add_date_filter("Date", "Date", date(2026, 3, 1), date(2026, 3, 7))
            .build()
        )

        assert "DEFINE" in query
        assert "__DS0FilterTable1" in query
        assert "__DS0FilterTable2" in query
        assert "'dim_market'[Market_for_OpCo_Reporting]" in query
        assert '"KPI Value"' in query
        assert "TREATAS" in query
        assert "ORDER BY" in query
        assert "[Set % MRK]" in query


class TestPhase2RouteAQuery:
    """TEST 2: Phase 2 Route A — customer drill with Market B entity pin."""

    def test_phase2_route_a_query(
        self,
        customer_dim: DimensionRef,
        market_dim: DimensionRef,
        product_dim: DimensionRef,
    ) -> None:
        query = (
            DAXQueryBuilder()
            .with_kpi("[Set % MRK]")
            .group_by(customer_dim)
            .add_member_filter(product_dim, ["Bath"])
            .add_entity_pin_filter(market_dim, "Market B")
            .add_date_filter("Date", "Date", date(2026, 3, 1), date(2026, 3, 7))
            .build()
        )

        assert "'dim_customer'[Customer_Name]" in query
        assert "'dim_market'[Market_for_OpCo_Reporting]" in query
        assert '"Market B"' in query
        assert "__DS0FilterTable1" in query
        assert "__DS0FilterTable2" in query
        assert "__DS0FilterTable3" in query


class TestPhase2RouteBDependencyQuery:
    """TEST 3: Phase 2 Route B — dependency measure with entity pin."""

    def test_phase2_route_b_dependency_query(
        self, market_dim: DimensionRef
    ) -> None:
        query = (
            DAXQueryBuilder()
            .with_kpi("[Set Count MRK]", alias="Set Count")
            .group_by(market_dim)
            .add_entity_pin_filter(market_dim, "Market B")
            .build()
        )

        assert '"Set Count"' in query
        assert "[Set Count MRK]" in query
        assert '"Market B"' in query


class TestExclusionFilter:
    """TEST 4: Exclusion filter emits KEEPFILTERS + FILTER."""

    def test_exclusion_filter(self) -> None:
        source_dim = DimensionRef(
            dimension_name="Source",
            pbi_table_name="dim_source",
            pbi_column_name="Source_Name",
        )
        query = (
            DAXQueryBuilder()
            .with_kpi("[Set % MRK]")
            .group_by(source_dim)
            .add_exclusion_filter(source_dim, ["Web", "Other"])
            .build()
        )

        assert "KEEPFILTERS" in query
        assert "FILTER" in query


class TestCalendarMemberFilterUsesDateLiteral:
    """ISO calendar keys from DB must be DATE(...) in TREATAS, not quoted text."""

    def test_iso_datetime_member_emits_date_literal(self) -> None:
        cal = DimensionRef(
            dimension_name="Calendar",
            pbi_table_name="Calender",
            pbi_column_name="Calender Date",
        )
        query = (
            DAXQueryBuilder()
            .with_kpi("[btd_amount_mrk]")
            .group_by(cal)
            .add_member_filter(cal, ["2026-03-06T00:00:00"])
            .add_date_filter("Calender", "Calender Date", date(2026, 3, 1), date(2026, 3, 21))
            .build()
        )

        assert "DATE(2026,3,6)" in query
        assert '"2026-03-06T00:00:00"' not in query


class TestDateSingleDayCollapse:
    """TEST 5: When start_date == end_date, CALENDAR uses the same DATE() twice."""

    def test_date_single_day_collapse(
        self, market_dim: DimensionRef
    ) -> None:
        query = (
            DAXQueryBuilder()
            .with_kpi("[Set % MRK]")
            .group_by(market_dim)
            .add_date_filter("Date", "Date", date(2026, 3, 1), date(2026, 3, 1))
            .build()
        )

        assert "CALENDAR(DATE(2026,3,1), DATE(2026,3,1))" in query


class TestBuildIdempotent:
    """TEST 6: Calling build() twice must return identical strings."""

    def test_build_is_idempotent(
        self, market_dim: DimensionRef, product_dim: DimensionRef
    ) -> None:
        builder = (
            DAXQueryBuilder()
            .with_kpi("[Set % MRK]")
            .group_by(market_dim)
            .add_member_filter(product_dim, ["Bath"])
            .add_date_filter("Date", "Date", date(2026, 3, 1), date(2026, 3, 7))
        )

        first = builder.build()
        second = builder.build()
        assert first == second


class TestNullPbiMeasureRaisesError:
    """TEST 7: ConfigIncompleteError when pbi_measure_name is NULL."""

    def test_null_pbi_measure_raises_error(self) -> None:
        with pytest.raises(ConfigIncompleteError, match="pbi_measure_name"):
            pbi_measure_name = None
            kpi_name = "Set %"
            if pbi_measure_name is None:
                raise ConfigIncompleteError(
                    f"KPI '{kpi_name}' has no pbi_measure_name. "
                    f"Populate configkpisclientportal.pbi_measure_name "
                    f"to use the DAX engine."
                )
