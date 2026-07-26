from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode


def _date(value: Any) -> str | None:
    text = str(value or "").strip().replace("-", "")
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    return None


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, list):
        return " ".join(filter(None, (_text(item) for item in value)))
    if isinstance(value, dict):
        if "content" in value:
            return _text(value["content"])
        return " ".join(filter(None, (_text(item) for item in value.values())))
    return str(value).strip()


def _official_url(target: str, *, official_id: str, version_id: str) -> str:
    if target == "law":
        return "https://www.law.go.kr/lsInfoP.do?" + urlencode({"lsiSeq": version_id})
    return "https://www.law.go.kr/admRulInfoP.do?" + urlencode(
        {"admRulSeq": version_id or official_id}
    )


def parse_search_results(target: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if target == "law":
        root = payload.get("LawSearch", {})
        rows = _list(root.get("law"))
        return [
            {
                "source_type": "law",
                "official_id": str(row.get("법령ID", "")),
                "version_id": str(row.get("법령일련번호", "")),
                "title": _text(row.get("법령명한글")),
                "authority": _text(row.get("소관부처명")),
                "document_kind": _text(row.get("법령구분명")),
                "effective_date": _date(row.get("시행일자")),
                "promulgation_date": _date(row.get("공포일자")),
                "status": _text(row.get("현행연혁코드")),
                "detail_link": _text(row.get("법령상세링크")),
            }
            for row in rows
        ]
    if target == "admrul":
        root = payload.get("AdmRulSearch", {})
        rows = _list(root.get("admrul"))
        return [
            {
                "source_type": "admrul",
                "official_id": str(row.get("행정규칙ID", "")),
                "version_id": str(row.get("행정규칙일련번호", "")),
                "title": _text(row.get("행정규칙명")),
                "authority": _text(row.get("소관부처명")),
                "document_kind": _text(row.get("행정규칙종류")),
                "effective_date": _date(row.get("시행일자")),
                "promulgation_date": _date(row.get("발령일자")),
                "status": _text(row.get("현행연혁구분")),
                "detail_link": _text(row.get("행정규칙상세링크")),
            }
            for row in rows
        ]
    raise ValueError(f"지원하지 않는 target: {target}")


def _law_provisions(root: dict[str, Any]) -> list[dict[str, Any]]:
    provisions: list[dict[str, Any]] = []
    units = _list(root.get("조문", {}).get("조문단위"))
    for unit in units:
        if not isinstance(unit, dict):
            continue
        number = _text(unit.get("조문번호"))
        branch = _text(unit.get("조문가지번호"))
        path = f"제{number}조" + (f"의{branch}" if branch and branch != "0" else "")
        kind = "article" if unit.get("조문여부") == "조문" else "heading"
        provisions.append(
            {
                "provision_path": path,
                "kind": kind,
                "text": _text(unit.get("조문내용")),
                "effective_date": _date(unit.get("조문시행일자")),
            }
        )
        for p_idx, paragraph in enumerate(_list(unit.get("항")), start=1):
            if not isinstance(paragraph, dict):
                continue
            p_path = f"{path} 제{p_idx}항"
            provisions.append(
                {
                    "provision_path": p_path,
                    "kind": "paragraph",
                    "text": _text(paragraph.get("항내용")),
                    "effective_date": _date(unit.get("조문시행일자")),
                }
            )
            for i_idx, item in enumerate(_list(paragraph.get("호")), start=1):
                if not isinstance(item, dict):
                    continue
                i_path = f"{p_path} 제{i_idx}호"
                provisions.append(
                    {
                        "provision_path": i_path,
                        "kind": "item",
                        "text": _text(item.get("호내용")),
                        "effective_date": _date(unit.get("조문시행일자")),
                    }
                )
                for s_idx, subitem in enumerate(_list(item.get("목")), start=1):
                    if not isinstance(subitem, dict):
                        continue
                    provisions.append(
                        {
                            "provision_path": f"{i_path} 제{s_idx}목",
                            "kind": "subitem",
                            "text": _text(subitem.get("목내용")),
                            "effective_date": _date(unit.get("조문시행일자")),
                        }
                    )
    addenda = root.get("부칙", {}).get("부칙단위")
    for idx, addendum in enumerate(_list(addenda), start=1):
        if not isinstance(addendum, dict):
            continue
        provisions.append(
            {
                "provision_path": f"부칙 {idx}",
                "kind": "addendum",
                "text": _text(addendum.get("부칙내용")),
                "effective_date": _date(addendum.get("부칙공포일자")),
            }
        )
    return [item for item in provisions if item["text"]]


def _admrul_provisions(root: dict[str, Any], effective_date: str | None) -> list[dict[str, Any]]:
    provisions: list[dict[str, Any]] = []
    for idx, raw in enumerate(_list(root.get("조문내용")), start=1):
        text = _text(raw)
        match = re.match(r"제(\d+)조(?:의(\d+))?", text)
        path = f"제{match.group(1)}조" if match else f"조문 {idx}"
        if match and match.group(2):
            path += f"의{match.group(2)}"
        provisions.append(
            {
                "provision_path": path,
                "kind": "article",
                "text": text,
                "effective_date": effective_date,
            }
        )
    addendum = root.get("부칙")
    if addendum:
        provisions.append(
            {
                "provision_path": "부칙",
                "kind": "addendum",
                "text": _text(addendum.get("부칙내용") if isinstance(addendum, dict) else addendum),
                "effective_date": _date(addendum.get("부칙공포일자"))
                if isinstance(addendum, dict)
                else effective_date,
            }
        )
    return [item for item in provisions if item["text"]]


def parse_detail(target: str, payload: dict[str, Any], *, version_id: str = "") -> dict[str, Any]:
    if target == "law":
        root = payload.get("법령", {})
        info = root.get("기본정보", {})
        official_id = str(info.get("법령ID", ""))
        effective_date = _date(info.get("시행일자"))
        version_id = version_id or str(root.get("법령키", ""))
        return {
            "source_type": "law",
            "official_id": official_id,
            "version_id": version_id,
            "title": _text(info.get("법령명_한글")),
            "authority": _text(info.get("소관부처")),
            "document_kind": _text(info.get("법종구분")),
            "promulgation_date": _date(info.get("공포일자")),
            "effective_date": effective_date,
            "status": "현행",
            "official_url": _official_url("law", official_id=official_id, version_id=version_id),
            "provisions": _law_provisions(root),
        }
    if target == "admrul":
        root = payload.get("AdmRulService", {})
        info = root.get("행정규칙기본정보", {})
        official_id = str(info.get("행정규칙ID", ""))
        version_id = version_id or str(info.get("행정규칙일련번호", ""))
        effective_date = _date(info.get("시행일자"))
        return {
            "source_type": "admrul",
            "official_id": official_id,
            "version_id": version_id,
            "title": _text(info.get("행정규칙명")),
            "authority": _text(info.get("소관부처명")),
            "document_kind": _text(info.get("행정규칙종류")),
            "promulgation_date": _date(info.get("발령일자")),
            "effective_date": effective_date,
            "status": "현행" if info.get("현행여부") == "Y" else "연혁",
            "official_url": _official_url("admrul", official_id=official_id, version_id=version_id),
            "provisions": _admrul_provisions(root, effective_date),
        }
    raise ValueError(f"지원하지 않는 target: {target}")
