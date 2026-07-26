from __future__ import annotations

import pytest

from fire_mcp.service import RegulatoryService
from fire_mcp.store import RegulatoryStore


def build_service(tmp_path) -> RegulatoryService:
    store = RegulatoryStore(tmp_path / "regulations.db")
    store.initialize()
    store.upsert_document(
        {
            "source_type": "admrul",
            "official_id": "A1",
            "version_id": "V1",
            "title": "가스누설경보기의 화재안전성능기준(NFPC 206)",
            "authority": "소방청",
            "document_kind": "고시",
            "promulgation_date": "2022-11-25",
            "effective_date": "2022-12-01",
            "status": "현행",
            "official_url": "https://www.law.go.kr/test",
            "payload_sha256": "hash",
            "approved": 1,
            "provisions": [
                {
                    "provision_path": "제2조",
                    "kind": "article",
                    "text": "적용한다. 다만, 다른 법에 적합한 경우 이 기준에 적합한 것으로 본다.",
                    "effective_date": "2022-12-01",
                }
            ],
        }
    )
    return RegulatoryService(store)


def test_search_current_rules_returns_grounded_envelope(tmp_path):
    service = build_service(tmp_path)

    result = service.search_current_rules("가스누설경보기", as_of="2025-01-01")

    assert result["query"] == "가스누설경보기"
    assert result["as_of"] == "2025-01-01"
    assert result["results"][0]["provision_path"] == "제2조"
    assert result["results"][0]["official_url"].startswith("https://www.law.go.kr")
    assert "자동 확정" in result["disclaimer"]


def test_search_current_rules_rejects_invalid_date(tmp_path):
    service = build_service(tmp_path)

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        service.search_current_rules("가스누설경보기", as_of="20250101")


def test_trace_exception_path_returns_candidate_not_final_judgment(tmp_path):
    service = build_service(tmp_path)

    result = service.trace_exception_path("A1", as_of="2025-01-01")

    assert len(result["candidates"]) == 1
    assert result["requires_human_review"] is True


def test_pending_changes_returns_review_queue(tmp_path):
    service = build_service(tmp_path)
    pending = {
        "source_type": "law",
        "official_id": "L2",
        "version_id": "V1",
        "title": "소방시설 설치 및 관리에 관한 법률",
        "official_url": "https://www.law.go.kr/test2",
        "payload_sha256": "pending-hash",
        "effective_date": "2026-01-01",
        "provisions": [],
    }
    service.store.upsert_document(pending)

    result = service.list_pending_changes()

    assert result["pending_count"] == 1
    assert result["pending_changes"][0]["official_id"] == "L2"
