"""ConfigFactDimensionRelationship model — maps how fact tables join to dimension tables."""
from sqlalchemy import Column, String, Integer, Text, DateTime, func, UniqueConstraint

from platform_core.database import Base
from .schema import SCHEMA


class ConfigFactDimensionRelationship(Base):
    """
    Defines how each fact table's foreign key columns map to dimension tables.
    
    This enables automatic SQL generation with correct column aliases, handling cases where:
    - Different facts use different column names for the same dimension
    - Example: fact_sales.product_id vs fact_marketing.product_id_lead both join to dim_product
    
    Parsed from ER diagram relationship metadata.
    """
    __tablename__ = "config_fact_dimension_relationships"
    __table_args__ = (
        UniqueConstraint('fact_table', 'fact_fk_column', 'dimension_table', 
                        name='uq_fact_fk_dimension'),
        {"schema": SCHEMA}
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Source (Fact) side
    fact_table = Column(String(200), nullable=False, index=True,
                       comment="Name of the fact table (e.g., 'fact_lead', 'mrkcostdirectmail')")
    fact_fk_column = Column(String(100), nullable=False,
                           comment="Foreign key column in fact table (e.g., 'Branch_ID_Key', 'BranchID')")
    
    # Target (Dimension) side
    dimension_table = Column(String(200), nullable=False, index=True,
                            comment="Name of dimension table (e.g., 'dim_branch', 'xref_leads')")
    dimension_pk_column = Column(String(100), nullable=False,
                                comment="Primary key column in dimension table")
    
    # Standardization
    standard_alias = Column(String(100), nullable=False,
                           comment="Standard alias for KPI queries (e.g., 'BranchID', 'ProductID')")
    dimension_type = Column(String(100), nullable=True, index=True,
                           comment="Logical dimension category (e.g., 'branch', 'product', 'lead_source')")
    
    # Metadata
    relationship_type = Column(String(50), nullable=False, default='direct',
                              comment="Relationship type: 'direct', 'bridge', 'derived'")
    cardinality = Column(String(20), nullable=True,
                        comment="Cardinality from ER diagram (e.g., '2' = many-to-one)")
    description = Column(Text, nullable=True, default="")
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
