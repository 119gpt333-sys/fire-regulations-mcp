from __future__ import annotations

import pytest

from fire_mcp.store import RegulatoryStore
from fire_mcp.sync import sync_registry


class FakeClient:
    last_display = 0

    def search(
        self,
        target: str,
        query: str,
        *,
        display: int,
        page: int = 1,
        nw: int | None = None,
    ):
        self.last_display = display
        return [
            {
                "source_type": target,
                "title": f"{query} 시행령",
                "official_id": "X",
                "version_id": "VX",
            },
            {"source_type": target, "title": query, "official_id": "T1", "version_id": "V1"},
        ]

    def fetch_document(self, candidate: dict):
        return {
            "source_type": candidate["source_type"],
            "official_id": candidate["official_id"],
            "version_id": candidate["version_id"],
            "title": candidate["title"],
            "authority": "소방청",
            "document_kind": "법률",
            "promulgation_date": "2024-12-01",
            "effective_date": candidate.get("effective_date") or "2025-01-01",
            "status": "현행",
            "official_url": "https://www.law.go.kr/test",
            "payload_sha256": "hash",
            "provisions": [
                {
                    "provision_path": "제1조",
                    "kind": "article",
                    "text": "테스트 목적",
                    "effective_date": candidate.get("effective_date") or "2025-01-01",
                }
            ],
        }


def test_sync_registry_applies_exact_title_filter_and_records_report(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    registry = [{"target": "law", "query": "테스트법", "exact": True, "limit": 1}]

    client = FakeClient()
    report = sync_registry(registry, client=client, store=store)

    assert report["documents_seen"] == 1
    assert report["documents_saved"] == 1
    assert report["errors"] == []
    assert client.last_display >= 20
    assert store.catalog() == []
    pending = store.list_pending_changes()
    assert len(pending) == 1
    assert pending[0]["title"] == "테스트법"
    assert store.status()["last_sync"]["status"] == "success"


def test_invalid_registry_is_rejected_before_sync_run_starts(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    with pytest.raises(ValueError, match="query"):
        sync_registry([{"target": "law"}], client=FakeClient(), store=store)
    assert store.status()["last_sync"] is None


class FakeTemporalClient(FakeClient):
    calls: list[tuple]

    def __init__(self) -> None:
        self.calls = []

    def search(
        self,
        target: str,
        query: str,
        *,
        display: int,
        page: int = 1,
        nw: int | None = None,
    ):
        self.calls.append((target, nw))
        if target != "eflaw" or nw not in {1, 2, 3}:
            return []
        return [
            {
                "source_type": "law",
                "api_target": "eflaw",
                "title": "테스트법 종전명" if nw == 1 else query,
                "official_id": "T1",
                "version_id": f"V{nw}",
                "effective_date": f"202{nw}-01-01",
                "status": {1: "연혁", 2: "시행예정", 3: "현행"}[nw],
            }
        ]


def test_sync_registry_collects_historical_future_and_current_law_versions(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    registry = [
        {
            "target": "law",
            "query": "테스트법",
            "exact": True,
            "limit": 1,
            "all_versions": True,
        }
    ]
    client = FakeTemporalClient()

    report = sync_registry(registry, client=client, store=store)

    assert report["documents_seen"] == 3
    assert report["documents_saved"] == 3
    assert client.calls == [("eflaw", 1), ("eflaw", 2), ("eflaw", 3)]
    assert {item["version_id"] for item in store.list_pending_changes()} == {"V1", "V2", "V3"}


class FakePaginatedTemporalClient(FakeTemporalClient):
    def search(
        self,
        target: str,
        query: str,
        *,
        display: int,
        page: int = 1,
        nw: int | None = None,
    ):
        self.calls.append((target, nw, page))
        if nw != 1:
            return []
        count = 100 if page == 1 else (1 if page == 2 else 0)
        start = (page - 1) * 100
        return [
            {
                "source_type": "law",
                "api_target": "eflaw",
                "title": query,
                "official_id": "T1",
                "version_id": f"V{start + index}",
                "effective_date": "2024-01-01",
                "status": "연혁",
            }
            for index in range(count)
        ]


def test_sync_registry_paginates_all_temporal_results(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    client = FakePaginatedTemporalClient()

    report = sync_registry(
        [{"target": "law", "query": "테스트법", "exact": True, "all_versions": True}],
        client=client,
        store=store,
    )

    assert report["documents_seen"] == 101
    assert ("eflaw", 1, 2) in client.calls


class FakeNoExactTemporalClient(FakeTemporalClient):
    def search(self, *args, **kwargs):
        rows = super().search(*args, **kwargs)
        for row in rows:
            row["title"] = "관련법 시행령"
        return rows


def test_sync_registry_records_coverage_error_when_exact_title_is_missing(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    client = FakeNoExactTemporalClient()

    report = sync_registry(
        [{"target": "law", "query": "없는법", "exact": True, "all_versions": True}],
        client=client,
        store=store,
    )

    assert report["status"] == "failed"
    assert report["documents_seen"] == 0
    assert any("정확한 제목" in error for error in report["errors"])


class FakeStagedEffectiveClient(FakeTemporalClient):
    def search(
        self,
        target: str,
        query: str,
        *,
        display: int,
        page: int = 1,
        nw: int | None = None,
    ):
        if nw != 1:
            return []
        return [
            {
                "source_type": "law",
                "api_target": "eflaw",
                "title": query,
                "official_id": "T1",
                "version_id": "SAME-MST",
                "effective_date": date,
                "status": "연혁",
            }
            for date in ("2022-12-01", "2024-12-01")
        ]


def test_sync_registry_preserves_multiple_effective_dates_for_same_mst(tmp_path):
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()

    report = sync_registry(
        [{"target": "law", "query": "테스트법", "exact": True, "all_versions": True}],
        client=FakeStagedEffectiveClient(),
        store=store,
    )

    assert report["documents_seen"] == 2
    assert report["documents_saved"] == 2
    assert {item["effective_date"] for item in store.list_pending_changes()} == {
        "2022-12-01",
        "2024-12-01",
    }
