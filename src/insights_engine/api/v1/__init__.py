"""API v1 router — PBI (Power BI DAX) endpoints only."""

from fastapi import APIRouter
from ..pbi import pbi_router

api_router = APIRouter()
api_router.include_router(pbi_router, prefix="/pbi", tags=["PBI Engine"])
