"""Local synthetic browser QA: python -m tests.browser_preview (Ctrl-C cleans up)."""

from contextlib import contextmanager

import uvicorn
from psycopg.conninfo import make_conninfo

from eki.access import set_owner
from eki.api.app import create_app
from eki.config import get_settings
from eki.graph.workflow import setup_checkpoints
from tests.conftest import _scratch_connection
from tests.fakes import ScriptedModel, checklist_interviewer
from tests.interview_support import make_service, seed_run
from tests.test_api_access import ADMIN, OWNER, authenticator


def main() -> None:
    with contextmanager(_scratch_connection)(migrated=True) as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()
        assert schema is not None
        dsn = make_conninfo(
            get_settings().database_url.get_secret_value(),
            options=f"-c search_path={schema[0]},public",
        )
        setup_checkpoints(dsn)
        run = seed_run(conn)
        set_owner(conn, run, OWNER.subject, ADMIN)
        conn.commit()
        service = make_service(dsn, agent=None, interviewer=ScriptedModel(checklist_interviewer))
        print("Synthetic workspace: http://127.0.0.1:8765", flush=True)
        uvicorn.run(
            create_app(service, authenticator=authenticator()),
            host="127.0.0.1",
            port=8765,
            log_level="warning",
        )


if __name__ == "__main__":
    main()
