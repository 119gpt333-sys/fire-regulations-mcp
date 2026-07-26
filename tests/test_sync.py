from __future__ import annotations

from fire_mcp.store import RegulatoryStore
from fire_mcp.sync import sync_registry


class FakeClient:
    last_display = 0

    def search(self, target: str, query: str, *, display: int, page: int = 1):
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
            "effective_date": "2025-01-01",
            "status": "현행",
            "official_url": "https://www.law.go.kr/test",
            "payload_sha256": "hash",
            "provisions": [
                {
                    "provision_path": "제1조",
                    "kind": "article",
                    "text": "테스트 목적",
                    "effective_date": "2025-01-01",
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
