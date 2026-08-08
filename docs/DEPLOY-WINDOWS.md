# 회사 PC 배포 (Windows + 클래식 Outlook)

README의 짧은 설치 절차를 실제로 따라 할 때 필요한 세부. 사내망 설정 항목,
자동 실행 등록, 아이콘 실행, 첫 환경 점검 목록을 담는다.

## 1. 준비

1. **Windows 네이티브 Python 3.11+** 설치 — WSL로는 안 된다(COM 접근 필요).
   3.11 이상인 이유는 표준 라이브러리 `tomllib`를 쓰기 때문이다.
2. `pip install pywin32` — 코어에서 유일하게 필요한 외부 패키지다.
   프록시 뒤라면 `pip install --proxy http://<프록시>:<포트> pywin32`.
3. **AI 기능을 쓸 것이면 CLI 하나가 PATH에 있어야 한다.** 내장 백엔드 이름
   `sonnet`/`haiku`/`opus`는 `claude -p --model <이름>`을, `internal`은
   `opencode run`을 부른다(`config._BUILTIN_BACKENDS`). 사내 CLI를 쓴다면
   §2의 `[ai.backends.*] cmd`로 지정한다. **없어도 AI 기능만 빠지고 나머지는
   전부 동작한다** — 수집·검색·회고·웹 UI는 네트워크 호출이 0이다.
4. 저장소를 받는다. 이후 갱신은 `git pull` 한 번이면 된다.

```
git clone https://github.com/dongjinpark-maker/mailkb
cd mailkb
python -m mailkb init
```

`init`은 데이터 홈을 코드 폴더 옆 `<mailkb>\data\`에 만든다(사용자 홈은 쓰지 않는다).
`data/`는 gitignore라 `git pull`이 실데이터·설정을 건드리지 않는다.

## 2. 사내망 설정 — `<mailkb>\data\config.toml`

**저장소와 코드 안의 값은 전부 가상 예시다.** 실환경 값은 이 파일에만 넣는다.
다른 위치에 두려면 `--home` 또는 환경변수 `MAILKB_HOME`을 쓴다.

| 항목 | 넣을 값 | 왜 중요한가 |
|---|---|---|
| `my_addresses` | 실제 회사 주소 | **메일 별칭(alias)으로도 발신한다면 별칭 주소를 함께 나열한다.** 빠뜨리면 내 발신이 수신으로 오분류된다 |
| `my_names` | 내 이름·호칭 (예: `["홍길동", "길동"]`) | 본문이 나를 지목했는지 판정한다. 그룹메일에서도 확인 대상으로 유지되므로 미답변·기한 판정의 과탐을 줄이는 핵심 |
| `internal_domains` | 사내 도메인 | 설정하면 외부 도메인 발신을 **전부** 노이즈로 제외한다. 협력사가 있으면 아래 `external_allowlist`를 반드시 함께 채운다 |
| `[filters] external_allowlist` | 추적할 협력사 도메인·주소 | `internal_domains`를 켠 상태에서 예외로 살릴 목록. 예: `["partner.co.kr", "kim@vendor.com"]`. **비워 두면 고객사·협력사 메일이 미답변·기한 판정에서 조용히 사라진다** |
| `[filters] subject_noise_strong` | **실제 사내 시스템의 제목 패턴** (전자결재·알림·설문 등) | 코드 기본값의 `[nflow]`/`[nwork]`는 가상 예시다. 이 단계를 건너뛰면 실환경 결재·알림 필터가 동작하지 않는다 |
| `[review] broadcast_to` | 조직 규모에 맞춘 값 | 기준은 **실무 그룹 메일은 포함되고 팀·전사 공지만 배제되는 값**. 예: 그룹 ~80명·팀 ~400명 조직이면 50 (20~30명 실무 메일은 유지, 전체 발송만 배제) |
| `source` | `"outlook"` | `init`이 이미 이 값으로 만든다. 데모만 `fake` |
| `[ai.backends.*] cmd` | 실제 사내 LLM CLI 호출 형태 | 프롬프트를 stdin으로 받고 응답을 stdout으로 내는 명령이면 무엇이든 된다 |
| `[ai] ask` | 분석 전용 백엔드 | 미설정이면 `[ai] search`를 상속한다. 분석은 한 질문에 **최대 12콜**을 쓰므로, 비용이 걱정되면 검색과 분리해 지정한다. 웹 **설정 › 분석 백엔드**에서도 바꿀 수 있다 |

`[ai] summary_max_days` (기본 **1** — 오늘만): 누적 요약과 결정 수확이 소급하는
날짜 창. 며칠 건너뛴 뒤 그 사이의 결정까지 수확하려면 2~3으로 올린다. 늘린 만큼
AI 입력이 늘어난다.

그 외 `[review]` 항목:

- `stall_workdays` (기본 2) — 내가 넘긴 공이 안 돌아온 기준, **영업일**
- `stale_workdays` (기본 3) — 멈춘 스레드 기준, **영업일**
- `direct_to` (기본 4) — 직접 수신 판정 기준 수신자 수
- `holidays` — 대한민국 공휴일 2026(대체 포함)이 기본 내장. **음력 공휴일은 연 1회 갱신**해야 한다

설정은 웹 UI **설정** 화면에서도 바꿀 수 있다. 그쪽 변경은 `overrides.json`에
쌓이므로 `config.toml`은 손상되지 않는다.

## 3. 첫 수집

Outlook을 실행한 상태에서:

```powershell
python -m mailkb sync --since (Get-Date).AddMonths(-6).ToString("yyyy-MM-dd")
```

**첫 수집은 `--since` 로 좁히기를 권한다.** 인물 화면이 최근 6개월 교류를 창으로
쓰므로 6개월이면 모든 화면이 채워지고, 그만큼 첫 실행이 짧아진다. `--since` 없이
돌리면 사서함 전체를 백필하며 크기에 따라 수 분~수십 분 걸린다. 이후는 증분이다.
나중에 더 옛날 것이 필요해지면 더 이른 `--since` 로 한 번 더 돌리면 된다
(이미 있는 메일은 Message-ID 로 건너뛴다).

## 4. 실행

루트 배치 파일(`mailkb.bat`/`sync.bat`)은 2026-07-11 제거했다 — 메일 전송 필터에
걸리는 문제 때문이다. 명령을 직접 쓴다.

```
python -m mailkb serve --app       # Minerva 웹 UI (Edge 앱 모드, 실패 시 기본 브라우저)
python -m mailkb sync              # 증분 수집 (Outlook 실행 상태)
```

### 아이콘으로 실행 (앱처럼)

터미널 없이 바탕화면·작업표시줄 아이콘으로 연다. 저장소의 **`launch_minerva.pyw`**가
클릭 한 번에 ① 떠 있던 서버 종료 ② 서버 시작 ③ Edge 앱 창 열기까지 하고,
**그 창을 닫으면 서버도 함께 내려간다**(콘솔 없음 — pythonw). 재시작·종료 버튼은
없다. 다시 아이콘을 누르면 새 서버로 뜬다.

```
1. launch_minerva.pyw 우클릭 → 바로 가기 만들기 → 바탕화면/작업표시줄에 고정
2. 바로 가기 속성 → 대상을  pythonw.exe "<mailkb>\launch_minerva.pyw"  로,
   아이콘 변경 → 저장소의 minerva.ico 지정
3. (더 앱 같은 설치) 창이 뜬 뒤 Edge 메뉴 → 앱으로 설치 →
   시작 메뉴·바탕화면에 파비콘 아이콘으로 등록
```

창-서버 수명은 **Edge 창 프로세스를 추적**해 묶는다(하트비트·폴링 없음 — CPU 0).
Edge가 없으면 기본 브라우저로 열되 자동 종료는 적용되지 않는다(회사 PC는 Edge 전제).

**최신 코드 반영**은 매 실행마다 하지 않는다(시간 낭비). **설정 › 최신으로 업데이트**
버튼이나 터미널 `git pull` 후 **창을 닫았다 다시 열면** 적용된다.

### 자동 동기화

Windows 작업 스케줄러에 직접 등록한다(등록 스크립트는 제거했다 — 아래 한 줄로 충분).

```
schtasks /Create /TN mailkb-sync /SC HOURLY /MO 2 ^
  /TR "cmd /c cd /d <mailkb 경로> && python -m mailkb sync"
schtasks /Run    /TN mailkb-sync     # 등록 직후 시험 실행 (Outlook 켠 상태 권장)
schtasks /Query  /TN mailkb-sync     # 확인
schtasks /Delete /TN mailkb-sync /F  # 해제
```

데이터 폴더가 코드 기준 고정(`<mailkb>\data`)이라 스케줄러가 다른 작업 디렉토리에서
실행해도 안전하다(빈 DB가 생길 위험 없음).

웹 UI 자체도 열려 있는 동안 주기적으로 동기화한다(설정에서 주기 조절). 즉시
받고 싶으면 상단 **↻** 아이콘을 누른다.

## 5. 신규 환경에서 확인할 것

- [ ] 보안 경고 팝업 여부 → 뜨면: 파일 › 옵션 › 보안 센터 › 프로그래밍 방식 액세스
- [ ] Exchange 주소가 SMTP로 나오는지 (`mailkb ls`에서 발신자 주소 확인)
- [ ] `is_sent` 판별 정상 여부 (`→` 마크) — `my_addresses` 설정과 대조
- [ ] 증분 sync 속도 (`sync` 출력의 소요 시간)
- [ ] `mailkb open <번호>`로 Outlook 원문 열기
- [ ] AI를 쓸 계획이면 `mailkb diagnose --backend <이름>`으로 백엔드 응답 확인

## 6. 백업

데이터 폴더(`<mailkb>\data` — `db.sqlite` + `config.toml` + `vault/`) 복사 한 번이면
된다. 연 200~300MB 수준이다.

## 7. New Outlook 리스크

회사가 `olk.exe`(New Outlook)로 강제 전환하면 COM이 사라진다. 그때는
`mailkb/sources/` 어댑터만 교체하면 된다(EWS/IMAP). 나머지 코드는 원본 접근을
어댑터 뒤에 숨겨 두었으므로 무변경이다.
