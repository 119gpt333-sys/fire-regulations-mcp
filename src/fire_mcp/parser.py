from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse


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


_CIRCLED_NUMBERS = {char: index for index, char in enumerate("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳", 1)}


def _number_marker(value: Any) -> str | None:
    text = _text(value)
    if text in _CIRCLED_NUMBERS:
        return str(_CIRCLED_NUMBERS[text])
    match = re.search(r"\d+", text)
    return match.group(0) if match else None


def _subitem_marker(value: Any) -> str | None:
    text = re.sub(r"[.．、)\s]", "", _text(value))
    if not text:
        return None
    if text.isdigit():
        return f"제{text}"
    return text


def _is_sensitive_query_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    if normalized in {"oc", "auth", "authorization", "credential", "credentials", "key"}:
        return True
    return any(
        marker in normalized
        for marker in (
            "apikey",
            "accesskey",
            "privatekey",
            "token",
            "secret",
            "password",
            "passwd",
            "signature",
            "session",
            "authorization",
            "credential",
            "bearer",
            "jwt",
            "cookie",
        )
    )


def _download_url(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if text.startswith(("http://law.go.kr/", "http://www.law.go.kr/")):
        text = "https://www.law.go.kr/" + text.split("/", 3)[-1]
    url = urljoin("https://www.law.go.kr/", text)
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"law.go.kr", "www.law.go.kr"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return ""
    public_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_sensitive_query_key(key)
    ]
    return parsed._replace(query=urlencode(public_query, doseq=True), fragment="").geturl()


def _annexes(root: dict[str, Any], effective_date: str | None) -> list[dict[str, Any]]:
    annex_root = root.get("별표")
    if not isinstance(annex_root, dict):
        return []
    annexes: list[dict[str, Any]] = []
    for unit in _list(annex_root.get("별표단위")):
        if not isinstance(unit, dict):
            continue
        raw_number = _text(unit.get("별표번호"))
        number = str(int(raw_number)) if raw_number.isdigit() else raw_number
        annex_key = _text(unit.get("별표키"))
        raw_branch = _text(unit.get("별표가지번호"))
        branch = str(int(raw_branch)) if raw_branch.isdigit() and int(raw_branch) else ""
        annex_kind = _text(unit.get("별표구분")) or "별표"
        if number:
            path = f"{annex_kind} {number}" + (f"의{branch}" if branch else "")
        elif annex_key:
            path = f"{annex_kind} (번호없음·키 {annex_key})"
        else:
            path = f"{annex_kind} (번호없음)"
        title = _text(unit.get("별표제목"))
        text = _text(unit.get("별표내용"))
        file_links: list[dict[str, str]] = []
        link_specs = (
            ("pdf", "별표서식PDF파일링크", "별표PDF파일명"),
            ("hwp", "별표서식파일링크", "별표HWP파일명"),
            ("image", "별표서식이미지파일링크", "별표이미지파일명"),
        )
        for file_kind, link_key, name_key in link_specs:
            links = _list(unit.get(link_key))
            names = _list(unit.get(name_key))
            for link_index, link in enumerate(links):
                url = _download_url(link)
                if not url:
                    continue
                name = _text(names[link_index]) if link_index < len(names) else ""
                file_links.append({"kind": file_kind, "name": name, "url": url})
        annexes.append(
            {
                "annex_key": _text(unit.get("별표키")),
                "provision_path": path,
                "kind": annex_kind,
                "title": title,
                "text": text,
                "effective_date": effective_date,
                "file_links": file_links,
            }
        )
    return annexes


def _attachments(root: dict[str, Any]) -> list[dict[str, str]]:
    raw = root.get("첨부파일")
    if not isinstance(raw, dict):
        return []
    names = _list(raw.get("첨부파일명"))
    links = _list(raw.get("첨부파일링크"))
    attachments: list[dict[str, str]] = []
    for index, link in enumerate(links):
        url = _download_url(link)
        if not url:
            continue
        name = _text(names[index]) if index < len(names) else ""
        attachments.append({"name": name, "url": url})
    return attachments


def parse_search_results(target: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if target in {"law", "eflaw"}:
        root = payload.get("LawSearch", {})
        rows = _list(root.get("law"))
        return [
            {
                "source_type": "law",
                "api_target": target,
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
        kind = "article" if unit.get("조문여부") == "조문" else "heading"
        if number:
            path = f"제{number}조" + (f"의{branch}" if branch and branch != "0" else "")
        else:
            path = "번호없음 조문" if kind == "article" else "번호없음 제목"
        provisions.append(
            {
                "provision_path": path,
                "kind": kind,
                "text": _text(unit.get("조문내용")),
                "effective_date": _date(unit.get("조문시행일자")),
            }
        )
        for paragraph in _list(unit.get("항")):
            if not isinstance(paragraph, dict):
                continue
            paragraph_text = _text(paragraph.get("항내용"))
            paragraph_number = _number_marker(paragraph.get("항번호"))
            p_path = f"{path}제{paragraph_number}항" if paragraph_number else path
            if paragraph_text:
                provisions.append(
                    {
                        "provision_path": p_path,
                        "kind": "paragraph",
                        "text": paragraph_text,
                        "effective_date": _date(unit.get("조문시행일자")),
                    }
                )
            for item in _list(paragraph.get("호")):
                if not isinstance(item, dict):
                    continue
                item_number = _number_marker(item.get("호번호"))
                item_branch = _number_marker(item.get("호가지번호"))
                if item_number:
                    branch_suffix = f"의{item_branch}" if item_branch else ""
                    i_path = f"{p_path}제{item_number}호{branch_suffix}"
                else:
                    i_path = p_path
                provisions.append(
                    {
                        "provision_path": i_path,
                        "kind": "item",
                        "text": _text(item.get("호내용")),
                        "effective_date": _date(unit.get("조문시행일자")),
                    }
                )
                for subitem in _list(item.get("목")):
                    if not isinstance(subitem, dict):
                        continue
                    subitem_marker = _subitem_marker(subitem.get("목번호"))
                    subitem_path = f"{i_path}{subitem_marker}목" if subitem_marker else i_path
                    provisions.append(
                        {
                            "provision_path": subitem_path,
                            "kind": "subitem",
                            "text": _text(subitem.get("목내용")),
                            "effective_date": _date(unit.get("조문시행일자")),
                        }
                    )
    addenda = root.get("부칙", {}).get("부칙단위")
    for addendum in _list(addenda):
        if not isinstance(addendum, dict):
            continue
        provisions.append(
            {
                "provision_path": "부칙",
                "kind": "addendum",
                "text": _text(addendum.get("부칙내용")),
                "effective_date": _date(addendum.get("부칙공포일자")),
            }
        )
    return [item for item in provisions if item["text"]]


def _admrul_provisions(root: dict[str, Any], effective_date: str | None) -> list[dict[str, Any]]:
    provisions: list[dict[str, Any]] = []
    for raw in _list(root.get("조문내용")):
        text = _text(raw)
        match = re.match(r"제(\d+)조(?:의(\d+))?", text)
        path = f"제{match.group(1)}조" if match else "번호없음 조문"
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
        annexes = _annexes(root, effective_date)
        provisions = _law_provisions(root)
        provisions.extend(
            {
                "provision_path": annex["provision_path"],
                "kind": "annex",
                "text": " ".join(filter(None, (annex["title"], annex["text"]))),
                "effective_date": annex["effective_date"],
            }
            for annex in annexes
            if annex["text"] or annex["title"]
        )
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
            "provisions": provisions,
            "annexes": annexes,
            "attachments": _attachments(root),
        }
    if target == "admrul":
        root = payload.get("AdmRulService", {})
        info = root.get("행정규칙기본정보", {})
        official_id = str(info.get("행정규칙ID", ""))
        version_id = version_id or str(info.get("행정규칙일련번호", ""))
        effective_date = _date(info.get("시행일자"))
        annexes = _annexes(root, effective_date)
        provisions = _admrul_provisions(root, effective_date)
        provisions.extend(
            {
                "provision_path": annex["provision_path"],
                "kind": "annex",
                "text": " ".join(filter(None, (annex["title"], annex["text"]))),
                "effective_date": annex["effective_date"],
            }
            for annex in annexes
            if annex["text"] or annex["title"]
        )
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
            "provisions": provisions,
            "annexes": annexes,
            "attachments": _attachments(root),
        }
    raise ValueError(f"지원하지 않는 target: {target}")
