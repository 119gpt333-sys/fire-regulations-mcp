# Fire Inspection Regulatory MCP

국가법령정보센터 공식 API를 동기화하여 소방·건축 법령, NFPC/NFTC, 형식승인·제품검사 기술기준을 기준일과 함께 검색하는 로컬 읽기 중심 MCP입니다.

## 현재 제공 기능

- 공식 법령·행정규칙 JSON 원문 스냅샷과 SHA-256 보존
- 법령·행정규칙의 연혁·현행·시행예정 시행본 수집과 기준일 조회
- 공식 항번호·호번호·목번호를 보존한 조·항·호·목·부칙 구조화
- 별표·별지의 표 본문 검색, PDF·HWP·이미지 공식 링크 및 첨부파일 메타데이터 보존
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
  --effective-date '<YYYY-MM-DD>' \
  --change-event-id '<변경이벤트ID>' \
  --reviewer '<검토자>' --reason '<공식 원문 대조 근거>'
# 반려는 approve 대신 reject
```

최소 한 개 이상의 시행본을 공식 원문과 대조해 승인한 뒤 MCP 프로토콜·검색 통합검증을 실행합니다.
동일 시행본 ID에 시행일이 여러 개면 `--effective-date`, 같은 시행일에 원문 후보가 여러 개면
`fire-mcp-review list`에 표시된 `--change-event-id`를 지정합니다.
`rejected`는 동일 후보의 단순 재동기화를 차단합니다. `superseded`는 같은 동기화 실행 안에서는
다시 열리지 않지만 사람의 반려가 아니므로 후속 공식 동기화 실행에서 다시 관측되면 새 검토 후보로 재개방됩니다.

```bash
uv run python scripts/verify_mcp.py
```

승인 전에는 마지막 승인본이 계속 검색되며, 동일 시행본의 해시 변경도 후보 원문을 별도로 보존해
승인 전 운영검색 결과를 덮어쓰지 않습니다. 검토자·결정·사유는 `review_events`에 기록됩니다.

`OC`, 검토자 권한, 이중승인 정책은 소스코드가 아니라 운영 환경과 별도 승인 웹앱에서 관리해야 합니다.

수집대상은 `config/source_registry.json`에서 관리합니다. `all_versions=true` 항목은 공식 검색의
연혁·현행·시행예정 구간을 페이지 끝까지 순회하며, 법령명이 변경된 연혁도 동일 공식 ID로 연결합니다.
원문은 `data/raw`, 검색 DB는 `data/index/fire_regulations.db`에 저장됩니다.
0.1 계열 DB를 처음 열 때는 시행본 키 마이그레이션 전에 같은 디렉터리에
`fire_regulations.db.pre-v0.2.bak` 백업을 자동 생성합니다.

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

- 구조화 신구 diff와 조문별 변경이력 피드는 다음 단계입니다.
- 별표·별지의 공식 본문과 파일 링크는 보존하지만 HWP/PDF 파일 자체의 로컬 다운로드·OCR은 포함하지 않습니다.
- NFPC/NFTC·형식승인 기준 전체 범위는 운영 OC로 전체 동기화해야 합니다.
- 변경 승인용 내부 웹앱은 아직 포함하지 않습니다.
- KFI 개별 제품의 승인상태 데이터는 별도 공개 API·이용조건 확인이 필요합니다.

## 라이선스

소프트웨어 소스코드는 [MIT License](LICENSE)로 배포합니다.
동기화되는 법령·행정규칙 원문 데이터의 이용조건과 출처 표시는 해당 공식 제공기관의
정책을 따르며, 이 저장소의 MIT 라이선스가 외부 원문 데이터에 적용된다는 의미는 아닙니다.
