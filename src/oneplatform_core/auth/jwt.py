"""JWT manager placeholder; real verification is not required for local replica endpoints."""


class JWTManager:
    """Holds service settings and optional cache reference (production parity)."""

    def __init__(self, settings, cache=None) -> None:
        self.settings = settings
        self.cache = cache
