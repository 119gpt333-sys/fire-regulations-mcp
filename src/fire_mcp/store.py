from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class RegulatoryStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def initialize(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    official_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    authority TEXT,
                    document_kind TEXT,
                    promulgation_date TEXT,
                    effective_date TEXT,
                    effective_to TEXT,
                    status TEXT,
                    official_url TEXT NOT NULL,
                    payload_sha256 TEXT,
                    collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    approved INTEGER NOT NULL DEFAULT 0,
                    review_status TEXT NOT NULL DEFAULT 'pending',
                    change_type TEXT,
                    UNIQUE(source_type, official_id, version_id)
                );
                CREATE TABLE IF NOT EXISTS provisions (
                    id INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    provision_path TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    effective_date TEXT
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS provisions_fts USING fts5(
                    title, provision_path, text, tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    documents_seen INTEGER NOT NULL DEFAULT 0,
                    documents_saved INTEGER NOT NULL DEFAULT 0,
                    errors TEXT
                );
                CREATE TABLE IF NOT EXISTS change_events (
                    id INTEGER PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    official_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    previous_sha256 TEXT,
                    candidate_sha256 TEXT,
                    candidate_payload TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at TEXT,
                    UNIQUE(source_type, official_id, version_id, candidate_sha256, status)
                );
                CREATE TABLE IF NOT EXISTS review_events (
                    id INTEGER PRIMARY KEY,
                    change_event_id INTEGER REFERENCES change_events(id),
                    source_type TEXT NOT NULL,
                    official_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            columns = {row[1] for row in con.execute("PRAGMA table_info(documents)")}
            migrations = {
                "effective_to": "ALTER TABLE documents ADD COLUMN effective_to TEXT",
                "review_status": (
                    "ALTER TABLE documents ADD COLUMN review_status TEXT NOT NULL DEFAULT 'approved'"
                ),
                "change_type": "ALTER TABLE documents ADD COLUMN change_type TEXT",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    con.execute(statement)
            # API 인증값이 포함된 과거 DRF HTML URL을 공개용 버전 고정 URL로 정리한다.
            con.execute(
                """
                UPDATE documents
                SET official_url='https://www.law.go.kr/lsInfoP.do?lsiSeq=' || version_id
                WHERE source_type='law' AND official_url LIKE '%OC=%'
                """
            )
            con.execute(
                """
                UPDATE documents
                SET official_url='https://www.law.go.kr/admRulInfoP.do?admRulSeq=' || version_id
                WHERE source_type='admrul' AND official_url LIKE '%OC=%'
                """
            )

    @staticmethod
    def _event_payload(document: dict[str, Any]) -> str:
        return json.dumps(document, ensure_ascii=False, sort_keys=True)

    def _record_change(
        self,
        con: sqlite3.Connection,
        document: dict[str, Any],
        *,
        change_type: str,
        previous_sha256: str | None,
    ) -> None:
        con.execute(
            """
            INSERT OR IGNORE INTO change_events (
                source_type, official_id, version_id, title, change_type,
                previous_sha256, candidate_sha256, candidate_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document["source_type"],
                document["official_id"],
                document["version_id"],
                document["title"],
                change_type,
                previous_sha256,
                document.get("payload_sha256"),
                self._event_payload(document),
            ),
        )

    @staticmethod
    def _replace_provisions(
        con: sqlite3.Connection, document_id: int, document: dict[str, Any]
    ) -> None:
        ids = [
            row[0]
            for row in con.execute("SELECT id FROM provisions WHERE document_id=?", (document_id,))
        ]
        con.executemany("DELETE FROM provisions_fts WHERE rowid=?", ((item,) for item in ids))
        con.execute("DELETE FROM provisions WHERE document_id=?", (document_id,))
        for provision in document.get("provisions", []):
            p_cur = con.execute(
                """INSERT INTO provisions
                (document_id, provision_path, kind, text, effective_date)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    document_id,
                    provision["provision_path"],
                    provision["kind"],
                    provision["text"],
                    provision.get("effective_date") or document.get("effective_date"),
                ),
            )
            con.execute(
                "INSERT INTO provisions_fts(rowid, title, provision_path, text) VALUES (?, ?, ?, ?)",
                (
                    int(p_cur.lastrowid),
                    document["title"],
                    provision["provision_path"],
                    provision["text"],
                ),
            )

    @staticmethod
    def _recompute_effective_intervals(
        con: sqlite3.Connection, source_type: str, official_id: str
    ) -> None:
        rows = list(
            con.execute(
                """
                SELECT id, effective_date FROM documents
                WHERE source_type=? AND official_id=? AND approved=1
                ORDER BY COALESCE(effective_date, '0000-00-00'), id
                """,
                (source_type, official_id),
            )
        )
        for index, row in enumerate(rows):
            effective_to = rows[index + 1]["effective_date"] if index + 1 < len(rows) else None
            con.execute("UPDATE documents SET effective_to=? WHERE id=?", (effective_to, row["id"]))
        con.execute(
            "UPDATE documents SET effective_to=NULL WHERE source_type=? AND official_id=? AND approved=0",
            (source_type, official_id),
        )

    def _write_document(
        self, con: sqlite3.Connection, document: dict[str, Any], *, approved: int
    ) -> int:
        existing = con.execute(
            "SELECT id FROM documents WHERE source_type=? AND official_id=? AND version_id=?",
            (document["source_type"], document["official_id"], document["version_id"]),
        ).fetchone()
        review_status = "approved" if approved else "pending"
        change_type = None if approved else "new_version"
        values = (
            document["title"],
            document.get("authority"),
            document.get("document_kind"),
            document.get("promulgation_date"),
            document.get("effective_date"),
            document.get("status"),
            document["official_url"],
            document.get("payload_sha256"),
            approved,
            review_status,
            change_type,
        )
        if existing:
            document_id = int(existing["id"])
            con.execute(
                """
                UPDATE documents SET title=?, authority=?, document_kind=?, promulgation_date=?,
                    effective_date=?, status=?, official_url=?, payload_sha256=?, approved=?,
                    review_status=?, change_type=?, collected_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (*values, document_id),
            )
        else:
            cur = con.execute(
                """
                INSERT INTO documents (
                    source_type, official_id, version_id, title, authority, document_kind,
                    promulgation_date, effective_date, status, official_url, payload_sha256,
                    approved, review_status, change_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document["source_type"],
                    document["official_id"],
                    document["version_id"],
                    *values,
                ),
            )
            document_id = int(cur.lastrowid)
        self._replace_provisions(con, document_id, document)
        self._recompute_effective_intervals(con, document["source_type"], document["official_id"])
        return document_id

    def upsert_document(self, document: dict[str, Any]) -> int:
        with self._connect() as con:
            old = con.execute(
                """
                SELECT id, payload_sha256, approved FROM documents
                WHERE source_type=? AND official_id=? AND version_id=?
                """,
                (document["source_type"], document["official_id"], document["version_id"]),
            ).fetchone()
            if old and old["payload_sha256"] == document.get("payload_sha256"):
                return int(old["id"])
            if old:
                self._record_change(
                    con,
                    document,
                    change_type="content_changed",
                    previous_sha256=old["payload_sha256"],
                )
                return int(old["id"])
            approved = 1 if document.get("approved") in (1, True) else 0
            document_id = self._write_document(con, document, approved=approved)
            if not approved:
                self._record_change(con, document, change_type="new_version", previous_sha256=None)
            return document_id

    @staticmethod
    def _fts_query(query: str) -> str:
        terms = [term.replace('"', '""') for term in query.split() if term.strip()]
        if not terms:
            raise ValueError("검색어가 비어 있습니다.")
        return " AND ".join(f'"{term}"' for term in terms)

    def search(
        self,
        query: str,
        *,
        as_of: str | None = None,
        source_type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        sql = """
            SELECT d.source_type, d.official_id, d.version_id, d.title, d.authority,
                   d.document_kind, d.promulgation_date, d.effective_date AS document_effective_date,
                   d.effective_to, d.status, d.official_url, d.review_status,
                   p.provision_path, p.kind, p.text,
                   COALESCE(p.effective_date, d.effective_date) AS effective_date,
                   bm25(provisions_fts) AS score
            FROM provisions_fts
            JOIN provisions p ON p.id = provisions_fts.rowid
            JOIN documents d ON d.id = p.document_id
            WHERE provisions_fts MATCH ? AND d.approved = 1
        """
        params: list[Any] = [self._fts_query(query)]
        if as_of:
            sql += """ AND COALESCE(p.effective_date, d.effective_date, '0000-00-00') <= ?
                AND (d.effective_to IS NULL OR ? < d.effective_to)"""
            params.extend([as_of, as_of])
        if source_type:
            sql += " AND d.source_type = ?"
            params.append(source_type)
        sql += " ORDER BY score, d.title, p.id LIMIT ?"
        params.append(limit)
        with self._connect() as con:
            rows = [dict(row) for row in con.execute(sql, params)]
            if rows:
                return rows
            # unicode61은 한국어 조사·어미가 붙은 토큰의 부분일치를 놓칠 수 있다.
            # 먼저 본문 부분일치로 보완하고, 본문 결과가 없을 때만 제목까지 넓힌다.
            terms = [term for term in query.split() if term.strip()]
            common = """
                SELECT d.source_type, d.official_id, d.version_id, d.title, d.authority,
                       d.document_kind, d.promulgation_date,
                       d.effective_date AS document_effective_date, d.effective_to,
                       d.status, d.official_url, d.review_status,
                       p.provision_path, p.kind, p.text,
                       COALESCE(p.effective_date, d.effective_date) AS effective_date,
                       0.0 AS score
                FROM provisions p JOIN documents d ON d.id=p.document_id
                WHERE d.approved=1
            """
            text_where = "".join(" AND p.text LIKE ?" for _ in terms)
            text_filters: list[Any] = [f"%{term}%" for term in terms]
            if as_of:
                text_where += """ AND COALESCE(p.effective_date, d.effective_date, '0000-00-00') <= ?
                    AND (d.effective_to IS NULL OR ? < d.effective_to)"""
                text_filters.extend([as_of, as_of])
            if source_type:
                text_where += " AND d.source_type=?"
                text_filters.append(source_type)
            text_rows = [
                dict(row)
                for row in con.execute(
                    common + text_where + " ORDER BY d.title, p.id LIMIT ?",
                    [*text_filters, limit],
                )
            ]
            if text_rows:
                return text_rows
            title_where = "".join(" AND d.title LIKE ?" for _ in terms)
            title_filters: list[Any] = [f"%{term}%" for term in terms]
            if as_of:
                title_where += """ AND COALESCE(p.effective_date, d.effective_date, '0000-00-00') <= ?
                    AND (d.effective_to IS NULL OR ? < d.effective_to)"""
                title_filters.extend([as_of, as_of])
            if source_type:
                title_where += " AND d.source_type=?"
                title_filters.append(source_type)
            return [
                dict(row)
                for row in con.execute(
                    common + title_where + " ORDER BY d.title, p.id LIMIT ?",
                    [*title_filters, limit],
                )
            ]

    def get_document(self, official_id: str, *, as_of: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM documents WHERE official_id=? AND approved=1"
        params: list[Any] = [official_id]
        if as_of:
            sql += """ AND COALESCE(effective_date, '0000-00-00') <= ?
                AND (effective_to IS NULL OR ? < effective_to)"""
            params.extend([as_of, as_of])
        sql += " ORDER BY effective_date DESC, id DESC LIMIT 1"
        with self._connect() as con:
            doc = con.execute(sql, params).fetchone()
            if not doc:
                return None
            result = dict(doc)
            result["provisions"] = [
                dict(row)
                for row in con.execute(
                    "SELECT provision_path, kind, text, effective_date FROM provisions WHERE document_id=? ORDER BY id",
                    (doc["id"],),
                )
            ]
            return result

    def trace_exception_path(
        self, official_id: str, *, as_of: str | None = None
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT d.title, d.official_id, d.version_id, d.official_url,
                   p.provision_path, p.kind, p.text,
                   COALESCE(p.effective_date, d.effective_date) AS effective_date
            FROM provisions p JOIN documents d ON d.id=p.document_id
            WHERE d.official_id=? AND d.approved=1
              AND (p.text LIKE '%다만%' OR p.text LIKE '%제외%' OR p.text LIKE '%갈음%'
                   OR p.text LIKE '%경과조치%' OR p.text LIKE '%적용하지 아니%')
        """
        params: list[Any] = [official_id]
        if as_of:
            sql += """ AND COALESCE(p.effective_date, d.effective_date, '0000-00-00') <= ?
                AND (d.effective_to IS NULL OR ? < d.effective_to)"""
            params.extend([as_of, as_of])
        sql += " ORDER BY d.effective_date DESC, p.id"
        with self._connect() as con:
            return [dict(row) for row in con.execute(sql, params)]

    def list_pending_changes(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            return [
                dict(row)
                for row in con.execute(
                    """
                    SELECT id AS change_event_id, source_type, official_id, version_id, title,
                           change_type, previous_sha256, candidate_sha256, status AS review_status,
                           detected_at
                    FROM change_events
                    WHERE status='pending'
                    ORDER BY detected_at, id
                    """
                )
            ]

    def review_version(
        self,
        source_type: str,
        official_id: str,
        version_id: str,
        *,
        decision: str,
        reviewer: str,
        reason: str,
    ) -> None:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision은 approved 또는 rejected여야 합니다.")
        if not reviewer.strip() or not reason.strip():
            raise ValueError("reviewer와 reason은 필수입니다.")
        with self._connect() as con:
            event = con.execute(
                """
                SELECT * FROM change_events
                WHERE source_type=? AND official_id=? AND version_id=? AND status='pending'
                ORDER BY id DESC LIMIT 1
                """,
                (source_type, official_id, version_id),
            ).fetchone()
            if not event:
                raise ValueError("검토 대기 중인 변경을 찾을 수 없습니다.")
            if decision == "approved":
                if event["change_type"] == "content_changed":
                    document = json.loads(event["candidate_payload"])
                    self._write_document(con, document, approved=1)
                else:
                    con.execute(
                        """
                        UPDATE documents
                        SET approved=1, review_status='approved', change_type=NULL
                        WHERE source_type=? AND official_id=? AND version_id=?
                        """,
                        (source_type, official_id, version_id),
                    )
                    self._recompute_effective_intervals(con, source_type, official_id)
            else:
                con.execute(
                    """
                    UPDATE documents SET review_status='rejected'
                    WHERE source_type=? AND official_id=? AND version_id=? AND approved=0
                    """,
                    (source_type, official_id, version_id),
                )
            con.execute(
                "UPDATE change_events SET status=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                (decision, event["id"]),
            )
            con.execute(
                """
                INSERT INTO review_events (
                    change_event_id, source_type, official_id, version_id,
                    decision, reviewer, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["id"],
                    source_type,
                    official_id,
                    version_id,
                    decision,
                    reviewer.strip(),
                    reason.strip(),
                ),
            )

    def catalog(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            return [
                dict(row)
                for row in con.execute(
                    """SELECT source_type, official_id, version_id, title, authority,
                    effective_date, effective_to, status, official_url, collected_at, review_status
                    FROM documents WHERE approved=1 ORDER BY source_type, title"""
                )
            ]

    def start_sync_run(self, started_at: str) -> int:
        with self._connect() as con:
            cur = con.execute(
                "INSERT INTO sync_runs(started_at, status) VALUES (?, 'running')",
                (started_at,),
            )
            return int(cur.lastrowid)

    def finish_sync_run(
        self,
        run_id: int,
        *,
        finished_at: str,
        status: str,
        documents_seen: int,
        documents_saved: int,
        errors: list[str],
    ) -> None:
        with self._connect() as con:
            con.execute(
                """UPDATE sync_runs SET finished_at=?, status=?, documents_seen=?,
                documents_saved=?, errors=? WHERE id=?""",
                (
                    finished_at,
                    status,
                    documents_seen,
                    documents_saved,
                    "\n".join(errors) if errors else None,
                    run_id,
                ),
            )

    def status(self) -> dict[str, Any]:
        with self._connect() as con:
            documents = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            approved_documents = con.execute(
                "SELECT COUNT(*) FROM documents WHERE approved=1"
            ).fetchone()[0]
            pending_changes = con.execute(
                "SELECT COUNT(*) FROM change_events WHERE status='pending'"
            ).fetchone()[0]
            provisions = con.execute("SELECT COUNT(*) FROM provisions").fetchone()[0]
            last = con.execute(
                "SELECT started_at, finished_at, status, documents_saved, errors FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return {
                "documents": documents,
                "approved_documents": approved_documents,
                "pending_changes": pending_changes,
                "provisions": provisions,
                "last_sync": dict(last) if last else None,
                "database": str(self.db_path),
            }
