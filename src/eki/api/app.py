"""The eki HTTP API.

Phase 0 exposes a liveness probe only. It touches neither settings nor the
database, so the process can report that it is up before anything is configured.
"""

from fastapi import FastAPI

from eki import __version__


def create_app() -> FastAPI:
    app = FastAPI(title="Exit-Knowledge Interviewer", version=__version__)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
