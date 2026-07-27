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
    assert "제2조제1항" in paths
    assert "제2조제1항제1호" in paths
    article = next(p for p in doc["provisions"] if p["provision_path"] == "제2조")
    assert article["effective_date"] == "2025-03-01"
    assert any(p["kind"] == "addendum" for p in doc["provisions"])


def test_parse_law_uses_official_item_and_subitem_markers_without_inventing_paragraph():
    payload = {
        "법령": {
            "기본정보": {
                "법령명_한글": "테스트규칙",
                "법령ID": "T2",
                "시행일자": "20250101",
            },
            "조문": {
                "조문단위": {
                    "조문번호": "2",
                    "조문내용": "제2조(정의)",
                    "조문여부": "조문",
                    "항": {
                        "호": [
                            {
                                "호번호": "3.",
                                "호내용": "3. 목욕장",
                                "목": {"목번호": "가.", "목내용": "가. 일반목욕장"},
                            },
                            {
                                "호번호": "4.",
                                "호가지번호": "2",
                                "호내용": "4의2. 분기된 항목",
                            },
                        ],
                    },
                }
            },
        }
    }

    doc = parse_detail("law", payload, version_id="V2")

    paths = {p["provision_path"] for p in doc["provisions"]}
    assert "제2조제3호" in paths
    assert "제2조제3호가목" in paths
    assert "제2조제4호의2" in paths
    assert not any("제1항" in path for path in paths)
    assert not any("제1목" in path for path in paths)


def test_parse_law_never_synthesizes_missing_official_numbers():
    payload = {
        "법령": {
            "기본정보": {"법령명_한글": "불완전응답", "법령ID": "T3", "시행일자": "20250101"},
            "조문": {
                "조문단위": {
                    "조문번호": "5",
                    "조문내용": "제5조",
                    "조문여부": "조문",
                    "항": {
                        "항내용": "번호 없는 항 본문",
                        "호": {
                            "호내용": "번호 없는 호 본문",
                            "목": {"목내용": "번호 없는 목 본문"},
                        },
                    },
                }
            },
        }
    }

    doc = parse_detail("law", payload, version_id="V3")

    paths = [p["provision_path"] for p in doc["provisions"]]
    assert not any("제1항" in path or "제1호" in path or "제1목" in path for path in paths)
    assert {p["text"] for p in doc["provisions"]} >= {
        "번호 없는 항 본문",
        "번호 없는 호 본문",
        "번호 없는 목 본문",
    }


def test_parse_law_marks_unnumbered_article_and_does_not_number_addenda():
    payload = {
        "법령": {
            "기본정보": {"법령명_한글": "번호미상법", "법령ID": "T4", "시행일자": "20250101"},
            "조문": {
                "조문단위": {
                    "조문내용": "번호 없는 조문",
                    "조문여부": "조문",
                    "항": {"항번호": "①", "항내용": "자식 항"},
                }
            },
            "부칙": {
                "부칙단위": [
                    {"부칙내용": "첫 부칙"},
                    {"부칙내용": "둘째 부칙"},
                ]
            },
        }
    }

    doc = parse_detail("law", payload, version_id="V4")
    paths = [p["provision_path"] for p in doc["provisions"]]
    assert "번호없음 조문" in paths
    assert "번호없음 조문제1항" in paths
    assert paths.count("부칙") == 2
    assert not any(path.startswith("제조") or path.startswith("부칙 ") for path in paths)


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


def test_parse_administrative_rule_does_not_number_unnumbered_articles():
    payload = {
        "AdmRulService": {
            "행정규칙기본정보": {
                "행정규칙명": "번호 없는 행정규칙",
                "행정규칙ID": "A0",
                "행정규칙일련번호": "AV0",
                "시행일자": "20250101",
            },
            "조문내용": ["번호 없는 첫 문장", "번호 없는 둘째 문장"],
        }
    }
    doc = parse_detail("admrul", payload, version_id="AV0")
    assert [row["provision_path"] for row in doc["provisions"]] == [
        "번호없음 조문",
        "번호없음 조문",
    ]


def test_parse_detail_preserves_annex_table_and_attachment_metadata():
    payload = {
        "AdmRulService": {
            "행정규칙기본정보": {
                "행정규칙명": "테스트 기술기준",
                "행정규칙ID": "A2",
                "행정규칙일련번호": "AV2",
                "시행일자": "20250101",
                "현행여부": "Y",
            },
            "조문내용": "제1조(목적) 목적",
            "별표": {
                "별표단위": {
                    "별표번호": "0001",
                    "별표가지번호": "00",
                    "별표키": "000100",
                    "별표구분": "별표",
                    "별표제목": "시험농도표",
                    "별표내용": [["[별표 1]", "농도 10 %", "시간 5분"]],
                    "별표서식PDF파일링크": "/LSW/flDownload.do?flSeq=123",
                    "별표서식파일링크": "/LSW/flDownload.do?flSeq=124",
                }
            },
            "첨부파일": {
                "첨부파일명": [
                    "개정전문.pdf",
                    "개정전문.hwp",
                    "외부파일.exe",
                    "사용자정보위장.exe",
                    "비표준포트.exe",
                    "인증쿼리제거.pdf",
                ],
                "첨부파일링크": [
                    "http://law.go.kr/flDownload.do?flSeq=200",
                    "/flDownload.do?flSeq=201",
                    "https://evil.example/payload.exe",
                    "https://attacker@law.go.kr/payload.exe",
                    "https://law.go.kr:8443/payload.exe",
                    "https://law.go.kr/flDownload.do?flSeq=202&OC=private-value&refresh%5Ftoken=r&db_password=p&api-key=k&clientSecret=s&authorization=b&passwd=x&session=leak&session_key=leak2",
                ],
            },
        }
    }

    doc = parse_detail("admrul", payload, version_id="AV2")

    assert doc["annexes"][0]["provision_path"] == "별표 1"
    assert doc["annexes"][0]["title"] == "시험농도표"
    assert "농도 10 %" in doc["annexes"][0]["text"]
    assert doc["annexes"][0]["file_links"][0]["url"].startswith("https://www.law.go.kr/")
    assert any(p["kind"] == "annex" and p["provision_path"] == "별표 1" for p in doc["provisions"])
    assert doc["attachments"] == [
        {"name": "개정전문.pdf", "url": "https://www.law.go.kr/flDownload.do?flSeq=200"},
        {"name": "개정전문.hwp", "url": "https://www.law.go.kr/flDownload.do?flSeq=201"},
        {"name": "인증쿼리제거.pdf", "url": "https://law.go.kr/flDownload.do?flSeq=202"},
    ]


def test_parse_annex_without_official_number_never_synthesizes_sequence():
    payload = {
        "AdmRulService": {
            "행정규칙기본정보": {
                "행정규칙명": "미번호 별표 기준",
                "행정규칙ID": "A3",
                "행정규칙일련번호": "AV3",
                "시행일자": "20250101",
            },
            "별표": {
                "별표단위": [
                    {"별표키": "K-A", "별표구분": "별표", "별표내용": "첫 번째"},
                    {"별표키": "K-B", "별표구분": "별표", "별표내용": "두 번째"},
                ]
            },
        }
    }

    doc = parse_detail("admrul", payload, version_id="AV3")

    paths = [annex["provision_path"] for annex in doc["annexes"]]
    assert paths == ["별표 (번호없음·키 K-A)", "별표 (번호없음·키 K-B)"]
    assert not any(path in {"별표 1", "별표 2"} for path in paths)
