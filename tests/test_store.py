from __future__ import annotations

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
