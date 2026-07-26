from __future__ import annotations

from fire_mcp.parser import parse_detail, parse_search_results


def test_parse_law_search_result_normalizes_official_metadata():
    payload = {
        "LawSearch": {
            "law": [
                {
                    "법령명한글": "소방시설 설치 및 관리에 관한 법률",
                    "법령ID": "009503",
                    "법령일련번호": "236977",
                    "시행일자": "20241201",
                    "공포일자": "20211130",
                    "소관부처명": "소방청",
                    "법령구분명": "법률",
                    "현행연혁코드": "현행",
                    "법령상세링크": "/DRF/lawService.do?target=law&MST=236977",
                }
            ],
            "totalCnt": "1",
        }
    }

    result = parse_search_results("law", payload)

    assert result[0]["official_id"] == "009503"
    assert result[0]["version_id"] == "236977"
    assert result[0]["effective_date"] == "2024-12-01"
    assert result[0]["source_type"] == "law"


def test_parse_law_detail_preserves_article_paragraph_item_and_effective_date():
    payload = {
        "법령": {
            "기본정보": {
                "법령명_한글": "테스트법",
                "법령ID": "T1",
                "시행일자": "20250101",
                "공포일자": "20241201",
                "제개정구분": "일부개정",
                "법종구분": {"content": "법률"},
                "소관부처": {"content": "소방청"},
            },
            "조문": {
                "조문단위": [
                    {
                        "조문번호": "2",
                        "조문키": "0002001",
                        "조문제목": "정의",
                        "조문내용": "제2조(정의)",
                        "조문시행일자": "20250301",
                        "조문여부": "조문",
                        "항": [
                            {
                                "항번호": "①",
                                "항내용": "① 정의 본문",
                                "호": [{"호번호": "1.", "호내용": "1. 첫 번째 정의"}],
                            }
                        ],
                    }
                ]
            },
            "부칙": {
                "부칙단위": {"부칙공포일자": "20241201", "부칙내용": [["부칙", "제1조 시행일"]]}
            },
        }
    }

    doc = parse_detail("law", payload, version_id="V1")

    paths = {p["provision_path"] for p in doc["provisions"]}
    assert "제2조" in paths
    assert "제2조 제1항" in paths
    assert "제2조 제1항 제1호" in paths
    article = next(p for p in doc["provisions"] if p["provision_path"] == "제2조")
    assert article["effective_date"] == "2025-03-01"
    assert any(p["kind"] == "addendum" for p in doc["provisions"])


def test_parse_administrative_rule_preserves_addendum_and_exceptions():
    payload = {
        "AdmRulService": {
            "행정규칙기본정보": {
                "행정규칙명": "테스트 화재안전성능기준(NFPC 999)",
                "행정규칙ID": "A1",
                "행정규칙일련번호": "AV1",
                "시행일자": "20250101",
                "발령일자": "20241201",
                "소관부처명": "소방청",
                "행정규칙종류": "고시",
                "현행여부": "Y",
            },
            "조문내용": ["제1조(목적) 목적", "제2조(적용범위) 다만, 예외로 한다."],
            "부칙": {"부칙공포일자": "20241201", "부칙내용": "부칙 제1조(시행일) 시행한다."},
        }
    }

    doc = parse_detail("admrul", payload, version_id="AV1")

    assert doc["title"].startswith("테스트 화재안전성능기준")
    assert len(doc["provisions"]) == 3
    assert any("다만" in p["text"] for p in doc["provisions"])
    assert any(p["kind"] == "addendum" for p in doc["provisions"])
