from __future__ import annotations

from datetime import date
from typing import Any

from .store import RegulatoryStore

DISCLAIMER = (
    "이 결과는 공식 원문 검색과 적용 후보 제시이며 개별 시설의 적법·부적합을 자동 확정하지 않습니다. "
    "허가·사용승인 시점, 용도, 면적, 층, 설비상태와 부칙·예외를 담당자가 원문으로 확인해야 합니다."
)


def _validate_date(value: str | None) -> str:
    if value is None:
        return date.today().isoformat()
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError("as_of는 YYYY-MM-DD 형식이어야 합니다.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("as_of는 YYYY-MM-DD 형식이어야 합니다.") from exc


class RegulatoryService:
    def __init__(self, store: RegulatoryStore):
        self.store = store

    def search_current_rules(
        self,
        query: str,
        *,
        as_of: str | None = None,
        source_type: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        if not query.strip():
            raise ValueError("query가 비어 있습니다.")
        as_of = _validate_date(as_of)
        return {
            "query": query,
            "as_of": as_of,
            "source_type": source_type,
            "results": self.store.search(
                query,
                as_of=as_of,
                source_type=source_type,
                limit=limit,
            ),
            "disclaimer": DISCLAIMER,
        }

    def get_rule_as_of(self, official_id: str, *, as_of: str | None = None) -> dict[str, Any]:
        as_of = _validate_date(as_of)
        document = self.store.get_document(official_id, as_of=as_of)
        return {
            "official_id": official_id,
            "as_of": as_of,
            "document": document,
            "found": document is not None,
            "disclaimer": DISCLAIMER,
        }

    def trace_exception_path(self, official_id: str, *, as_of: str | None = None) -> dict[str, Any]:
        as_of = _validate_date(as_of)
        return {
            "official_id": official_id,
            "as_of": as_of,
            "candidates": self.store.trace_exception_path(official_id, as_of=as_of),
            "requires_human_review": True,
            "missing_facts_to_check": [
                "건축허가·사용승인·용도변경 시점",
                "건축물 용도·면적·층수·수용인원",
                "설비 종류·승인도면·현장 상태",
                "부칙·경과조치·상위법 위임범위",
            ],
            "disclaimer": DISCLAIMER,
        }

    def get_source_status(self) -> dict[str, Any]:
        status = self.store.status()
        status["sample_credential_in_use"] = False
        status["disclaimer"] = DISCLAIMER
        return status

    def catalog(self) -> dict[str, Any]:
        return {"sources": self.store.catalog(), "disclaimer": DISCLAIMER}

    def list_pending_changes(self) -> dict[str, Any]:
        status = self.store.status()
        last = status.get("last_sync") or {}
        errors = [line for line in (last.get("errors") or "").splitlines() if line]
        pending = self.store.list_pending_changes()
        return {
            "last_sync": last,
            "pending_count": len(pending),
            "pending_changes": pending,
            "sync_errors_requiring_review": errors,
            "note": (
                "신규 시행본과 동일 시행본의 원문 해시 변경은 승인 전 운영검색에 반영되지 않습니다. "
                "검토·승인은 MCP 밖의 권한 통제 절차에서 수행해야 합니다."
            ),
        }
