"""Structured-style logging compatible with call sites that pass keyword fields."""
import logging
from typing import Any, Mapping, Optional


def _format_msg(msg: str, kwargs: Mapping[str, Any]) -> str:
    if not kwargs:
        return msg
    parts = [f"{k}={v!r}" for k, v in kwargs.items()]
    return f"{msg} | " + " ".join(parts)


class _LoggerAdapter:
    """Accepts ``logger.info(\"msg\", key=value)`` like structlog."""

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        exc_info = kwargs.pop("exc_info", False)
        self._log.debug(_format_msg(msg % args if args else msg, kwargs), exc_info=exc_info)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log.info(_format_msg(msg % args if args else msg, kwargs))

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log.warning(_format_msg(msg % args if args else msg, kwargs))

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        exc_info = kwargs.pop("exc_info", False)
        self._log.error(_format_msg(msg % args if args else msg, kwargs), exc_info=exc_info)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log.exception(_format_msg(msg % args if args else msg, kwargs))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger: _LoggerAdapter = _LoggerAdapter("platform.insights")


def get_logger(name: Optional[str] = None) -> _LoggerAdapter:
    return _LoggerAdapter(name or "platform.insights")
