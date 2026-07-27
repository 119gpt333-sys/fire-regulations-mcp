from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import time
from contextlib import suppress
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from .parser import parse_detail, parse_search_results

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://www.law.go.kr/DRF"


def _safe_filename_component(value: Any, *, fallback: str = "unknown") -> str:
    safe = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", str(value or ""))
    while ".." in safe:
        safe = safe.replace("..", "_")
    safe = safe.strip("._-")[:120]
    return safe or fallback


def _open_pinned_directory(path: Path) -> int:
    absolute = path.absolute()
    if not absolute.is_absolute():
        raise ValueError("원문 스냅샷 루트는 절대경로여야 합니다.")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open("/", directory_flags)
    try:
        for component in absolute.parts[1:]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
    except Exception:
        os.close(current_fd)
        raise
    return current_fd


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _write_snapshot_without_following_symlinks(
    raw_root: Path,
    source_type: str,
    filename: str,
    content: bytes,
    *,
    expected_root_stat: os.stat_result | None = None,
) -> Path:
    raw_root = raw_root.absolute()
    source_dir = raw_root / source_type
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = _open_pinned_directory(raw_root)
    except OSError as exc:
        raise ValueError("원문 스냅샷 루트가 초기화 후 변경되었습니다.") from exc
    root_stat = os.fstat(root_fd)
    if expected_root_stat is not None and not _same_inode(root_stat, expected_root_stat):
        os.close(root_fd)
        raise ValueError("원문 스냅샷 루트가 초기화 후 변경되었습니다.")
    day_name = date.today().isoformat()
    try:
        with suppress(FileExistsError):
            os.mkdir(source_type, mode=0o700, dir_fd=root_fd)
        source_fd = os.open(source_type, directory_flags, dir_fd=root_fd)
        try:
            with suppress(FileExistsError):
                os.mkdir(day_name, mode=0o700, dir_fd=source_fd)
            try:
                day_fd = os.open(day_name, directory_flags, dir_fd=source_fd)
            except OSError as exc:
                raise ValueError("원문 스냅샷 경로가 허용 디렉터리를 벗어났습니다.") from exc
            try:
                create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                try:
                    file_fd = os.open(filename, create_flags, 0o600, dir_fd=day_fd)
                except FileExistsError:
                    file_fd = os.open(
                        filename,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=day_fd,
                    )
                    try:
                        snapshot_stat = os.fstat(file_fd)
                        if not stat.S_ISREG(snapshot_stat.st_mode):
                            raise ValueError("원문 스냅샷 대상이 일반 파일이 아닙니다.")
                        if snapshot_stat.st_size != len(content):
                            raise ValueError("기존 원문 스냅샷의 무결성 검증에 실패했습니다.")
                        with os.fdopen(file_fd, "rb") as existing_file:
                            file_fd = -1
                            existing_content = existing_file.read()
                        if (
                            not hashlib.sha256(existing_content).digest()
                            == hashlib.sha256(content).digest()
                        ):
                            raise ValueError("기존 원문 스냅샷의 무결성 검증에 실패했습니다.")
                    finally:
                        if file_fd >= 0:
                            os.close(file_fd)
                else:
                    snapshot_stat = os.fstat(file_fd)
                    with os.fdopen(file_fd, "wb") as raw_file:
                        raw_file.write(content)
            finally:
                os.close(day_fd)
        finally:
            os.close(source_fd)

        raw_path = source_dir / day_name / filename
        try:
            visible_root_stat = os.stat(raw_root, follow_symlinks=False)
            visible_file_stat = os.stat(raw_path, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("원문 스냅샷 경로가 쓰기 중 변경되었습니다.") from exc
        if (
            not stat.S_ISDIR(visible_root_stat.st_mode)
            or not _same_inode(root_stat, visible_root_stat)
            or not _same_inode(snapshot_stat, visible_file_stat)
        ):
            raise ValueError("원문 스냅샷 경로가 쓰기 중 변경되었습니다.")
        return raw_path
    finally:
        os.close(root_fd)


def choose_candidates(
    rows: list[dict[str, Any]], *, query: str, exact: bool, limit: int
) -> list[dict[str, Any]]:
    if exact:
        rows = [row for row in rows if row.get("title", "").strip() == query.strip()]
    return rows[: max(1, limit)]


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
        self.raw_root = self.raw_dir.resolve()
        self.raw_root_stat = os.stat(self.raw_root, follow_symlinks=False)
        self.retries = max(1, retries)
        self.client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "fire-inspection-mcp/0.2"},
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
        nw: int | None = None,
    ) -> list[dict[str, Any]]:
        if target not in {"law", "eflaw", "admrul"}:
            raise ValueError("target은 law, eflaw 또는 admrul만 허용됩니다.")
        params: dict[str, Any] = {
            "target": target,
            "query": query,
            "display": max(1, min(display, 100)),
            "page": max(1, page),
        }
        if nw is not None:
            params["nw"] = nw
        payload = self._get_json("/lawSearch.do", params)
        return parse_search_results(target, payload)

    def fetch_document(self, candidate: dict[str, Any]) -> dict[str, Any]:
        source_type = str(candidate.get("source_type") or "").strip()
        if not source_type:
            raise ValueError("검색 후보 필수값 누락: source_type")
        api_target = candidate.get("api_target", source_type)
        version_id = str(candidate.get("version_id") or "").strip()
        required_candidate_values = {
            "official_id": str(candidate.get("official_id") or "").strip(),
            "version_id": version_id,
            "title": str(candidate.get("title") or "").strip(),
        }
        missing = [key for key, value in required_candidate_values.items() if not value]
        if api_target == "eflaw":
            effective_date = str(candidate.get("effective_date") or "").strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", effective_date):
                missing.append("effective_date")
        if missing:
            raise ValueError(f"검색 후보 필수값 누락 또는 형식 오류: {', '.join(missing)}")
        detail_params = {"target": api_target}
        if source_type == "law":
            detail_params["MST"] = version_id
            if api_target == "eflaw" and candidate.get("effective_date"):
                detail_params["efYd"] = str(candidate["effective_date"]).replace("-", "")
        elif source_type == "admrul":
            detail_params["ID"] = version_id
        else:
            raise ValueError(f"지원하지 않는 source_type: {source_type}")
        payload = self._get_json("/lawService.do", detail_params)
        if source_type == "law":
            detail_root = payload.get("법령", payload.get("LawService", {}))
            basic = detail_root.get("기본정보", {}) if isinstance(detail_root, dict) else {}
            actual_version_id = str(
                basic.get("법령일련번호") or basic.get("법령MST") or basic.get("MST") or ""
            ).strip()
            law_key = str(detail_root.get("법령키") or "").strip()
            if not actual_version_id and api_target == "eflaw":
                expected_key_prefix = required_candidate_values["official_id"] + str(
                    candidate.get("effective_date") or ""
                ).replace("-", "")
                if not law_key or not law_key.startswith(expected_key_prefix):
                    raise ValueError("검색 후보와 상세 응답의 법령키가 일치하지 않습니다.")
        else:
            detail_root = payload.get("AdmRulService", payload.get("행정규칙", {}))
            basic = detail_root.get("행정규칙기본정보", {}) if isinstance(detail_root, dict) else {}
            actual_version_id = str(basic.get("행정규칙일련번호") or "").strip()
        if actual_version_id and actual_version_id != version_id:
            raise ValueError("검색 후보와 상세 응답의 시행본 ID가 일치하지 않습니다.")
        raw_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        raw_root = self.raw_root
        safe_id = _safe_filename_component(candidate.get("official_id"))
        safe_version = _safe_filename_component(version_id, fallback="version")
        filename = f"{safe_id}_{safe_version}_{digest[:12]}.json"
        raw_path = _write_snapshot_without_following_symlinks(
            raw_root,
            source_type,
            filename,
            raw_bytes,
            expected_root_stat=self.raw_root_stat,
        )
        document = parse_detail(source_type, payload, version_id=version_id)
        if not document.get("title") or not document.get("effective_date"):
            raise ValueError("상세 응답의 법령명 또는 시행일이 누락되었습니다.")
        expected_official_id = str(candidate.get("official_id") or "").strip()
        if expected_official_id and document["official_id"] != expected_official_id:
            raise ValueError("검색 후보와 상세 응답의 공식 ID가 일치하지 않습니다.")
        expected_effective_date = str(candidate.get("effective_date") or "").strip()
        if len(expected_effective_date) == 8 and expected_effective_date.isdigit():
            expected_effective_date = (
                f"{expected_effective_date[:4]}-{expected_effective_date[4:6]}-"
                f"{expected_effective_date[6:]}"
            )
        if expected_effective_date and document.get("effective_date") != expected_effective_date:
            raise ValueError("검색 후보와 상세 응답의 시행일이 일치하지 않습니다.")
        document["status"] = candidate.get("status") or document["status"]
        document["payload_sha256"] = digest
        document["raw_path"] = str(raw_path)
        document["search_metadata"] = candidate
        return document
