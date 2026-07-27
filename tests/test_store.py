from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from fire_mcp.store import RegulatoryStore


def sample_document(version_id: str = "V1", effective_date: str = "2025-01-01") -> dict:
    return {
        "source_type": "admrul",
        "official_id": "A1",
        "version_id": version_id,
        "title": "스프링클러설비의 화재안전성능기준(NFPC 103)",
        "authority": "소방청",
        "document_kind": "고시",
        "promulgation_date": "2024-12-01",
        "effective_date": effective_date,
        "status": "현행",
        "official_url": "https://www.law.go.kr/example",
        "payload_sha256": "abc",
        "approved": 1,
        "provisions": [
            {
                "provision_path": "제3조",
                "kind": "article",
                "text": "스프링클러설비는 기준에 따라 설치해야 한다.",
                "effective_date": effective_date,
            },
            {
                "provision_path": "제4조",
                "kind": "article",
                "text": "다만, 특정 조건에서는 적용하지 않는다.",
                "effective_date": effective_date,
            },
        ],
    }


def test_store_search_returns_official_source_and_exact_provision(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    store.upsert_document(sample_document())

    results = store.search("스프링클러설비", as_of="2025-02-01", limit=5)

    assert len(results) == 1
    assert results[0]["provision_path"] == "제3조"
    assert results[0]["official_url"].startswith("https://www.law.go.kr")
    assert results[0]["effective_date"] == "2025-01-01"


def test_initialize_repairs_null_fts_content(tmp_path):
    db_path = tmp_path / "regulations.db"
    store = RegulatoryStore(db_path)
    store.initialize()
    store.upsert_document(sample_document())
    with sqlite3.connect(db_path) as con:
        con.execute(
            "UPDATE provisions_fts SET text=NULL WHERE rowid=(SELECT MIN(rowid) FROM provisions_fts)"
        )
    store.initialize()
    assert store.search("스프링클러설비", as_of="2025-02-01", limit=5)


def test_store_returns_annex_and_attachment_metadata_and_searches_annex_text(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    document = sample_document()
    document["annexes"] = [
        {
            "annex_key": "000100",
            "provision_path": "별표 1",
            "kind": "별표",
            "title": "설치대상표",
            "text": "바닥면적 600제곱미터 이상",
            "effective_date": "2025-01-01",
            "file_links": [
                {
                    "kind": "pdf",
                    "name": "별표1.pdf",
                    "url": "https://www.law.go.kr/flDownload.do?flSeq=1",
                }
            ],
        }
    ]
    document["attachments"] = [
        {"name": "개정전문.pdf", "url": "https://www.law.go.kr/flDownload.do?flSeq=2"}
    ]
    document["provisions"].append(
        {
            "provision_path": "별표 1",
            "kind": "annex",
            "text": "설치대상표 바닥면적 600제곱미터 이상",
            "effective_date": "2025-01-01",
        }
    )

    store.upsert_document(document)

    result = store.get_document("A1", as_of="2025-02-01")
    assert result is not None
    assert result["annexes"][0]["provision_path"] == "별표 1"
    assert result["annexes"][0]["file_links"][0]["kind"] == "pdf"
    assert result["attachments"][0]["name"] == "개정전문.pdf"
    assert store.search("600제곱미터", as_of="2025-02-01")[0]["kind"] == "annex"


def test_store_excludes_future_version_from_as_of_query(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    store.upsert_document(sample_document(effective_date="2026-01-01"))

    assert store.search("스프링클러설비", as_of="2025-02-01") == []


def test_store_trace_exception_path_only_returns_exception_candidates(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    store.upsert_document(sample_document())

    results = store.trace_exception_path("A1", as_of="2025-02-01")

    assert len(results) == 1
    assert results[0]["provision_path"] == "제4조"
    assert "다만" in results[0]["text"]


def test_store_replaces_same_version_without_duplicate_provisions(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    store.upsert_document(sample_document())
    store.upsert_document(sample_document())

    status = store.status()

    assert status["documents"] == 1
    assert status["provisions"] == 2


def test_new_unreviewed_version_is_pending_and_not_served(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    store.upsert_document(sample_document())
    pending = sample_document(version_id="V2", effective_date="2026-01-01")
    pending.pop("approved")
    pending["payload_sha256"] = "changed"

    store.upsert_document(pending)

    assert store.get_document("A1", as_of="2026-02-01")["version_id"] == "V1"
    changes = store.list_pending_changes()
    assert len(changes) == 1
    assert changes[0]["version_id"] == "V2"
    assert changes[0]["review_status"] == "pending"


def test_approved_versions_use_closed_effective_intervals(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    store.upsert_document(sample_document(version_id="V1", effective_date="2024-01-01"))
    store.upsert_document(sample_document(version_id="V2", effective_date="2025-01-01"))

    old = store.get_document("A1", as_of="2024-06-01")
    current = store.get_document("A1", as_of="2025-06-01")

    assert old["version_id"] == "V1"
    assert old["effective_to"] == "2025-01-01"
    assert current["version_id"] == "V2"
    assert current["effective_to"] is None


def test_same_mst_with_different_effective_dates_are_distinct_editions(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    older = sample_document(version_id="SAME", effective_date="2022-12-01")
    newer = sample_document(version_id="SAME", effective_date="2024-12-01")
    newer["payload_sha256"] = "newer"

    store.upsert_document(older)
    store.upsert_document(newer)

    assert store.get_document("A1", as_of="2023-01-01")["effective_date"] == "2022-12-01"
    assert store.get_document("A1", as_of="2025-01-01")["effective_date"] == "2024-12-01"


def test_future_document_and_provisions_do_not_leak_into_as_of_queries(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    future = sample_document(version_id="FUTURE", effective_date="2026-01-01")
    future["provisions"][0]["effective_date"] = "2025-01-01"
    future["provisions"][0]["text"] = "다만 미래 문서 예외"
    store.upsert_document(future)

    assert store.search("미래 문서", as_of="2025-06-01") == []
    assert store.trace_exception_path("A1", as_of="2025-06-01") == []
    assert store.get_document("A1", as_of="2025-06-01") is None

    staged = sample_document(version_id="STAGED", effective_date="2025-01-01")
    staged["official_id"] = "A2"
    staged["provisions"][1]["effective_date"] = "2026-01-01"
    store.upsert_document(staged)
    as_of_document = store.get_document("A2", as_of="2025-06-01")
    assert as_of_document is not None
    assert all(row["effective_date"] != "2026-01-01" for row in as_of_document["provisions"])


def test_same_effective_date_prefers_latest_promulgation_not_insertion_order(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    latest = sample_document(version_id="V2", effective_date="2022-12-01")
    latest["promulgation_date"] = "2021-12-28"
    latest["payload_sha256"] = "latest"
    older = sample_document(version_id="V1", effective_date="2022-12-01")
    older["promulgation_date"] = "2021-11-30"
    older["payload_sha256"] = "older"

    store.upsert_document(latest)
    store.upsert_document(older)

    assert store.get_document("A1", as_of="2022-12-01")["version_id"] == "V2"


def test_review_requires_effective_date_when_mst_has_staged_editions(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    older = sample_document(version_id="SAME", effective_date="2022-12-01")
    newer = sample_document(version_id="SAME", effective_date="2024-12-01")
    older.pop("approved")
    newer.pop("approved")
    newer["payload_sha256"] = "newer"
    store.upsert_document(older)
    store.upsert_document(newer)

    with pytest.raises(ValueError, match="effective_date"):
        store.review_version(
            "admrul", "A1", "SAME", decision="approved", reviewer="tester", reason="검증"
        )

    store.review_version(
        "admrul",
        "A1",
        "SAME",
        effective_date="2022-12-01",
        decision="approved",
        reviewer="tester",
        reason="과거 시행본 검증",
    )
    assert {row["effective_date"] for row in store.list_pending_changes()} == {"2024-12-01"}


def test_review_can_select_one_of_multiple_candidates_by_change_event_id(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    original = sample_document(version_id="SAME", effective_date="2024-12-01")
    original.pop("approved")
    changed = sample_document(version_id="SAME", effective_date="2024-12-01")
    changed.pop("approved")
    changed["payload_sha256"] = "changed"
    changed["provisions"][0]["text"] = "변경 후보 본문"
    original["_sync_run_id"] = 1
    changed["_sync_run_id"] = 1
    store.upsert_document(original)
    store.upsert_document(changed)
    candidates = store.list_pending_changes()

    with pytest.raises(ValueError, match="change_event_id"):
        store.review_version(
            "admrul",
            "A1",
            "SAME",
            effective_date="2024-12-01",
            decision="approved",
            reviewer="tester",
            reason="후보 선택 필요",
        )

    selected = next(row for row in candidates if row["candidate_sha256"] == "changed")
    store.review_version(
        "admrul",
        "A1",
        "SAME",
        change_event_id=selected["change_event_id"],
        decision="approved",
        reviewer="tester",
        reason="변경 후보 원문 확인",
    )

    assert store.list_pending_changes() == []
    document = store.get_document("A1", as_of="2024-12-01")
    assert document["payload_sha256"] == "changed"
    assert document["provisions"][0]["text"] == "변경 후보 본문"

    store.upsert_document(original)
    assert store.list_pending_changes() == []
    original["_sync_run_id"] = 2
    store.upsert_document(original)
    reopened = store.list_pending_changes()
    assert len(reopened) == 1
    assert reopened[0]["candidate_sha256"] == original["payload_sha256"]


def test_competing_candidates_cannot_both_be_approved_concurrently(tmp_path, monkeypatch):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    original = sample_document(version_id="SAME", effective_date="2024-12-01")
    original.pop("approved")
    changed = sample_document(version_id="SAME", effective_date="2024-12-01")
    changed.pop("approved")
    changed["payload_sha256"] = "changed"
    changed["provisions"][0]["text"] = "경쟁 후보 본문"
    store.upsert_document(original)
    store.upsert_document(changed)
    event_ids = [row["change_event_id"] for row in store.list_pending_changes()]
    select_barrier = threading.Barrier(2)

    class BarrierConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if "SELECT * FROM change_events" in sql and not self.in_transaction:
                select_barrier.wait(timeout=5)
            return cursor

    def connect_with_barrier():
        con = sqlite3.connect(
            store.db_path,
            timeout=5,
            factory=BarrierConnection,
        )
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    monkeypatch.setattr(store, "_connect", connect_with_barrier)

    def approve(event_id):
        try:
            store.review_version(
                "admrul",
                "A1",
                "SAME",
                change_event_id=event_id,
                decision="approved",
                reviewer="concurrent-reviewer",
                reason="동시 승인 직렬화 검증",
            )
        except Exception as exc:
            return type(exc), str(exc)
        return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(approve, event_ids))

    assert results.count(None) == 1
    failures = [result for result in results if result is not None]
    assert len(failures) == 1
    assert failures[0][0] is ValueError
    with sqlite3.connect(store.db_path) as con:
        statuses = [
            row[0]
            for row in con.execute(
                "SELECT status FROM change_events WHERE id IN (?, ?) ORDER BY id",
                event_ids,
            )
        ]
        review_count = con.execute("SELECT COUNT(*) FROM review_events").fetchone()[0]
    assert sorted(statuses) == ["approved", "superseded"]
    assert review_count == 1


def test_undated_new_version_can_be_approved_consistently(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    document = sample_document(version_id="UNDATED-APPROVAL", effective_date="")
    document.pop("approved")
    store.upsert_document(document)
    event = store.list_pending_changes()[0]
    store.review_version(
        "admrul",
        "A1",
        "UNDATED-APPROVAL",
        change_event_id=event["change_event_id"],
        decision="approved",
        reviewer="tester",
        reason="시행일 미상 자료 검토",
    )
    with sqlite3.connect(store.db_path) as con:
        row = con.execute(
            "SELECT effective_date, approved, review_status FROM documents WHERE version_id=?",
            ("UNDATED-APPROVAL",),
        ).fetchone()
    assert row == (None, 1, "approved")
    assert store.get_document("A1") is not None
    assert store.get_document("A1", as_of="2025-01-01") is None


def test_change_events_use_non_null_edition_key_for_undated_documents(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    document = sample_document(version_id="UNDATED", effective_date="")
    document.pop("approved")
    store.upsert_document(document)

    with sqlite3.connect(tmp_path / "regulations.db") as con:
        row = con.execute(
            "SELECT edition_key, effective_date FROM change_events WHERE version_id='UNDATED'"
        ).fetchone()
        index_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_change_events_pending'"
        ).fetchone()[0]
    assert row == ("UNDATED@undated", None)
    assert "edition_key" in index_sql
    assert "WHERE status='pending'" in index_sql


def test_initialize_migrates_legacy_document_key_without_losing_children(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE documents (
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
            CREATE TABLE provisions (
                id INTEGER PRIMARY KEY,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                provision_path TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                effective_date TEXT
            );
            CREATE TABLE change_events (
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
            CREATE TABLE review_events (
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
            INSERT INTO documents (
                id, source_type, official_id, version_id, title, effective_date,
                official_url, approved, review_status
            ) VALUES (7, 'law', 'L1', 'MST1', 'provisions_fts 보존 검증', '2022-12-01',
                      'https://www.law.go.kr/example', 1, 'approved');
            INSERT INTO documents (
                id, source_type, official_id, version_id, title, effective_date,
                official_url, approved, review_status
            ) VALUES (8, 'law', 'L2', 'EMPTY', '시행일미상법', '',
                      'https://www.law.go.kr/undated', 1, 'approved');
            INSERT INTO provisions (document_id, provision_path, kind, text)
            VALUES (7, '제1조', 'article', '보존할 조문');
            INSERT INTO change_events (
                id, source_type, official_id, version_id, title, change_type,
                candidate_sha256, candidate_payload, status
            ) VALUES (
                11, 'law', 'L1', 'MST1', '레거시법', 'new_version', 'sha',
                '{"effective_date":"2022-12-01"}', 'approved'
            );
            INSERT INTO change_events (
                id, source_type, official_id, version_id, title, change_type,
                candidate_sha256, candidate_payload, status
            ) VALUES (
                12, 'law', 'L1', 'MST1', '레거시법', 'content_changed', NULL,
                '{"effective_date":"2022-12-01","text":"동일 후보"}', 'pending'
            );
            INSERT INTO change_events (
                id, source_type, official_id, version_id, title, change_type,
                candidate_sha256, candidate_payload, status
            ) VALUES (
                14, 'law', 'L1', 'MST1', '레거시법', 'content_changed', NULL,
                '{"effective_date":"2022-12-01","text":"동일 후보"}', 'pending'
            );
            INSERT INTO review_events (
                id, change_event_id, source_type, official_id, version_id,
                decision, reviewer, reason
            ) VALUES (13, 11, 'law', 'L1', 'MST1', 'approved', 'tester', '레거시 검토');
            """
        )

    backup_path = db_path.with_name(f"{db_path.name}.pre-v0.2.bak")
    with sqlite3.connect(backup_path) as unrelated_backup:
        unrelated_backup.execute("CREATE TABLE unrelated(value TEXT)")
        unrelated_backup.execute("INSERT INTO unrelated VALUES ('다른 내용')")
    store = RegulatoryStore(db_path)
    inode_before = db_path.stat().st_ino
    preopened_writer = sqlite3.connect(db_path)
    store.initialize()
    assert db_path.stat().st_ino == inode_before
    preopened_writer.execute("UPDATE documents SET authority='대기 writer' WHERE id=7")
    preopened_writer.commit()
    preopened_writer.close()

    assert backup_path.exists()
    assert backup_path.stat().st_size > 0
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2

    with sqlite3.connect(db_path) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(documents)")}
        assert (
            con.execute("SELECT authority FROM documents WHERE id=7").fetchone()[0] == "대기 writer"
        )
        assert (
            con.execute("SELECT title FROM documents WHERE id=7").fetchone()[0]
            == "provisions_fts 보존 검증"
        )
        child = con.execute("SELECT document_id, text FROM provisions").fetchone()
        change = con.execute(
            "SELECT id, edition_key, effective_date, status FROM change_events WHERE id=11"
        ).fetchone()
        review = con.execute(
            "SELECT change_event_id, effective_date FROM review_events WHERE id=13"
        ).fetchone()
        pending_hashes = con.execute(
            "SELECT candidate_sha256 FROM change_events WHERE status='pending'"
        ).fetchall()
        duplicate_statuses = con.execute(
            "SELECT status FROM change_events WHERE id IN (12, 14) ORDER BY id"
        ).fetchall()
        undated = con.execute(
            "SELECT edition_key, effective_date FROM documents WHERE id=8"
        ).fetchone()
        fts_count = con.execute("SELECT COUNT(*) FROM provisions_fts").fetchone()[0]
        fts_match = con.execute(
            "SELECT text FROM provisions_fts WHERE provisions_fts MATCH '보존할'"
        ).fetchone()
        integrity = con.execute("PRAGMA foreign_key_check").fetchall()
    assert "edition_key" in columns
    assert child == (7, "보존할 조문")
    assert change == (11, "MST1@2022-12-01", "2022-12-01", "approved")
    assert review == (11, "2022-12-01")
    assert len(pending_hashes) == 1
    assert len(pending_hashes[0][0]) == 64
    assert sorted(row[0] for row in duplicate_statuses) == ["pending", "superseded"]
    assert undated == ("EMPTY@undated", None)
    assert fts_count == 1
    assert fts_match == ("보존할 조문",)
    assert integrity == []
    assert store.get_document("L1", as_of="2023-01-01")["edition_key"] == "MST1@2022-12-01"

    store.upsert_document(
        {
            "source_type": "law",
            "official_id": "L2",
            "version_id": "EMPTY",
            "title": "시행일미상법",
            "effective_date": None,
            "official_url": "https://www.law.go.kr/undated",
            "payload_sha256": None,
            "approved": 1,
            "provisions": [],
        }
    )
    with sqlite3.connect(db_path) as con:
        assert (
            con.execute("SELECT COUNT(*) FROM documents WHERE official_id='L2'").fetchone()[0] == 1
        )


def test_initialize_rebuilds_partial_document_migration_with_legacy_unique_key(tmp_path):
    db_path = tmp_path / "partial.db"
    store = RegulatoryStore(db_path)
    store.initialize()
    with sqlite3.connect(db_path) as con:
        table_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()[0]
        bad_sql = table_sql.replace(
            "UNIQUE(source_type, official_id, edition_key)",
            "UNIQUE(source_type, official_id, edition_key), "
            "UNIQUE(source_type, official_id, version_id)",
        ).replace("CREATE TABLE documents", "CREATE TABLE documents_bad", 1)
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute(bad_sql)
        con.execute("DROP TABLE documents")
        con.execute("ALTER TABLE documents_bad RENAME TO documents")

    store.initialize()
    with sqlite3.connect(db_path) as con:
        unique_columns = []
        for index in con.execute("PRAGMA index_list(documents)"):
            if index[2]:
                unique_columns.append(
                    tuple(row[2] for row in con.execute(f"PRAGMA index_info({index[1]})"))
                )
    assert ("source_type", "official_id", "edition_key") in unique_columns
    assert ("source_type", "official_id", "version_id") not in unique_columns


def test_copy_failure_removes_secure_migration_temporary_file(tmp_path, monkeypatch):
    db_path = tmp_path / "cleanup.db"
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY,
                source_type TEXT NOT NULL,
                official_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                title TEXT NOT NULL
            );
            """
        )
    store = RegulatoryStore(db_path)
    original_signature = store._database_signature
    calls = 0

    def unstable_signature():
        nonlocal calls
        calls += 1
        signature = original_signature()
        if calls == 2:
            return (None, *signature[1:])
        return signature

    monkeypatch.setattr(store, "_database_signature", unstable_signature)
    with pytest.raises(sqlite3.OperationalError, match="복사 중 운영 DB가 변경"):
        store.initialize()
    assert list(tmp_path.glob(".cleanup.db.migration-*.db")) == []


def test_copy_migration_aborts_if_live_database_changes(tmp_path, monkeypatch):
    db_path = tmp_path / "concurrent.db"
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY,
                source_type TEXT NOT NULL,
                official_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                title TEXT NOT NULL
            );
            INSERT INTO documents VALUES (1, 'law', 'L1', 'V1', '원본');
            """
        )

    def mutate_live_database(self):
        if self.db_path != db_path:
            with sqlite3.connect(db_path) as live:
                live.execute("UPDATE documents SET title='동시 커밋' WHERE id=1")

    monkeypatch.setattr(RegulatoryStore, "_initialize_in_place", mutate_live_database)
    with pytest.raises(sqlite3.OperationalError, match="운영 DB가 변경"):
        RegulatoryStore(db_path).initialize()

    with sqlite3.connect(db_path) as con:
        assert con.execute("SELECT title FROM documents WHERE id=1").fetchone()[0] == "동시 커밋"


def test_failed_copy_migration_leaves_live_database_unchanged(tmp_path, monkeypatch):
    db_path = tmp_path / "atomic.db"
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY,
                source_type TEXT NOT NULL,
                official_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                title TEXT NOT NULL
            );
            INSERT INTO documents VALUES (1, 'law', 'L1', 'V1', '원본');
            """
        )

    original = RegulatoryStore._initialize_in_place

    def fail_working_copy(self):
        if self.db_path != db_path:
            raise RuntimeError("실패 주입")
        return original(self)

    monkeypatch.setattr(RegulatoryStore, "_initialize_in_place", fail_working_copy)
    with pytest.raises(RuntimeError, match="실패 주입"):
        RegulatoryStore(db_path).initialize()

    with sqlite3.connect(db_path) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(documents)")}
        row = con.execute("SELECT id, title FROM documents").fetchone()
    assert "edition_key" not in columns
    assert row == (1, "원본")


def test_initialize_rejects_legacy_foreign_key_orphans(tmp_path):
    db_path = tmp_path / "orphan.db"
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE change_events (
                id INTEGER PRIMARY KEY,
                source_type TEXT NOT NULL,
                official_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                title TEXT NOT NULL,
                change_type TEXT NOT NULL,
                candidate_sha256 TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE review_events (
                id INTEGER PRIMARY KEY,
                change_event_id INTEGER REFERENCES change_events(id),
                source_type TEXT NOT NULL,
                official_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                reason TEXT NOT NULL
            );
            INSERT INTO review_events (
                id, change_event_id, source_type, official_id, version_id,
                decision, reviewer, reason
            ) VALUES (1, 999, 'law', 'L1', 'V1', 'approved', 'tester', '고아 행');
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="외래키 무결성"):
        RegulatoryStore(db_path).initialize()


def test_rejected_same_hash_stays_rejected_on_resync(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    document = sample_document()
    document.pop("approved")
    store.upsert_document(document)
    event = store.list_pending_changes()[0]
    store.review_version(
        "admrul",
        "A1",
        "V1",
        change_event_id=event["change_event_id"],
        decision="rejected",
        reviewer="tester",
        reason="공식 원문 대조 후 반려",
    )

    store.upsert_document(document)

    with sqlite3.connect(tmp_path / "regulations.db") as con:
        status = con.execute(
            "SELECT review_status FROM documents WHERE edition_key='V1@2025-01-01'"
        ).fetchone()[0]
    assert status == "rejected"
    assert store.list_pending_changes() == []


def test_rejected_content_candidate_is_not_reopened_without_hash_change(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    original = sample_document()
    store.upsert_document(original)
    changed = sample_document()
    changed["payload_sha256"] = "rejected-change"
    changed["provisions"][0]["text"] = "반려할 변경"
    store.upsert_document(changed)
    event = store.list_pending_changes()[0]
    store.review_version(
        "admrul",
        "A1",
        "V1",
        change_event_id=event["change_event_id"],
        decision="rejected",
        reviewer="tester",
        reason="공식 원문과 불일치",
    )

    store.upsert_document(changed)

    assert store.list_pending_changes() == []
    assert store.get_document("A1", as_of="2025-01-01")["payload_sha256"] == "abc"


def test_hashless_new_version_does_not_overwrite_unreviewed_candidate(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    first = sample_document()
    first.pop("approved")
    first["payload_sha256"] = None
    first["provisions"][0]["text"] = "후보 A"
    second = sample_document()
    second.pop("approved")
    second["payload_sha256"] = None
    second["provisions"][0]["text"] = "후보 B"

    store.upsert_document(first)
    store.upsert_document(second)

    pending = store.list_pending_changes()
    assert len(pending) == 2
    with sqlite3.connect(store.db_path) as con:
        stored_text = con.execute(
            """
            SELECT p.text FROM provisions p JOIN documents d ON d.id=p.document_id
            WHERE d.official_id='A1' AND p.provision_path='제3조'
            """
        ).fetchone()[0]
    assert stored_text == "후보 A"
    selected = next(
        row for row in pending if row["candidate_sha256"] == store._candidate_sha256(second)
    )
    store.review_version(
        "admrul",
        "A1",
        "V1",
        change_event_id=selected["change_event_id"],
        decision="approved",
        reviewer="tester",
        reason="후보 B 확인",
    )
    approved = store.get_document("A1", as_of="2025-01-01")
    assert approved is not None
    assert approved["provisions"][0]["text"] == "후보 B"


def test_hashless_candidate_uses_non_null_stable_identity(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    store.upsert_document(sample_document())
    candidate = sample_document()
    candidate["payload_sha256"] = None
    candidate["provisions"][0]["text"] = "해시 없는 변경 후보"

    store.upsert_document(candidate)
    store.upsert_document(candidate)

    pending = store.list_pending_changes()
    assert len(pending) == 1
    assert pending[0]["candidate_sha256"]


def test_previously_approved_hash_can_reappear_and_be_approved_again(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    store.upsert_document(sample_document())

    def approve_candidate(payload_hash: str, text: str) -> None:
        candidate = sample_document()
        candidate["payload_sha256"] = payload_hash
        candidate["provisions"][0]["text"] = text
        store.upsert_document(candidate)
        event = store.list_pending_changes()[0]
        store.review_version(
            "admrul",
            "A1",
            "V1",
            change_event_id=event["change_event_id"],
            decision="approved",
            reviewer="tester",
            reason="공식 원문 변경 확인",
        )

    approve_candidate("hash-b", "본문 B")
    approve_candidate("hash-c", "본문 C")
    approve_candidate("hash-b", "본문 B 재등장")

    document = store.get_document("A1", as_of="2025-01-01")
    assert document["payload_sha256"] == "hash-b"
    assert document["provisions"][0]["text"] == "본문 B 재등장"
    assert store.list_pending_changes() == []


def test_hash_change_revokes_approval_until_explicit_review(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    store.upsert_document(sample_document())
    changed = sample_document()
    changed["payload_sha256"] = "new-hash"
    changed["provisions"][0]["text"] = "변경된 공식 원문"

    store.upsert_document(changed)

    assert store.get_document("A1", as_of="2025-02-01")["provisions"][0]["text"] == (
        "스프링클러설비는 기준에 따라 설치해야 한다."
    )
    assert store.list_pending_changes()[0]["change_type"] == "content_changed"
    store.review_version(
        "admrul", "A1", "V1", decision="approved", reviewer="tester", reason="fixture review"
    )
    approved = store.get_document("A1", as_of="2025-02-01")
    assert approved["approved"] == 1
    assert approved["provisions"][0]["text"] == "변경된 공식 원문"
