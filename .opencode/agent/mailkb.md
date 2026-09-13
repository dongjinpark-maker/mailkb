---
# 저장소 루트에서 `opencode --agent mailkb`. 설정은 시작 때 한 번 읽는다 — 고치면 재시작.
# `---` 아래 본문만 모델로 간다. 프로젝트 설명·규칙은 CLAUDE.md 가 자동 적재되므로 여기 적지 않는다.
# permission 은 마지막에 맞는 규칙이 이긴다 — 넓은 것 먼저, 좁은 것 나중(opencode 내장 문서).
# Windows 파이썬(PowerShell 경유)인 이유: WSL 의 python3 로 Windows 쪽 실 DB(WAL)를 열면 disk I/O error 다(실측 12/12).
# WSL↔Windows 경계를 WAL 의 -shm 이 못 넘는다. Linux 네이티브 실행은 이 저장소의 시나리오가
# 아니라 python3 분기는 적지 않는다 — 본문은 콜마다 실려 간다.
# permission.bash 에 allow 를 섞으면 tools 차단이 무효가 된다(실측) — 여기는 deny 만 둔다.
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
    # WSL 파이썬이 Windows 쪽 실 DB(WAL)를 열면 `disk I/O error` 가 난다(실측 12/12).
    "*sqlite*": deny
  # 시스템 임시 자리에 쓰지 않는다 — 스크래치는 저장소 안 .opencode/tmp/ 하나로 몬다.
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

DB 파일(`*.sqlite`)은 직접 열지 않는다. mailkb 는 아래 PowerShell 형식으로 돌린다.
데모로 시험할 때는 하위 명령 앞에 `--home demo` 를 붙인다. 없으면 실제 데이터(`data/`)다.
메일 내용을 조사하는 질문은 `mail-research` 스킬을 쓴다.

임시 파일·스크립트·중간 산출물은 저장소 루트의 `temp/` 에만 쓴다(없으면 만든다). 그 안은
언제든 지워질 수 있으니 남겨야 할 것은 두지 않는다.

WSL 에서 Windows 도구를 부르면 출력이 cp949 다.
- PowerShell: `chcp 65001 | Out-Null;` 로 시작하고 결과를 **파일로** 받는다
  (`| Out-File -Encoding utf8 temp\x.txt`). stdout 으로 직접 받으면 cp949 로 깨진다.
  그 파일은 BOM 이 붙으니 읽을 때 `utf-8-sig`. `>` 리다이렉트는 UTF-16 이라 쓰지 않는다.
  실패를 알려면 끝에 `; exit \$LASTEXITCODE` 를 붙인다. bash 에서 부르므로 `$` 앞에 `\`.
  예: `powershell.exe -NoProfile -Command "chcp 65001 | Out-Null; python -m mailkb ls | Out-File -Encoding utf8 temp\ls.txt; exit \$LASTEXITCODE"`
- 그 밖의 `*.exe`: 받아서 `iconv -f cp949 -t utf-8`, 파이썬이면 `.decode("cp949","replace")`.
