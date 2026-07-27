from __future__ import annotations

import fcntl
import hashlib
import json
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class RegulatoryStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.db_path.with_name(f"{self.db_path.name}.lock")

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        with self.lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _database_signature(self) -> tuple[tuple[int, int, int, int] | None, ...]:
        signatures: list[tuple[int, int, int, int] | None] = []
        for path in (self.db_path, Path(f"{self.db_path}-wal"), Path(f"{self.db_path}-shm")):
            try:
                current = path.stat()
                signatures.append(
                    (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                )
            except FileNotFoundError:
                signatures.append(None)
        return tuple(signatures)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    @staticmethod
    def _unique_index_columns(con: sqlite3.Connection, table_name: str) -> list[tuple[str, ...]]:
        result: list[tuple[str, ...]] = []
        for row in con.execute(f"PRAGMA index_list({table_name})"):
            if not row[2]:
                continue
            result.append(
                tuple(index_row[2] for index_row in con.execute(f"PRAGMA index_info({row[1]})"))
            )
        return result

    @staticmethod
    def _needs_schema_migration(con: sqlite3.Connection) -> bool:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('documents', 'change_events')"
            )
        }
        for table_name in tables:
            columns = {row[1] for row in con.execute(f"PRAGMA table_info({table_name})")}
            if "edition_key" not in columns:
                return True
            if table_name == "documents":
                unique_indexes = RegulatoryStore._unique_index_columns(con, "documents")
                expected = ("source_type", "official_id", "edition_key")
                legacy = ("source_type", "official_id", "version_id")
                if expected not in unique_indexes or legacy in unique_indexes:
                    return True
            if table_name == "change_events":
                column_info = {
                    row[1]: row for row in con.execute("PRAGMA table_info(change_events)")
                }
                indexes = {row[1] for row in con.execute("PRAGMA index_list(change_events)")}
                table_sql = con.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='change_events'"
                ).fetchone()[0]
                if (
                    "uq_change_events_pending" not in indexes
                    or "UNIQUE(source_type" in table_sql
                    or not column_info["candidate_sha256"][3]
                ):
                    return True
        return False

    def _backup_before_migration(self, con: sqlite3.Connection) -> None:
        backup_path = self.db_path.with_name(f"{self.db_path.name}.pre-v0.2.bak")
        with tempfile.NamedTemporaryFile(
            prefix=f".{backup_path.name}.",
            suffix=".tmp",
            dir=backup_path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            with sqlite3.connect(temporary_path) as backup:
                con.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("마이그레이션 백업 무결성 검사에 실패했습니다.")
            temporary_path.replace(backup_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _edition_key(document: dict[str, Any]) -> str:
        return f"{document['version_id']}@{document.get('effective_date') or 'undated'}"

    @staticmethod
    def _migrate_documents_edition_key(con: sqlite3.Connection) -> None:
        table = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()
        if not table:
            return
        columns = {row[1] for row in con.execute("PRAGMA table_info(documents)")}
        unique_indexes = RegulatoryStore._unique_index_columns(con, "documents")
        expected = ("source_type", "official_id", "edition_key")
        legacy = ("source_type", "official_id", "version_id")
        if "edition_key" in columns and expected in unique_indexes and legacy not in unique_indexes:
            return
        edition_expr = (
            "COALESCE(NULLIF(edition_key, ''), version_id || '@' || "
            "COALESCE(NULLIF(effective_date, ''), 'undated'))"
            if "edition_key" in columns
            else "version_id || '@' || COALESCE(NULLIF(effective_date, ''), 'undated')"
        )
        con.commit()
        con.execute("PRAGMA foreign_keys = OFF")
        try:
            con.executescript(
                f"""
                BEGIN;
                CREATE TABLE documents_new (
                    id INTEGER PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    official_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    edition_key TEXT NOT NULL,
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
                    UNIQUE(source_type, official_id, edition_key)
                );
                INSERT INTO documents_new (
                    id, source_type, official_id, version_id, edition_key, title, authority,
                    document_kind, promulgation_date, effective_date, effective_to, status,
                    official_url, payload_sha256, collected_at, approved, review_status, change_type
                )
                SELECT id, source_type, official_id, version_id,
                       {edition_expr},
                       title, authority, document_kind, promulgation_date,
                       NULLIF(effective_date, ''), effective_to, status, official_url, payload_sha256, collected_at,
                       approved, review_status, change_type
                FROM documents;
                DROP TABLE documents;
                ALTER TABLE documents_new RENAME TO documents;
                COMMIT;
                """
            )
        except Exception:
            con.rollback()
            raise
        finally:
            con.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _migrate_change_events_effective_date(con: sqlite3.Connection) -> None:
        table = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='change_events'"
        ).fetchone()
        if not table:
            return
        table_sql = table["sql"] or ""
        column_info = {row[1]: row for row in con.execute("PRAGMA table_info(change_events)")}
        columns = set(column_info)
        indexes = {row[1] for row in con.execute("PRAGMA index_list(change_events)")}
        if (
            "edition_key" in columns
            and "uq_change_events_pending" in indexes
            and "UNIQUE(source_type" not in table_sql
            and column_info["candidate_sha256"][3]
        ):
            return
        con.commit()
        con.execute("PRAGMA foreign_keys = OFF")
        try:
            effective_expr = (
                "CASE WHEN json_valid(candidate_payload) "
                "THEN json_extract(candidate_payload, '$.effective_date') END"
                if "effective_date" not in columns
                else "effective_date"
            )
            edition_expr = (
                f"version_id || '@' || COALESCE(NULLIF(({effective_expr}), ''), 'undated')"
            )

            def legacy_candidate_sha(value: Any) -> str:
                try:
                    payload = json.loads(str(value or "{}"))
                    if isinstance(payload, dict):
                        return RegulatoryStore._candidate_sha256(payload)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
                return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()

            con.create_function(
                "_sha256_text",
                1,
                legacy_candidate_sha,
                deterministic=True,
            )
            con.executescript(
                f"""
                BEGIN;
                CREATE TABLE change_events_new (
                    id INTEGER PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    official_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    edition_key TEXT NOT NULL,
                    effective_date TEXT,
                    title TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    previous_sha256 TEXT,
                    candidate_sha256 TEXT NOT NULL,
                    candidate_payload TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at TEXT
                );
                INSERT INTO change_events_new (
                    id, source_type, official_id, version_id, edition_key, effective_date, title,
                    change_type, previous_sha256, candidate_sha256, candidate_payload,
                    status, detected_at, reviewed_at
                )
                SELECT id, source_type, official_id, version_id, {edition_expr},
                       {effective_expr}, title, change_type, previous_sha256,
                       COALESCE(candidate_sha256, _sha256_text(candidate_payload)),
                       candidate_payload, status, detected_at, reviewed_at
                FROM change_events;
                UPDATE change_events_new
                SET status='superseded', reviewed_at=COALESCE(reviewed_at, CURRENT_TIMESTAMP)
                WHERE status='pending' AND id NOT IN (
                    SELECT MIN(id) FROM change_events_new
                    WHERE status='pending'
                    GROUP BY source_type, official_id, edition_key, candidate_sha256
                );
                DROP TABLE change_events;
                ALTER TABLE change_events_new RENAME TO change_events;
                CREATE UNIQUE INDEX uq_change_events_pending
                ON change_events(source_type, official_id, edition_key, candidate_sha256)
                WHERE status='pending';
                COMMIT;
                """
            )
        except Exception:
            con.rollback()
            raise
        finally:
            con.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _assert_foreign_key_integrity(con: sqlite3.Connection) -> None:
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            tables = sorted({str(row[0]) for row in violations})
            raise sqlite3.IntegrityError(
                f"외래키 무결성 위반 {len(violations)}건: {', '.join(tables)}"
            )

    @staticmethod
    def _ensure_fts_consistency(con: sqlite3.Connection) -> None:
        provision_count = con.execute("SELECT COUNT(*) FROM provisions").fetchone()[0]
        fts_count = con.execute("SELECT COUNT(*) FROM provisions_fts").fetchone()[0]
        mismatch = con.execute(
            """
            SELECT EXISTS(
                SELECT 1
                FROM provisions p
                JOIN documents d ON d.id=p.document_id
                LEFT JOIN provisions_fts f ON f.rowid=p.id
                WHERE f.rowid IS NULL OR f.title IS NOT d.title
                   OR f.provision_path IS NOT p.provision_path OR f.text IS NOT p.text
                UNION ALL
                SELECT 1
                FROM provisions_fts f
                LEFT JOIN provisions p ON p.id=f.rowid
                WHERE p.id IS NULL
            )
            """
        ).fetchone()[0]
        if provision_count == fts_count and not mismatch:
            return
        con.execute("DELETE FROM provisions_fts")
        con.execute(
            """
            INSERT INTO provisions_fts(rowid, title, provision_path, text)
            SELECT p.id, d.title, p.provision_path, p.text
            FROM provisions p
            JOIN documents d ON d.id=p.document_id
            """
        )

    @staticmethod
    def _is_fts_dump_statement(statement: str) -> bool:
        if statement.startswith("PRAGMA writable_schema="):
            return True
        if statement.startswith("INSERT INTO sqlite_master("):
            return "VALUES('table','provisions_fts','provisions_fts'," in statement
        return bool(
            re.match(
                r"^(?:CREATE TABLE|INSERT INTO)\s+[\"']provisions_fts"
                r"(?:_(?:config|content|data|docsize|idx))?[\"']",
                statement,
            )
        )

    def _publish_working_database(
        self,
        migration_path: Path,
        source_signature: tuple[tuple[int, int, int, int] | None, ...],
    ) -> None:
        with sqlite3.connect(migration_path) as migrated:
            dump_statements = [
                statement
                for statement in migrated.iterdump()
                if statement not in {"BEGIN TRANSACTION;", "COMMIT;"}
                and not self._is_fts_dump_statement(statement)
            ]
        with self._connect() as live:
            live.execute("PRAGMA foreign_keys=OFF")
            live.execute("BEGIN EXCLUSIVE")
            try:
                if self._database_signature() != source_signature:
                    raise sqlite3.OperationalError(
                        "마이그레이션 중 운영 DB가 변경되어 게시를 중단했습니다."
                    )
                for row in live.execute(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE type IN ('view', 'trigger') AND name NOT LIKE 'sqlite_%'"
                ).fetchall():
                    object_type = "VIEW" if row["type"] == "view" else "TRIGGER"
                    quoted_name = str(row["name"]).replace('"', '""')
                    live.execute(f'DROP {object_type} "{quoted_name}"')
                table_rows = live.execute("PRAGMA table_list").fetchall()
                root_tables = [
                    row
                    for row in table_rows
                    if row[0] == "main"
                    and row[2] in {"table", "virtual"}
                    and not str(row[1]).startswith("sqlite_")
                ]
                root_tables.sort(key=lambda row: 0 if row[2] == "virtual" else 1)
                for row in root_tables:
                    quoted_name = str(row[1]).replace('"', '""')
                    live.execute(f'DROP TABLE "{quoted_name}"')
                for statement in dump_statements:
                    live.execute(statement)
                live.execute(
                    "CREATE VIRTUAL TABLE provisions_fts USING fts5("
                    "title, provision_path, text, tokenize='unicode61')"
                )
                self._ensure_fts_consistency(live)
                if live.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("운영 DB 게시본 무결성 검사에 실패했습니다.")
                live.commit()
            except Exception:
                live.rollback()
                raise
            finally:
                live.execute("PRAGMA foreign_keys=ON")
        with self._connect() as verified:
            self._assert_foreign_key_integrity(verified)

    def initialize(self) -> None:
        with self._write_lock():
            self._initialize_locked()

    def _initialize_locked(self) -> None:
        migration_path: Path | None = None
        source_signature: tuple[tuple[int, int, int, int] | None, ...] | None = None
        needs_migration = False
        with self._connect() as source:
            self._assert_foreign_key_integrity(source)
            needs_migration = self._needs_schema_migration(source)
            if needs_migration:
                source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                journal_mode = source.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                if str(journal_mode).casefold() != "delete":
                    raise sqlite3.OperationalError(
                        "마이그레이션 전에 WAL 모드를 종료하지 못했습니다."
                    )
                source_signature = self._database_signature()
                self._backup_before_migration(source)
                try:
                    with tempfile.NamedTemporaryFile(
                        prefix=f".{self.db_path.name}.migration-",
                        suffix=".db",
                        dir=self.db_path.parent,
                        delete=False,
                    ) as temporary:
                        migration_path = Path(temporary.name)
                    with sqlite3.connect(migration_path) as working:
                        source.backup(working)
                    if self._database_signature() != source_signature:
                        raise sqlite3.OperationalError(
                            "마이그레이션 복사 중 운영 DB가 변경되어 작업을 중단했습니다."
                        )
                except Exception:
                    if migration_path is not None:
                        migration_path.unlink(missing_ok=True)
                    raise

        if not needs_migration:
            self._initialize_in_place()
            return

        assert source_signature is not None
        assert migration_path is not None
        working_store = RegulatoryStore(migration_path)
        working_backup = migration_path.with_name(f"{migration_path.name}.pre-v0.2.bak")
        try:
            working_store._initialize_in_place()
            with working_store._connect() as migrated:
                if migrated.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("마이그레이션 작업본 무결성 검사에 실패했습니다.")
                self._assert_foreign_key_integrity(migrated)
            self._publish_working_database(migration_path, source_signature)
        finally:
            migration_path.unlink(missing_ok=True)
            working_backup.unlink(missing_ok=True)

    def _initialize_in_place(self) -> None:
        with self._connect() as con:
            self._assert_foreign_key_integrity(con)
            if self._needs_schema_migration(con):
                self._backup_before_migration(con)
            self._migrate_documents_edition_key(con)
            self._migrate_change_events_effective_date(con)
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    official_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    edition_key TEXT NOT NULL,
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
                    UNIQUE(source_type, official_id, edition_key)
                );
                CREATE TABLE IF NOT EXISTS provisions (
                    id INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    provision_path TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    effective_date TEXT
                );
                CREATE TABLE IF NOT EXISTS annexes (
                    id INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    annex_key TEXT,
                    provision_path TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT,
                    text TEXT NOT NULL,
                    effective_date TEXT,
                    file_links TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    name TEXT,
                    url TEXT NOT NULL
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
                    edition_key TEXT NOT NULL,
                    effective_date TEXT,
                    title TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    previous_sha256 TEXT,
                    candidate_sha256 TEXT NOT NULL,
                    candidate_payload TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_change_events_pending
                ON change_events(source_type, official_id, edition_key, candidate_sha256)
                WHERE status='pending';
                CREATE TABLE IF NOT EXISTS review_events (
                    id INTEGER PRIMARY KEY,
                    change_event_id INTEGER REFERENCES change_events(id),
                    source_type TEXT NOT NULL,
                    official_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    effective_date TEXT,
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
            for table_name in ("change_events", "review_events"):
                event_columns = {row[1] for row in con.execute(f"PRAGMA table_info({table_name})")}
                if "effective_date" not in event_columns:
                    con.execute(f"ALTER TABLE {table_name} ADD COLUMN effective_date TEXT")
            con.execute("UPDATE documents SET effective_date=NULL WHERE effective_date=''")
            con.execute("UPDATE change_events SET effective_date=NULL WHERE effective_date=''")
            con.execute("UPDATE review_events SET effective_date=NULL WHERE effective_date=''")
            con.execute(
                """
                UPDATE review_events
                SET effective_date = (
                    SELECT effective_date FROM change_events
                    WHERE change_events.id = review_events.change_event_id
                )
                WHERE effective_date IS NULL AND change_event_id IS NOT NULL
                """
            )
            # API 인증값이 포함된 과거 DRF HTML URL을 공개용 버전 고정 URL로 정리한다.
            con.execute(
                """
                UPDATE documents
                SET official_url='https://www.law.go.kr/lsInfoP.do?lsiSeq=' || version_id
                WHERE source_type='law'
                """
            )
            con.execute(
                """
                UPDATE documents
                SET official_url='https://www.law.go.kr/admRulInfoP.do?admRulSeq=' || version_id
                WHERE source_type='admrul'
                """
            )
            self._ensure_fts_consistency(con)
            self._assert_foreign_key_integrity(con)

    @staticmethod
    def _event_payload(document: dict[str, Any]) -> str:
        return json.dumps(document, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _candidate_sha256(document: dict[str, Any]) -> str:
        identity = {
            key: value
            for key, value in document.items()
            if key not in {"_sync_run_id", "collected_at", "raw_path", "payload_sha256"}
        }
        payload = json.dumps(identity, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _record_change(
        self,
        con: sqlite3.Connection,
        document: dict[str, Any],
        *,
        change_type: str,
        previous_sha256: str | None,
    ) -> None:
        edition_key = self._edition_key(document)
        candidate_payload = self._event_payload(document)
        candidate_sha256 = document.get("payload_sha256") or self._candidate_sha256(document)
        previous_rows = con.execute(
            """
            SELECT status, candidate_payload FROM change_events
            WHERE source_type=? AND official_id=? AND edition_key=?
              AND candidate_sha256=?
            """,
            (
                document["source_type"],
                document["official_id"],
                edition_key,
                candidate_sha256,
            ),
        ).fetchall()
        previous_statuses = {row["status"] for row in previous_rows}
        if "rejected" in previous_statuses:
            return
        if "superseded" in previous_statuses and "approved" not in previous_statuses:
            current_run_id = document.get("_sync_run_id")
            previous_run_ids = set()
            for row in previous_rows:
                try:
                    previous_run_ids.add(
                        json.loads(row["candidate_payload"] or "{}").get("_sync_run_id")
                    )
                except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                    previous_run_ids.add(None)
            if current_run_id is None or current_run_id in previous_run_ids:
                return
        con.execute(
            """
            INSERT OR IGNORE INTO change_events (
                source_type, official_id, version_id, edition_key, effective_date,
                title, change_type, previous_sha256, candidate_sha256, candidate_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document["source_type"],
                document["official_id"],
                document["version_id"],
                edition_key,
                document.get("effective_date") or None,
                document["title"],
                change_type,
                previous_sha256,
                candidate_sha256,
                candidate_payload,
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
    def _replace_annexes_and_attachments(
        con: sqlite3.Connection, document_id: int, document: dict[str, Any]
    ) -> None:
        con.execute("DELETE FROM annexes WHERE document_id=?", (document_id,))
        con.execute("DELETE FROM attachments WHERE document_id=?", (document_id,))
        for annex in document.get("annexes", []):
            con.execute(
                """
                INSERT INTO annexes (
                    document_id, annex_key, provision_path, kind, title, text,
                    effective_date, file_links
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    annex.get("annex_key"),
                    annex["provision_path"],
                    annex.get("kind") or "별표",
                    annex.get("title"),
                    annex.get("text") or "",
                    annex.get("effective_date") or document.get("effective_date"),
                    json.dumps(annex.get("file_links", []), ensure_ascii=False),
                ),
            )
        for attachment in document.get("attachments", []):
            if not attachment.get("url"):
                continue
            con.execute(
                "INSERT INTO attachments (document_id, name, url) VALUES (?, ?, ?)",
                (document_id, attachment.get("name"), attachment["url"]),
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
                ORDER BY COALESCE(effective_date, '0000-00-00'),
                         COALESCE(promulgation_date, '0000-00-00'),
                         CASE WHEN version_id GLOB '[0-9]*' THEN CAST(version_id AS INTEGER) ELSE 0 END,
                         version_id, id
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
        edition_key = self._edition_key(document)
        existing = con.execute(
            "SELECT id FROM documents WHERE source_type=? AND official_id=? AND edition_key=?",
            (document["source_type"], document["official_id"], edition_key),
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
                    source_type, official_id, version_id, edition_key, title, authority, document_kind,
                    promulgation_date, effective_date, status, official_url, payload_sha256,
                    approved, review_status, change_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document["source_type"],
                    document["official_id"],
                    document["version_id"],
                    edition_key,
                    *values,
                ),
            )
            document_id = int(cur.lastrowid)
        self._replace_provisions(con, document_id, document)
        self._replace_annexes_and_attachments(con, document_id, document)
        self._recompute_effective_intervals(con, document["source_type"], document["official_id"])
        return document_id

    def upsert_document(self, document: dict[str, Any]) -> int:
        with self._write_lock():
            return self._upsert_document_locked(document)

    def _upsert_document_locked(self, document: dict[str, Any]) -> int:
        document = dict(document)
        document["effective_date"] = document.get("effective_date") or None
        if not document.get("payload_sha256"):
            document["payload_sha256"] = self._candidate_sha256(document)
        with self._connect() as con:
            edition_key = self._edition_key(document)
            old = con.execute(
                """
                SELECT id, payload_sha256, approved, review_status, change_type
                FROM documents
                WHERE source_type=? AND official_id=? AND edition_key=?
                """,
                (document["source_type"], document["official_id"], edition_key),
            ).fetchone()
            if old and old["payload_sha256"] == document.get("payload_sha256"):
                document_id = self._write_document(con, document, approved=int(old["approved"]))
                if not old["approved"]:
                    con.execute(
                        "UPDATE documents SET review_status=?, change_type=? WHERE id=?",
                        (old["review_status"], old["change_type"], document_id),
                    )
                return document_id
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
            sql += """ AND d.effective_date IS NOT NULL AND d.effective_date <= ?
                AND (p.effective_date IS NULL OR p.effective_date <= ?)
                AND (d.effective_to IS NULL OR ? < d.effective_to)"""
            params.extend([as_of, as_of, as_of])
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
                text_where += """ AND d.effective_date IS NOT NULL AND d.effective_date <= ?
                    AND (p.effective_date IS NULL OR p.effective_date <= ?)
                    AND (d.effective_to IS NULL OR ? < d.effective_to)"""
                text_filters.extend([as_of, as_of, as_of])
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
                title_where += """ AND d.effective_date IS NOT NULL AND d.effective_date <= ?
                    AND (p.effective_date IS NULL OR p.effective_date <= ?)
                    AND (d.effective_to IS NULL OR ? < d.effective_to)"""
                title_filters.extend([as_of, as_of, as_of])
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
            sql += """ AND effective_date IS NOT NULL AND effective_date <= ?
                AND (effective_to IS NULL OR ? < effective_to)"""
            params.extend([as_of, as_of])
        sql += """ ORDER BY effective_date DESC,
                     COALESCE(promulgation_date, '0000-00-00') DESC,
                     CASE WHEN version_id GLOB '[0-9]*' THEN CAST(version_id AS INTEGER) ELSE 0 END DESC,
                     version_id DESC, id DESC LIMIT 1"""
        with self._connect() as con:
            doc = con.execute(sql, params).fetchone()
            if not doc:
                return None
            result = dict(doc)
            provision_sql = (
                "SELECT provision_path, kind, text, effective_date "
                "FROM provisions WHERE document_id=?"
            )
            provision_params: list[Any] = [doc["id"]]
            if as_of:
                provision_sql += " AND (effective_date IS NULL OR effective_date<=?)"
                provision_params.append(as_of)
            provision_sql += " ORDER BY id"
            result["provisions"] = [
                dict(row) for row in con.execute(provision_sql, provision_params)
            ]
            result["annexes"] = []
            annex_sql = """
                SELECT annex_key, provision_path, kind, title, text, effective_date, file_links
                FROM annexes WHERE document_id=?
            """
            annex_params: list[Any] = [doc["id"]]
            if as_of:
                annex_sql += " AND (effective_date IS NULL OR effective_date<=?)"
                annex_params.append(as_of)
            annex_sql += " ORDER BY id"
            for row in con.execute(annex_sql, annex_params):
                annex = dict(row)
                annex["file_links"] = json.loads(annex["file_links"] or "[]")
                result["annexes"].append(annex)
            result["attachments"] = [
                dict(row)
                for row in con.execute(
                    "SELECT name, url FROM attachments WHERE document_id=? ORDER BY id",
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
            sql += """ AND d.effective_date IS NOT NULL AND d.effective_date <= ?
                AND (p.effective_date IS NULL OR p.effective_date <= ?)
                AND (d.effective_to IS NULL OR ? < d.effective_to)"""
            params.extend([as_of, as_of, as_of])
        sql += " ORDER BY d.effective_date DESC, p.id"
        with self._connect() as con:
            return [dict(row) for row in con.execute(sql, params)]

    def list_pending_changes(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            return [
                dict(row)
                for row in con.execute(
                    """
                    SELECT id AS change_event_id, source_type, official_id, version_id,
                           edition_key, effective_date, title, change_type, previous_sha256,
                           candidate_sha256, status AS review_status, detected_at
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
        effective_date: str | None = None,
        change_event_id: int | None = None,
        decision: str,
        reviewer: str,
        reason: str,
    ) -> None:
        with self._write_lock():
            self._review_version_locked(
                source_type,
                official_id,
                version_id,
                effective_date=effective_date,
                change_event_id=change_event_id,
                decision=decision,
                reviewer=reviewer,
                reason=reason,
            )

    def _review_version_locked(
        self,
        source_type: str,
        official_id: str,
        version_id: str,
        *,
        effective_date: str | None = None,
        change_event_id: int | None = None,
        decision: str,
        reviewer: str,
        reason: str,
    ) -> None:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision은 approved 또는 rejected여야 합니다.")
        if not reviewer.strip() or not reason.strip():
            raise ValueError("reviewer와 reason은 필수입니다.")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            event_sql = """
                SELECT * FROM change_events
                WHERE source_type=? AND official_id=? AND version_id=? AND status='pending'
            """
            event_params: list[Any] = [source_type, official_id, version_id]
            if change_event_id is not None:
                event_sql += " AND id=?"
                event_params.append(change_event_id)
            elif effective_date is not None:
                event_sql += " AND effective_date=?"
                event_params.append(effective_date)
            event_sql += " ORDER BY id DESC LIMIT 2"
            events = list(con.execute(event_sql, event_params))
            if not events:
                raise ValueError("검토 대기 중인 변경을 찾을 수 없습니다.")
            if len(events) > 1:
                dates = {event["effective_date"] for event in events}
                if effective_date is None and change_event_id is None and len(dates) > 1:
                    raise ValueError(
                        "동일 시행본 ID에 여러 시행일이 있습니다. effective_date를 지정하세요."
                    )
                raise ValueError(
                    "동일 시행본에 검토 대기 후보가 여러 개입니다. change_event_id를 지정하세요."
                )
            event = events[0]
            claimed = con.execute(
                "UPDATE change_events SET status='reviewing' WHERE id=? AND status='pending'",
                (event["id"],),
            )
            if claimed.rowcount != 1:
                raise ValueError("검토 대기 이벤트를 원자적으로 선택하지 못했습니다.")
            if decision == "approved":
                if event["change_type"] == "content_changed":
                    document = json.loads(event["candidate_payload"])
                    self._write_document(con, document, approved=1)
                else:
                    approved_update = con.execute(
                        """
                        UPDATE documents
                        SET approved=1, review_status='approved', change_type=NULL
                        WHERE source_type=? AND official_id=? AND version_id=?
                          AND effective_date IS ?
                        """,
                        (source_type, official_id, version_id, event["effective_date"]),
                    )
                    if approved_update.rowcount != 1:
                        raise ValueError("검토 대상 문서 승인에 실패했습니다.")
                    self._recompute_effective_intervals(con, source_type, official_id)
                con.execute(
                    """
                    UPDATE change_events
                    SET status='superseded', reviewed_at=CURRENT_TIMESTAMP
                    WHERE source_type=? AND official_id=? AND edition_key=?
                      AND status='pending' AND id<>?
                    """,
                    (source_type, official_id, event["edition_key"], event["id"]),
                )
            else:
                con.execute(
                    """
                    UPDATE documents SET review_status='rejected'
                    WHERE source_type=? AND official_id=? AND version_id=?
                      AND effective_date IS ? AND approved=0
                    """,
                    (source_type, official_id, version_id, event["effective_date"]),
                )
            finalized = con.execute(
                """
                UPDATE change_events SET status=?, reviewed_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='reviewing'
                """,
                (decision, event["id"]),
            )
            if finalized.rowcount != 1:
                raise ValueError("검토 이벤트 상태 확정에 실패했습니다.")
            con.execute(
                """
                INSERT INTO review_events (
                    change_event_id, source_type, official_id, version_id, effective_date,
                    decision, reviewer, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["id"],
                    source_type,
                    official_id,
                    version_id,
                    event["effective_date"],
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
        with self._write_lock():
            return self._start_sync_run_locked(started_at)

    def _start_sync_run_locked(self, started_at: str) -> int:
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
        with self._write_lock():
            self._finish_sync_run_locked(
                run_id,
                finished_at=finished_at,
                status=status,
                documents_seen=documents_seen,
                documents_saved=documents_saved,
                errors=errors,
            )

    def _finish_sync_run_locked(
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
