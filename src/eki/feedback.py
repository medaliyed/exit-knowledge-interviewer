"""Reader feedback and owner triage with transaction-local ACL/version checks."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from eki import access, maintenance
from eki.accounts import Throttled
from eki.audit import AuditDetails, append_event
from eki.auth import Principal, acting_as
from eki.errors import ConflictError, NotFoundError
from eki.ingest.redaction import redact
from eki.runbook import store
from eki.runbook.maintenance import expected
from eki.runbook.models import SECTION_NAMES


def receipt(flag: dict[str, Any]) -> dict[str, Any]:
    return {
        key: flag[key]
        for key in (
            "id",
            "entry_id",
            "request_id",
            "observed_version",
            "category",
            "section",
            "description",
            "created_at",
            "resolution",
            "resolved_at",
        )
    }


@dataclass
class FeedbackService:
    dsn: str = field(repr=False)
    domains: frozenset[str] = frozenset()
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, autocommit=True)

    def _flag(
        self, conn: psycopg.Connection, flag_id: UUID | str, principal: Principal
    ) -> tuple[dict[str, Any], UUID, bool]:
        maintenance.active(conn, principal)
        with conn.cursor(row_factory=dict_row) as cursor:
            flag = cursor.execute("SELECT * FROM entry_flags WHERE id=%s", (flag_id,)).fetchone()
        if flag is None:
            raise NotFoundError("flag not found")
        run = access.resource_run(conn, "entries", flag["entry_id"])
        if run is None:
            raise NotFoundError("flag not found")
        visible = conn.execute(
            sql.SQL("SELECT 1 FROM ingest_runs r WHERE r.id=%s AND ") + sql.SQL(access.VISIBLE_RUN),
            (run, *access.visible_params(principal)),
        ).fetchone()
        if visible is None:
            raise NotFoundError("flag not found")
        owner = conn.execute("SELECT owner_subject FROM ingest_runs WHERE id=%s", (run,)).fetchone()
        manager = bool(principal.admin or (owner and owner[0] == principal.subject))
        if not manager and flag["reporter_subject"] != principal.subject:
            raise NotFoundError("flag not found")
        return dict(flag), run, manager

    def get(self, flag_id: UUID | str, principal: Principal) -> dict[str, Any]:
        with self._connect() as conn:
            flag, _, manager = self._flag(conn, flag_id, principal)
            return flag if manager else receipt(flag)

    def list(
        self, entry_id: UUID, principal: Principal, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("invalid flag page")
        with self._connect() as conn:
            run = access.resource_run(conn, "entries", entry_id)
            if run is None:
                raise NotFoundError("entry not found")
            maintenance.active(conn, principal)
            access.require_manager(conn, run, principal)
            with conn.cursor(row_factory=dict_row) as cursor:
                flags = cursor.execute(
                    "SELECT * FROM entry_flags WHERE entry_id=%s ORDER BY created_at,id LIMIT"
                    " %s OFFSET %s",
                    (entry_id, limit, offset),
                ).fetchall()
            current = store.load_entry(conn, str(entry_id))
            return {
                "flags": [dict(f) for f in flags],
                "current_version": current.version if current else None,
                "next_offset": offset + limit if len(flags) == limit else None,
            }

    def create(
        self,
        entry_id: UUID,
        principal: Principal,
        *,
        request_id: UUID,
        expected_version: int,
        category: str,
        description: str,
        section: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if (
            category not in ("incorrect", "outdated", "unclear")
            or (section is not None and section not in SECTION_NAMES)
            or not description.strip()
            or len(description) > 2000
        ):
            raise ValueError("invalid feedback")
        clean = str(redact(description.strip(), work_email_domains=self.domains).text)
        now = self.clock()
        with acting_as(principal), self._connect() as conn, conn.transaction():
            run = access.resource_run(conn, "entries", entry_id)
            if run is None:
                raise NotFoundError("entry not found")
            maintenance.lock_run(conn, run)
            maintenance.active(conn, principal)
            visible = conn.execute(
                sql.SQL("SELECT 1 FROM ingest_runs r WHERE r.id=%s AND ")
                + sql.SQL(access.VISIBLE_RUN),
                (run, *access.visible_params(principal)),
            ).fetchone()
            if visible is None:
                raise NotFoundError("entry not found")
            with conn.cursor(row_factory=dict_row) as cursor:
                previous = cursor.execute(
                    "SELECT * FROM entry_flags WHERE entry_id=%s AND reporter_subject=%s AND "
                    "request_id=%s",
                    (entry_id, principal.subject, request_id),
                ).fetchone()
            if previous:
                if (
                    previous["observed_version"],
                    previous["category"],
                    previous["section"],
                    previous["description"],
                ) != (expected_version, category, section, clean):
                    raise ConflictError("request ID already used for different feedback")
                return receipt(dict(previous)), False
            entry = store.load_entry(conn, str(entry_id), lock=True)
            if entry is None or entry.entry.status != "approved":
                raise NotFoundError("entry not found")
            if entry.version != expected_version:
                raise ConflictError("entry changed; reopen before submitting feedback")
            conn.execute(
                "INSERT INTO flag_throttle(subject,window_at,count) VALUES(%s,%s,0) ON "
                "CONFLICT(subject) DO NOTHING",
                (principal.subject, now),
            )
            window = conn.execute(
                "SELECT window_at,count FROM flag_throttle WHERE subject=%s FOR UPDATE",
                (principal.subject,),
            ).fetchone()
            if window is None:
                raise RuntimeError("missing throttle")
            expired = now - window[0] >= timedelta(minutes=1)
            if not expired and window[1] >= 10:
                raise Throttled(max(1, int(60 - (now - window[0]).total_seconds())))
            conn.execute(
                "UPDATE flag_throttle SET window_at=%s,count=%s WHERE subject=%s",
                (now if expired else window[0], 1 if expired else window[1] + 1, principal.subject),
            )
            flag_id = uuid4()
            with conn.cursor(row_factory=dict_row) as cursor:
                flag = cursor.execute(
                    "INSERT INTO entry_flags(id,entry_id,reporter_subject,request_id,observed"
                    "_version,category,section,description,created_at,decision_cursor) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,(SELECT coalesce(max(id),0) FROM "
                    "entry_decisions WHERE entry_id=%s)) RETURNING *",
                    (
                        flag_id,
                        entry_id,
                        principal.subject,
                        request_id,
                        expected_version,
                        category,
                        section,
                        clean,
                        now,
                        entry_id,
                    ),
                ).fetchone()
            conn.execute(
                "UPDATE runbook_entries SET maintenance_revision=maintenance_revision+1 "
                "WHERE id=%s",
                (entry_id,),
            )
            append_event(
                conn,
                "review",
                "completed",
                AuditDetails(operation="flag_created", target_id=flag_id, version=entry.version),
                run_id=run,
            )
            if flag is None:
                raise RuntimeError("feedback not saved")
            return receipt(dict(flag)), True

    def resolve(
        self,
        flag_id: UUID | str,
        principal: Principal,
        *,
        resolution: str,
        note: str,
        expected_version: int,
        expected_maintenance_revision: int,
    ) -> dict[str, Any]:
        if (
            resolution not in ("corrected", "confirmed_current", "dismissed")
            or not note.strip()
            or len(note) > 2000
        ):
            raise ValueError("resolution needs a valid action and reason")
        clean = redact(note.strip(), work_email_domains=self.domains).text
        with acting_as(principal), self._connect() as conn, conn.transaction():
            flag, run, _ = self._flag(conn, flag_id, principal)
            owner, _, _, _ = maintenance.lock_run(conn, run)
            maintenance.active(conn, principal)
            if owner != principal.subject:
                raise NotFoundError("flag not found")
            entry = store.load_entry(conn, str(flag["entry_id"]))
            if entry is None:
                raise NotFoundError("entry not found")
            conn.execute("SELECT 1 FROM sessions WHERE id=%s FOR UPDATE", (entry.session_id,))
            entry = store.load_entry(conn, str(flag["entry_id"]), lock=True)
            if entry is None:
                raise NotFoundError("entry not found")
            expected(
                conn,
                entry.entry.id,
                entry.version,
                expected_version,
                expected_maintenance_revision,
                required=True,
            )
            locked = conn.execute(
                "SELECT resolution FROM entry_flags WHERE id=%s FOR UPDATE", (flag_id,)
            ).fetchone()
            if locked is None or locked[0] is not None:
                raise ConflictError("flag has already been resolved")
            if resolution != "dismissed" and entry.entry.status != "approved":
                raise ConflictError("approve the reviewed entry before resolving this flag")
            if resolution == "corrected":
                # Withdrawing and re-approving bumps the version without changing content;
                # a correction must follow a content edit made after this report.
                edited = conn.execute(
                    "SELECT 1 FROM entry_decisions WHERE entry_id=%s AND action IN "
                    "('edit','correction') AND id>%s",
                    (entry.entry.id, flag["decision_cursor"]),
                ).fetchone()
                if entry.version <= flag["observed_version"] or not edited:
                    raise ConflictError(
                        "correction requires an edit after this report and a later approved version"
                    )
            if resolution == "confirmed_current":
                reviewed = conn.execute(
                    "SELECT 1 FROM entry_decisions WHERE entry_id=%s AND action='review' AND "
                    "actor_subject=%s AND version=%s AND id>%s",
                    (entry.entry.id, principal.subject, entry.version, flag["decision_cursor"]),
                ).fetchone()
                if not reviewed:
                    raise ConflictError("review the current version before confirming this flag")
            conn.execute(
                "UPDATE entry_flags SET resolution=%s,resolved_at=%s,resolved_by=%s,resol"
                "ution_note=%s,resolved_version=%s WHERE id=%s",
                (resolution, self.clock(), principal.subject, clean, entry.version, flag_id),
            )
            conn.execute(
                "UPDATE runbook_entries SET maintenance_revision=maintenance_revision+1 "
                "WHERE id=%s",
                (entry.entry.id,),
            )
            append_event(
                conn,
                "review",
                "completed",
                AuditDetails(
                    operation="flag_resolved", target_id=UUID(str(flag_id)), version=entry.version
                ),
                run_id=run,
            )
            return {"id": str(flag_id), "resolution": resolution}
