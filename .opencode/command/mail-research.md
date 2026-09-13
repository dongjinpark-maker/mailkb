---
# opencode 에서 `/이름` 은 스킬이 아니라 command 다. 이 파일이 `/mail-research` 를 만든다.
# 본문은 .claude/skills/mail-research/SKILL.md 와 같은 내용이다. 다른 곳은 둘 — 질문 줄($ARGUMENTS),
# 실행 줄(opencode 는 Windows PowerShell 경유가 전제라 <PYTHON> 대신 그 형식).
# 스킬을 부르게 하면 매번 도구 호출이 한 번 더 든다 — 바꾸는 건 가끔, 쓰는 건 자주라 복사를 택했다.
# SKILL.md 를 고치면 여기도 같이 고친다.
description: 저장된 메일에서 근거 달린 답과 상황 브리핑을 만든다
agent: mailkb
---
# 메일 조사·심화 분석

조사할 질문: $ARGUMENTS

**확보는 mailkb 조회 명령으로, 분석은 이 세션이 직접 한다** — 엔진(`ask`)의 고정
파이프라인을 넘는 분석이 이 스킬의 존재 이유다.

**`agent-guides/minerva-researcher.md` 를 읽고 그 계약대로 조사한다** — 역할 분담,
조사 절차, 인용·기한 규율, 답변 형식, 보존(노트/지식)이 전부 거기 있다. 명령
옵션·검색 DSL·출력 형태가 불확실하면 `agent-guides/minerva-cli-reference.md` 를
본다.

실행은 저장소 루트에서 PowerShell 로 한다. 결과는 파일로 받고 `utf-8-sig` 로 읽는다.
`powershell.exe -NoProfile -Command "chcp 65001 | Out-Null; python -m mailkb <명령> | Out-File -Encoding utf8 temp\out.txt; exit \$LASTEXITCODE"`
