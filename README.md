# Fire Inspection Regulatory MCP

국가법령정보센터 공식 API를 동기화하여 소방·건축 법령, NFPC/NFTC, 형식승인·제품검사 기술기준을 기준일과 함께 검색하는 로컬 읽기 중심 MCP입니다.

## 현재 제공 기능

- 공식 법령·행정규칙 JSON 원문 스냅샷과 SHA-256 보존
- 조·항·호·목·부칙 구조화
- SQLite FTS5 및 한국어 부분일치 보완검색
- 기준일 폐구간 필터(`effective_from ≤ as_of < effective_to`)
- 신규 시행본·동일 시행본 해시 변경의 승인대기 격리
- 승인 전 마지막 승인 운영뷰 유지 및 검토 감사기록
- 단서·제외·경과조치 후보 검색
- 공식 URL, 시행일, 소관기관, 문서 버전 반환
- FastMCP stdio 서버

도구:

- `search_current_rules`
- `get_rule_as_of`
- `trace_exception_path`
- `get_source_status`
- `list_pending_changes`

Resources:

- `firelaw://catalog`
- `firelaw://documents/{official_id}`

Prompt:

- `investigate_fire_requirement`

## 설치 및 테스트

필수 조건: Python 3.11 이상과 [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/119gpt333-sys/fire-regulations-mcp.git
cd fire-regulations-mcp
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run python scripts/verify_mcp.py
```

처음 설치한 저장소에는 동기화 데이터가 포함되지 않습니다. 아래 공식 자료 동기화를 실행하면
`data/raw` 원문 스냅샷과 `data/index/fire_regulations.db` 검색 인덱스가 로컬에 생성됩니다.

## 공식 자료 동기화

샘플 확인:

```bash
uv run fire-mcp-sync --max-per-entry 3
```

전체 등록 자료:

```bash
export LAW_API_OC='공동활용에서_발급받은_인증값'
uv run fire-mcp-sync
```

`OC=test`로도 공식 샘플 호출은 가능하지만 운영용이 아닙니다. 운영 전 국가법령정보 공동활용 신청 후 인증값을 환경변수로 전달하십시오.

신규 시행본과 원문 해시 변경은 자동으로 운영뷰에 반영되지 않습니다. 동기화 후 검토 대기열을 확인하고,
법규 담당자가 공식 원문을 대조한 뒤 로컬 검토 CLI에서 승인·반려합니다.

```bash
uv run fire-mcp-review list
uv run fire-mcp-review approve \
  --source-type admrul --official-id '<공식ID>' --version-id '<시행본ID>' \
  --reviewer '<검토자>' --reason '<공식 원문 대조 근거>'
# 반려는 approve 대신 reject
```

승인 전에는 마지막 승인본이 계속 검색되며, 동일 시행본의 해시 변경도 후보 원문을 별도로 보존해
승인 전 운영검색 결과를 덮어쓰지 않습니다. 검토자·결정·사유는 `review_events`에 기록됩니다.

`OC`, 검토자 권한, 이중승인 정책은 소스코드가 아니라 운영 환경과 별도 승인 웹앱에서 관리해야 합니다.

수집대상은 `config/source_registry.json`에서 관리합니다. 원문은 `data/raw`, 검색 DB는
`data/index/fire_regulations.db`에 저장됩니다.

## Hermes 연결

로컬 체크아웃을 직접 연결하려면:

```bash
hermes mcp add fire-regulations \
  --command "$(pwd)/run_mcp.sh"
hermes mcp test fire-regulations
```

Hermes 공식 카탈로그에 병합된 뒤에는 다음 한 줄로 설치할 수 있습니다.

```bash
hermes mcp install fire-regulations
```

새 대화에서는 도구가 `mcp_fire_regulations_*` 형태로 노출됩니다.

## 안전 경계

이 서버는 개별 시설의 적법·부적합을 자동 확정하지 않습니다. 다음 사실을 담당자가 확인해야 합니다.

- 건축허가·사용승인·용도변경 시점
- 건축물 용도·면적·층수·수용인원
- 설비 종류·승인도면·현장 상태
- 부칙·경과조치·예외·상위법 위임범위

## 현재 MVP의 한계

- 초기 DB는 2026-07-25 실행 시점의 현행 검색결과 중심입니다.
- 연혁 전수수집과 구조화 신구 diff는 다음 단계입니다.
- NFPC/NFTC·형식승인 기준 전체 범위는 운영 OC로 전체 동기화해야 합니다.
- 변경 승인용 내부 웹앱은 아직 포함하지 않습니다.
- KFI 개별 제품의 승인상태 데이터는 별도 공개 API·이용조건 확인이 필요합니다.

## 라이선스

소프트웨어 소스코드는 [MIT License](LICENSE)로 배포합니다.
동기화되는 법령·행정규칙 원문 데이터의 이용조건과 출처 표시는 해당 공식 제공기관의
정책을 따르며, 이 저장소의 MIT 라이선스가 외부 원문 데이터에 적용된다는 의미는 아닙니다.
