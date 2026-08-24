"""Entry point — starts the PBI DAX Engine API server via uvicorn."""

import os

import uvicorn
from .app import app


def main():
    port = int(os.getenv("PLATFORM_SERVICE__PORT", "8010"))
    uvicorn.run(
        "ms_crm_insights_portal.app:app",
        host="0.0.0.0",
        port=port,
        reload=True,
    )


if __name__ == "__main__":
    main()
