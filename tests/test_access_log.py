"""F2: nothing typed by a user reaches the server log before redaction.

The web server writes one access-log line per request, URL included. Search
used to carry its query in the URL, so a secret typed into a search reached
the log unredacted. The query now travels in the request body, and the access
log keeps only the path of any URL.
"""

import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from eki.api.access_log import QUERY_DROPPED, PathOnly, install
from tests.support import FAKE_CREDENTIALS

SECRET = FAKE_CREDENTIALS[1]


def access_record(path: str) -> logging.LogRecord:
    """A record shaped exactly like the one uvicorn writes for each request."""
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", path, "1.1", 401),
        None,
    )


def test_the_access_log_keeps_the_path_and_drops_the_query() -> None:
    record = access_record(f"/runbook/search?q=password%3D{SECRET}&limit=5")
    assert PathOnly().filter(record) is True, "the line is still logged"
    assert record.getMessage() == (
        f'127.0.0.1:5000 - "GET /runbook/search?{QUERY_DROPPED} HTTP/1.1" 401'
    )
    plain = access_record("/healthz")
    PathOnly().filter(plain)
    assert plain.getMessage() == '127.0.0.1:5000 - "GET /healthz HTTP/1.1" 401'


def test_records_of_another_shape_are_left_alone() -> None:
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "started", None, None)
    assert PathOnly().filter(record) is True
    assert record.getMessage() == "started"


def test_installing_twice_adds_one_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    install()
    install()
    assert sum(isinstance(f, PathOnly) for f in logger.filters) == 1


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - local test server
            return int(response.status)
    except urllib.error.HTTPError as error:
        return error.code


def test_a_real_server_never_logs_a_query_string(tmp_path: Path) -> None:
    """Runs the app under uvicorn exactly as documented and reads what it logs."""
    port = _free_port()
    environment = {
        **os.environ,
        # Enough configuration to start; no database is reached by these requests.
        "EKI_DATABASE_URL": "postgresql://nobody:nothing@127.0.0.1:1/none",
        "EKI_API_TOKENS": "[]",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "eki.api.app:app", "--port", str(port)],
        cwd=tmp_path,  # no .env here
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while True:
            try:
                assert _get(base + "/healthz") == 200
                break
            except (urllib.error.URLError, ConnectionError):
                assert time.monotonic() < deadline, "the server did not start"
                assert server.poll() is None, "the server exited"
                time.sleep(0.2)
        _get(f"{base}/runbook/search?q=password%3D{SECRET}")
        _get(f"{base}/runbook/export?run_id={SECRET}")
        _get(f"{base}/healthz?token={SECRET}")
    finally:
        server.terminate()
        output, _ = server.communicate(timeout=10)
    assert '"GET /healthz HTTP/1.1" 200' in output, "access logging still works"
    assert f"GET /runbook/search?{QUERY_DROPPED} HTTP/1.1" in output
    assert SECRET not in output
