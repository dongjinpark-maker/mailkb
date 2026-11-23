---
# 저장소 루트에서 `opencode --agent mailkb`. 설정은 시작 때 한 번 읽는다 — 고치면 재시작.
# `---` 아래 본문만 모델로 간다. 프로젝트 설명·규칙은 CLAUDE.md 가 자동 적재되므로 여기 적지 않는다.
# permission 은 마지막에 맞는 규칙이 이긴다 — 넓은 것 먼저, 좁은 것 나중(opencode 내장 문서).
# permission.bash 에 allow 를 섞으면 tools 차단이 무효가 된다(실측) — 여기는 deny 만 둔다.
#
# 2026-11-23 — Windows 에 직접 깐 opencode 가 기본이다. 종전(WSL 안의 opencode)에는
# `python -m mailkb` 를 막고 powershell.exe 로 돌아가게 했는데, Windows 에서는 그 `python` 이
# 바로 맞는 파이썬이라 막으면 안 된다. `python3` 만 막는 것은 옛 방식(WSL 에서 띄운 TUI)의
# 울타리로 남긴다 — WSL 의 python3 가 Windows 디스크의 DB 를 열면 disk I/O error 다(실측 12/12).
# `-X utf8` 은 셸이 무엇이든(cmd·PowerShell·Git Bash) 출력이 UTF-8 로 오게 한다 — 없으면
# 파이프로 받은 한글이 cp949 라 깨진다.
description: mailkb 작업 — 도구 제한 없음, DB 파일 직접 접근만 막는다
mode: primary
permission:
  read:
    "*": allow
    "*.sqlite": deny
    "*.sqlite-wal": deny
    "*.sqlite-shm": deny
  bash:
    "sqlite3 *": deny
    # sqlite 를 언급하는 명령은 통째로 막는다 — `import sqlite3`·`-m sqlite3`·`cat db.sqlite` 류.
    # 웹 서버가 DB 를 늘 열어 두므로 mailkb 를 거치지 않고 열면 잠금이 꼬인다.
    "*sqlite*": deny
    "python3 -m mailkb*": deny
  # 시스템 임시 자리에 쓰지 않는다 — 스크래치는 저장소 루트의 temp/ 하나로 몬다.
  # bash 로 쓰는 것까지는 못 막는다(울타리). 저장소 안 다른 경로는 그대로 열려 있다.
  edit:
    "*": allow
    "/tmp/**": deny
    "/var/tmp/**": deny
  write:
    "*": allow
    "/tmp/**": deny
    "/var/tmp/**": deny
---
한국어로 답한다.

mailkb 는 저장소 루트에서 `python -X utf8 -m mailkb <하위 명령>` 으로 실행한다(Windows 파이썬).
`-X utf8` 을 빼면 한글 출력이 깨진다. `python3` 은 쓰지 않는다.
DB 파일(`*.sqlite`)은 직접 열지 않는다 — 웹 서버가 늘 열어 두므로 mailkb 명령으로만 읽는다.
데모로 시험할 때는 하위 명령 앞에 `--home demo` 를 붙인다. 없으면 실제 데이터(`data/`)다.
메일 내용을 조사하는 질문은 `mail-research` 스킬을 쓴다.

임시 파일·스크립트·중간 산출물은 저장소 루트의 `temp/` 에만 쓴다(없으면 만든다). 그 안은
언제든 지워질 수 있으니 남겨야 할 것은 두지 않는다.
