"""Keep user input out of the web server's access log (F2).

Uvicorn logs one line per request with the full URL, query string included,
before the application has seen or redacted anything. No route takes free
text in its query string, but a client can still put anything there, so the
access log keeps only the path.
"""

import logging

QUERY_DROPPED = "[query not logged]"
ACCESS_LOGGER = "uvicorn.access"


class PathOnly(logging.Filter):
    """Replace the query string of each access-log line with a fixed marker."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's record: (client, method, path with query, HTTP version, status).
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path, separator, _ = args[2].partition("?")
            if separator:
                record.args = (args[0], args[1], f"{path}?{QUERY_DROPPED}", *args[3:])
        return True


def install() -> None:
    """Add the filter to uvicorn's access logger once. Safe to call repeatedly."""
    logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(existing, PathOnly) for existing in logger.filters):
        logger.addFilter(PathOnly())
