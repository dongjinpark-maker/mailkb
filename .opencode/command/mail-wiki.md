---
# opencode 에서 `/이름` 은 스킬이 아니라 command 다. 이 파일이 `/mail-wiki` 를 만든다.
# 본문은 .claude/skills/mail-wiki/SKILL.md 와 같은 내용이다. 다른 곳은 둘 — 인자 줄($ARGUMENTS),
# 실행 줄(opencode 는 Windows PowerShell 경유가 전제라 <PYTHON> 대신 그 형식).
# SKILL.md 를 고치면 여기도 같이 고친다.
description: 등록한 주제의 메일 위키 페이지를 만들고 갱신한다
agent: mailkb
---
# 메일 주제 위키

인자: $ARGUMENTS

인자의 첫 낱말이 작업이다.

| 인자 | 작업 |
|---|---|
| (없음) · `status` | 주제마다 새 메일·후보가 얼마나 쌓였는지 보여 준다 |
| `new <주제> [별칭 a,b]` | 새 주제 페이지를 만든다 |
| `update <주제>` · `update all` | 마지막 반영 뒤에 온 메일로 갱신한다 |
| `add <주제> <스레드 번호…>` | 그 스레드를 주제에 넣는다 |
| `drop <주제> <스레드 번호…>` | 그 스레드를 주제에서 뺀다 |
| `freeze <주제>` · `unfreeze <주제>` | 끝난 주제를 동결한다 / 푼다 — 동결된 주제는 갱신하지 않는다 |
| `feedback` | 회사 내용이 없는 자가 평가 글을 만든다(밖으로 보내 개선에 쓰기 위한 것) |

작업 낱말 없이 주제만 오면: 페이지가 있으면 `update`, 없으면 `new` 로 본다.

**`agent-guides/minerva-wiki.md` 를 읽고 그 계약대로 한다** — 페이지 규약, 경위 줄 형식,
매듭지어짐 기준, 절차, 동결, 작업 메모, feedback 이 전부 거기 있다. 명령 옵션이 불확실하면
`agent-guides/minerva-cli-reference.md` 의 `wiki` 절을 본다.

**찾기(`wiki scan`)와 검증(`wiki check`)은 코드가 하고, 이 세션은 읽고 쓴다.** 둘 다 AI 호출이
없다. 쓰는 곳은 `<HOME>/vault/wiki/` 뿐이다.

실행은 저장소 루트에서 PowerShell 로 한다. 결과는 파일로 받고 `utf-8-sig` 로 읽는다.
`powershell.exe -NoProfile -Command "chcp 65001 | Out-Null; python -m mailkb wiki <명령> | Out-File -Encoding utf8 temp\out.txt; exit \$LASTEXITCODE"`
