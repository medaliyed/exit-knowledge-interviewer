"""Operation errors shared by the interview and runbook services."""


class NotFoundError(Exception):
    """The requested record does not exist."""


class ConflictError(Exception):
    """The operation does not fit the record's current state."""
