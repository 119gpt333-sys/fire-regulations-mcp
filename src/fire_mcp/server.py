from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .service import RegulatoryService
from .store import RegulatoryStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("FIRE_MCP_DATA_DIR", PROJECT_ROOT / "data"))
DB_PATH = DATA_DIR / "index" / "fire_regulations.db"

store = RegulatoryStore(DB_PATH)
store.initialize()
service = RegulatoryService(store)
mcp = FastMCP("Korean Fire Inspection Regulatory Knowledge")


@mcp.resource("firelaw://catalog")
def catalog_resource() -> str:
    """승인된 소방·건축 법령과 행정규칙의 공식 출처 목록."""
    return json.dumps(service.catalog(), ensure_ascii=False, indent=2)


@mcp.resource("firelaw://documents/{official_id}")
def document_resource(official_id: str) -> str:
    """공식 ID에 해당하는 현재 승인 시행본과 조문."""
    return json.dumps(
        service.get_rule_as_of(official_id),
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def search_current_rules(
    query: str,
    as_of: str | None = None,
    source_type: str | None = None,
    limit: int = 10,
) -> dict:
    """기준일 현재 승인된 소방·건축 법령과 NFPC/NFTC·형식승인 기준의 원문 조항을 검색한다.

    source_type은 law 또는 admrul이다. 결과는 법적 결론이 아닌 공식 근거 후보다.
    """
    return service.search_current_rules(
        query,
        as_of=as_of,
        source_type=source_type,
        limit=limit,
    )


@mcp.tool()
def get_rule_as_of(official_id: str, as_of: str | None = None) -> dict:
    """공식 법령/행정규칙 ID와 기준일에 해당하는 승인 시행본 전체를 조회한다."""
    return service.get_rule_as_of(official_id, as_of=as_of)


@mcp.tool()
def trace_exception_path(official_id: str, as_of: str | None = None) -> dict:
    """해당 공식 문서에서 단서·제외·경과조치 등 예외 후보를 찾아 추가 확인 사실과 함께 반환한다."""
    return service.trace_exception_path(official_id, as_of=as_of)


@mcp.tool()
def get_source_status() -> dict:
    """현재 데이터베이스 문서·조문 수와 마지막 공식 API 동기화 상태를 조회한다."""
    result = service.get_source_status()
    result["sample_credential_in_use"] = os.getenv("LAW_API_OC", "test") == "test"
    return result


@mcp.tool()
def list_pending_changes() -> dict:
    """최근 동기화 오류와 담당자 검토가 필요한 변경 후보 상태를 반환한다."""
    return service.list_pending_changes()


@mcp.prompt()
def investigate_fire_requirement(question: str, as_of: str) -> str:
    """소방점검 규정 질문을 기준일·법적 위계·예외경로 순으로 검토하는 절차."""
    return f"""다음 소방점검 질문을 검토하라.

질문: {question}
기준일: {as_of}

절차:
1. search_current_rules로 직접 관련 조항을 찾는다.
2. 각 결과의 시행일과 공식 URL을 확인한다.
3. get_rule_as_of로 상위법·하위기준의 전체 맥락을 확인한다.
4. trace_exception_path로 단서·제외·부칙·경과조치를 찾는다.
5. 확인된 사실, 공식 원문, 적용 후보, 부족한 현장사실, 담당자 판단 필요사항을 분리한다.
6. 적합·부적합을 자동 확정하지 않는다.
"""


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
