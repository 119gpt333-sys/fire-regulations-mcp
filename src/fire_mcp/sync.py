from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .collector import LawOpenApiClient, choose_candidates
from .store import RegulatoryStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = Path(os.getenv("FIRE_MCP_DATA_DIR", PROJECT_ROOT / "data"))
DEFAULT_REGISTRY = PROJECT_ROOT / "config" / "source_registry.json"


def load_registry(path: str | Path = DEFAULT_REGISTRY) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("source_registry.json 최상위 값은 배열이어야 합니다.")
    return payload


def sync_registry(
    registry: list[dict[str, Any]],
    *,
    client: Any,
    store: RegulatoryStore,
    max_per_entry: int | None = None,
) -> dict[str, Any]:
    started = datetime.now(UTC).isoformat()
    run_id = store.start_sync_run(started)
    seen = 0
    saved = 0
    errors: list[str] = []
    for entry in registry:
        target = entry["target"]
        query = entry["query"]
        configured_limit = int(entry.get("limit", 20))
        limit = min(configured_limit, max_per_entry) if max_per_entry else configured_limit
        try:
            search_display = max(20, limit) if entry.get("exact", False) else max(limit, 1)
            rows = client.search(target, query, display=search_display, page=1)
            chosen = choose_candidates(
                rows,
                query=query,
                exact=bool(entry.get("exact", False)),
                limit=limit,
            )
            seen += len(chosen)
            for candidate in chosen:
                try:
                    document = client.fetch_document(candidate)
                    store.upsert_document(document)
                    saved += 1
                except Exception as exc:  # 개별 문서 실패가 전체 동기화를 중단하지 않음
                    errors.append(f"{target}:{candidate.get('title', query)}: {exc}")
        except Exception as exc:
            errors.append(f"{target}:{query}: {exc}")
    status = "success" if not errors else ("partial" if saved else "failed")
    finished = datetime.now(UTC).isoformat()
    store.finish_sync_run(
        run_id,
        finished_at=finished,
        status=status,
        documents_seen=seen,
        documents_saved=saved,
        errors=errors,
    )
    return {
        "status": status,
        "started_at": started,
        "finished_at": finished,
        "documents_seen": seen,
        "documents_saved": saved,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="국가법령정보센터 공식 자료 동기화")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--max-per-entry", type=int, default=None)
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    store = RegulatoryStore(data_dir / "index" / "fire_regulations.db")
    store.initialize()
    oc = os.getenv("LAW_API_OC", "test")
    if oc == "test":
        print("주의: 샘플 인증값 OC=test로 실행합니다. 운영 전 LAW_API_OC를 설정하세요.")
    with LawOpenApiClient(oc=oc, raw_dir=data_dir / "raw") as client:
        report = sync_registry(
            load_registry(args.registry),
            client=client,
            store=store,
            max_per_entry=args.max_per_entry,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
