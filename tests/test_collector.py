from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

import fire_mcp.collector as collector_module
from fire_mcp.collector import (
    LawOpenApiClient,
    _write_snapshot_without_following_symlinks,
    choose_candidates,
)


def test_choose_candidates_prefers_exact_law_title():
    rows = [
        {"title": "소방시설 설치 및 관리에 관한 법률 시행령", "version_id": "2"},
        {"title": "소방시설 설치 및 관리에 관한 법률", "version_id": "1"},
    ]

    chosen = choose_candidates(rows, query="소방시설 설치 및 관리에 관한 법률", exact=True, limit=1)

    assert [row["version_id"] for row in chosen] == ["1"]


def test_choose_candidates_does_not_substitute_related_title_for_missing_exact_match():
    rows = [{"title": "테스트법 시행령", "version_id": "2"}]

    chosen = choose_candidates(rows, query="테스트법", exact=True, limit=1)

    assert chosen == []


def test_client_fetches_detail_and_writes_hashed_raw_snapshot(tmp_path):
    search_payload = {
        "LawSearch": {
            "law": [
                {
                    "법령명한글": "테스트법",
                    "법령ID": "T1",
                    "법령일련번호": "V1",
                    "시행일자": "20250101",
                    "공포일자": "20241201",
                    "소관부처명": "소방청",
                    "법령구분명": "법률",
                    "현행연혁코드": "현행",
                }
            ]
        }
    }
    detail_payload = {
        "법령": {
            "기본정보": {
                "법령명_한글": "테스트법",
                "법령ID": "T1",
                "시행일자": "20250101",
                "공포일자": "20241201",
                "제개정구분": "제정",
                "법종구분": {"content": "법률"},
                "소관부처": {"content": "소방청"},
            },
            "조문": {
                "조문단위": {
                    "조문번호": "1",
                    "조문키": "0001001",
                    "조문내용": "제1조(목적) 테스트",
                    "조문시행일자": "20250101",
                    "조문여부": "조문",
                }
            },
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = detail_payload if request.url.path.endswith("lawService.do") else search_payload
        return httpx.Response(200, json=payload)

    client = LawOpenApiClient(
        oc="test",
        raw_dir=tmp_path / "raw",
        transport=httpx.MockTransport(handler),
    )
    candidate = client.search("law", "테스트법", display=1)[0]
    document = client.fetch_document(candidate)

    assert document["payload_sha256"]
    assert "OC=" not in document["official_url"]
    assert document["official_url"] == "https://www.law.go.kr/lsInfoP.do?lsiSeq=V1"
    assert document["provisions"][0]["text"] == "제1조(목적) 테스트"
    raw_files = list((tmp_path / "raw" / "law").rglob("*.json"))
    assert len(raw_files) == 1
    assert json.loads(raw_files[0].read_text(encoding="utf-8"))["법령"]


def test_client_fetches_effective_law_version_with_eflaw_target(tmp_path):
    search_payload = {
        "LawSearch": {
            "law": {
                "법령명한글": "테스트법",
                "법령ID": "T1",
                "법령일련번호": "V1",
                "시행일자": "20240101",
                "공포일자": "20231201",
                "현행연혁코드": "연혁",
            }
        }
    }
    detail_payload = {
        "법령": {
            "법령키": "V1",
            "기본정보": {
                "법령명_한글": "테스트법",
                "법령ID": "T1",
                "법령일련번호": "V1",
                "시행일자": "20240101",
                "공포일자": "20231201",
            },
            "조문": {
                "조문단위": {
                    "조문번호": "1",
                    "조문내용": "제1조(목적) 테스트",
                    "조문여부": "조문",
                }
            },
        }
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = detail_payload if request.url.path.endswith("lawService.do") else search_payload
        return httpx.Response(200, json=payload)

    client = LawOpenApiClient(
        oc="test",
        raw_dir=tmp_path / "raw",
        transport=httpx.MockTransport(handler),
    )

    candidate = client.search("eflaw", "테스트법", display=20, nw=1)[0]
    document = client.fetch_document(candidate)

    assert candidate["source_type"] == "law"
    assert candidate["api_target"] == "eflaw"
    assert document["status"] == "연혁"
    assert requests[0].url.params["target"] == "eflaw"
    assert requests[0].url.params["nw"] == "1"
    assert requests[1].url.params["target"] == "eflaw"
    assert requests[1].url.params["efYd"] == "20240101"


def test_snapshot_path_components_cannot_escape_raw_directory(tmp_path):
    payload = {
        "법령": {
            "기본정보": {
                "법령명_한글": "테스트법",
                "법령ID": "../../outside",
                "시행일자": "20250101",
            }
        }
    }
    client = LawOpenApiClient(
        oc="test",
        raw_dir=tmp_path / "raw",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )

    document = client.fetch_document(
        {
            "source_type": "law",
            "official_id": "../../outside",
            "version_id": "../version/../../evil",
            "title": "테스트법",
        }
    )
    raw_path = Path(document["raw_path"])

    assert raw_path.resolve().is_relative_to((tmp_path / "raw").resolve())
    assert ".." not in raw_path.name
    assert json.loads(raw_path.read_text(encoding="utf-8")) == payload
    assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == document["payload_sha256"]


def test_snapshot_rejects_symlinked_day_directory_outside_raw_root(tmp_path):
    payload = {
        "법령": {
            "기본정보": {
                "법령명_한글": "테스트법",
                "법령ID": "T1",
                "시행일자": "20250101",
            }
        }
    }
    raw_dir = tmp_path / "raw"
    source_dir = raw_dir / "law"
    source_dir.mkdir(parents=True)
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    (source_dir / date.today().isoformat()).symlink_to(external_dir, target_is_directory=True)
    client = LawOpenApiClient(
        oc="test",
        raw_dir=raw_dir,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )

    with pytest.raises(ValueError, match="허용 디렉터리"):
        client.fetch_document(
            {
                "source_type": "law",
                "official_id": "T1",
                "version_id": "V1",
                "title": "테스트법",
            }
        )

    assert list(external_dir.iterdir()) == []


def test_snapshot_rejects_preexisting_file_with_different_content(tmp_path):
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    content = b'{"trusted": true}'
    path = _write_snapshot_without_following_symlinks(raw_root, "law", "T1_V1_digest.json", content)
    path.write_bytes(b'{"tampered": true}')

    with pytest.raises(ValueError, match="무결성"):
        _write_snapshot_without_following_symlinks(raw_root, "law", "T1_V1_digest.json", content)


def test_snapshot_root_swap_cannot_redirect_write_outside_pinned_directory(tmp_path, monkeypatch):
    raw_root = tmp_path / "raw"
    moved_root = tmp_path / "raw-pinned"
    external = tmp_path / "external"
    raw_root.mkdir()
    external.mkdir()
    real_open = collector_module.os.open
    swapped = False

    def attack_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "law" and dir_fd is not None and not swapped:
            swapped = True
            raw_root.rename(moved_root)
            raw_root.symlink_to(external, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(collector_module.os, "open", attack_open)

    with pytest.raises(ValueError, match="변경"):
        _write_snapshot_without_following_symlinks(raw_root, "law", "probe.json", b"trusted")

    assert list(external.iterdir()) == []
    assert (moved_root / "law" / date.today().isoformat() / "probe.json").read_bytes() == b"trusted"


def test_fetch_does_not_create_directories_after_parent_is_replaced_by_symlink(tmp_path):
    payload = {
        "법령": {
            "기본정보": {
                "법령명_한글": "테스트법",
                "법령ID": "T1",
                "시행일자": "20250101",
            }
        }
    }
    storage = tmp_path / "storage"
    external = tmp_path / "external"
    moved_storage = tmp_path / "storage-pinned"
    external.mkdir()
    client = LawOpenApiClient(
        oc="test",
        raw_dir=storage / "raw",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    storage.rename(moved_storage)
    storage.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="변경"):
        client.fetch_document(
            {
                "source_type": "law",
                "official_id": "T1",
                "version_id": "V1",
                "title": "테스트법",
            }
        )

    client.close()
    assert list(external.iterdir()) == []


def test_fetch_rejects_detail_with_different_effective_date(tmp_path):
    payload = {
        "법령": {
            "기본정보": {
                "법령명_한글": "테스트법",
                "법령ID": "T1",
                "법령일련번호": "V1",
                "시행일자": "20250101",
            }
        }
    }
    client = LawOpenApiClient(
        oc="test",
        raw_dir=tmp_path / "raw",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    with pytest.raises(ValueError, match="시행일"):
        client.fetch_document(
            {
                "source_type": "law",
                "api_target": "eflaw",
                "official_id": "T1",
                "version_id": "V1",
                "effective_date": "2026-01-01",
                "title": "테스트법",
            }
        )
    client.close()


def test_fetch_rejects_detail_with_different_version_id(tmp_path):
    payload = {
        "법령": {
            "기본정보": {
                "법령명_한글": "테스트법",
                "법령ID": "T1",
                "법령일련번호": "OTHER",
                "시행일자": "20250101",
            }
        }
    }
    client = LawOpenApiClient(
        oc="test",
        raw_dir=tmp_path / "raw",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    with pytest.raises(ValueError, match="시행본 ID"):
        client.fetch_document(
            {
                "source_type": "law",
                "official_id": "T1",
                "version_id": "EXPECTED",
                "title": "테스트법",
            }
        )
    client.close()


def test_fetch_rejects_eflaw_candidate_without_effective_date(tmp_path):
    client = LawOpenApiClient(
        oc="test",
        raw_dir=tmp_path / "raw",
        transport=httpx.MockTransport(
            lambda request: pytest.fail("시행일 검증 전에 API를 호출하면 안 됩니다.")
        ),
    )
    with pytest.raises(ValueError, match="effective_date"):
        client.fetch_document(
            {
                "source_type": "law",
                "api_target": "eflaw",
                "official_id": "T1",
                "version_id": "V1",
                "title": "테스트법",
            }
        )
    client.close()


def test_fetch_rejects_effective_law_with_mismatched_root_law_key(tmp_path):
    payload = {
        "법령": {
            "법령키": "OTHER202501010001",
            "기본정보": {
                "법령명_한글": "테스트법",
                "법령ID": "T1",
                "시행일자": "20250101",
            },
        }
    }
    client = LawOpenApiClient(
        oc="test",
        raw_dir=tmp_path / "raw",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    with pytest.raises(ValueError, match="법령키"):
        client.fetch_document(
            {
                "source_type": "law",
                "api_target": "eflaw",
                "official_id": "T1",
                "version_id": "V1",
                "effective_date": "2025-01-01",
                "title": "테스트법",
            }
        )
    client.close()
