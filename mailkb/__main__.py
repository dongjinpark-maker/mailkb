"""python -m mailkb 진입점.

**이 파일은 구 Python 에서도 파싱·실행되어야 한다.** 3.11 미만에서는 config 가
`import tomllib` 에서 죽어 doctor 의 버전 안내에 닿지도 못한다 — 그래서 안내를
import 앞으로 끌어올린다. f-string 외의 새 문법을 여기에 쓰지 않는다.
"""

import sys

if sys.version_info < (3, 11):
    sys.exit(
        "mailkb 는 Python 3.11 이상이 필요합니다 (현재 %d.%d).\n"
        "  표준 라이브러리 tomllib 로 설정을 읽습니다.\n"
        "  python.org 에서 3.11+ 를 설치하고 'Add to PATH' 를 켜세요."
        % sys.version_info[:2])

from .cli import main  # noqa: E402 — 위 버전 가드가 먼저 돌아야 한다

main()
