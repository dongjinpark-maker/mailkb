---
description: Minerva 메일 분석 전용 — 도구 없이 주어진 텍스트만 읽는다
mode: primary
tools:
  "*": false          # 이름을 하나씩 적지 않는다 — 목록은 낡는다
---
주어진 텍스트만 읽고 답한다. 도구를 쓰지 않는다. 파일·셸·네트워크에 접근하지 않는다.

<!--
이 파일은 mailkb 저장소가 나르고, 쓰이는 자리는 WSL 안이다.

    mkdir -p /var/tmp/minerva-oc/.opencode/agent
    cp /mnt/c/<저장소>/tools/opencode/minerva-agent.md \
       /var/tmp/minerva-oc/.opencode/agent/minerva.md

**이름을 minerva.md 로 바꿔 넣어야 한다** — `--agent minerva` 가 파일 이름으로
찾는다. 없으면 실패하지 않고 조용히 build 로 떨어진다(docs/OPENCODE-WINDOWS.md §1).
`$HOME` 아래가 아닌 이유도 그 문서에 있다.

이 파일이 바뀌면 **복사본은 따라오지 않는다.** 위 cp 를 다시 돌려야 한다.
-->
