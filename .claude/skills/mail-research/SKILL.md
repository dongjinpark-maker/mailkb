---
name: mail-research
description: 저장된 업무 메일에서 근거가 달린 답과 상황 브리핑을 만든다. "그 결정 뭐였지", "이 사람과 지금 뭐가 걸려 있지", "이 스레드 정리해 줘" 류의 질문에 쓴다. mailkb(Minerva) 저장소에서만 동작한다.
---

# 메일 조사·심화 분석

스킬 인자로 받은 텍스트가 조사할 질문이다.

**확보는 mailkb 조회 명령으로, 분석은 이 세션이 직접 한다** — 엔진(`ask`)의 고정
파이프라인을 넘는 분석이 이 스킬의 존재 이유다.

**`agent-guides/minerva-researcher.md` 를 읽고 그 계약대로 조사한다** — 역할 분담,
조사 절차, 인용·기한 규율, 답변 형식, 보존(노트/지식)이 전부 거기 있다. 명령
옵션·검색 DSL·출력 형태가 불확실하면 `agent-guides/minerva-cli-reference.md` 를
본다.

실행은 저장소 루트에서 `<PYTHON> -m mailkb`(Windows `python`, Linux/WSL `python3`).
