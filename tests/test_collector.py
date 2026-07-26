from __future__ import annotations

import json

import httpx

from fire_mcp.collector import LawOpenApiClient, choose_candidates


def test_choose_candidates_prefers_exact_law_title():
    rows = [
        {"title": "소방시설 설치 및 관리에 관한 법률 시행령", "version_id": "2"},
        {"title": "소방시설 설치 및 관리에 관한 법률", "version_id": "1"},
    ]

    chosen = choose_candidates(rows, query="소방시설 설치 및 관리에 관한 법률", exact=True, limit=1)

    assert [row["version_id"] for row in chosen] == ["1"]


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
