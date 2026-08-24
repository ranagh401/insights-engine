"""RabbitMQ-style publisher stub; audit path no-ops when exchange is unavailable."""


class _InnerPublisher:
    _exchange = None


class EventPublisher:
    """Minimal stand-in: ``_publisher._exchange`` is None so audit events are dropped."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._publisher = _InnerPublisher()

    async def _ensure_connected(self) -> None:
        return None
