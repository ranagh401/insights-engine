"""SQLAlchemy declarative base used by config ORM models."""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared metadata base for insights configuration tables."""
