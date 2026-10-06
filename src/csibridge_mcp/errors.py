class CsiError(Exception):
    """A problem to report to the caller as-is, as opposed to a bug in this server."""

    def __init__(self, kind: str, message: str, detail: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail
