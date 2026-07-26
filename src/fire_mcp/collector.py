from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from .parser import parse_detail, parse_search_results

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://www.law.go.kr/DRF"


def choose_candidates(
    rows: list[dict[str, Any]], *, query: str, exact: bool, limit: int
) -> list[dict[str, Any]]:
    if exact:
        exact_rows = [row for row in rows if row.get("title", "").strip() == query.strip()]
        if exact_rows:
            rows = exact_rows
    return rows[: max(1, min(limit, 100))]


class LawOpenApiClient:
    def __init__(
        self,
        *,
        oc: str,
        raw_dir: str | Path,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        retries: int = 3,
    ) -> None:
        if not oc.strip():
            raise ValueError("LAW_API_OC가 비어 있습니다.")
        self.oc = oc
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.retries = max(1, retries)
        self.client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "fire-inspection-mcp/0.1"},
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> LawOpenApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {"OC": self.oc, "type": "JSON", **params}
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.client.get(path, params=params)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("API 응답이 JSON 객체가 아닙니다.")
                return payload
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"국가법령정보 API 호출 실패: {last_error}") from last_error

    def search(
        self,
        target: str,
        query: str,
        *,
        display: int = 20,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        if target not in {"law", "admrul"}:
            raise ValueError("target은 law 또는 admrul만 허용됩니다.")
        payload = self._get_json(
            "/lawSearch.do",
            {
                "target": target,
                "query": query,
                "display": max(1, min(display, 100)),
                "page": max(1, page),
            },
        )
        return parse_search_results(target, payload)

    def fetch_document(self, candidate: dict[str, Any]) -> dict[str, Any]:
        target = candidate["source_type"]
        version_id = str(candidate["version_id"])
        detail_params = {"target": target}
        if target == "law":
            detail_params["MST"] = version_id
        elif target == "admrul":
            detail_params["ID"] = version_id
        else:
            raise ValueError(f"지원하지 않는 source_type: {target}")
        payload = self._get_json("/lawService.do", detail_params)
        raw_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        day_dir = self.raw_dir / target / date.today().isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        safe_id = candidate.get("official_id") or "unknown"
        raw_path = day_dir / f"{safe_id}_{version_id}_{digest[:12]}.json"
        if not raw_path.exists():
            raw_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        document = parse_detail(target, payload, version_id=version_id)
        document["payload_sha256"] = digest
        document["raw_path"] = str(raw_path)
        document["search_metadata"] = candidate
        return document
