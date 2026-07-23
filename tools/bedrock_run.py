#!/usr/bin/env python3
"""mailkb AI 백엔드 — Claude in Amazon Bedrock 어댑터.

ai_run 계약: 프롬프트를 stdin(utf-8)으로 받아 응답 텍스트만 stdout 으로 쓴다.
실패는 비0 종료 + stderr(호출부가 AIError 로 승격해 자동 재시도). anthropic 은
이 스크립트만 의존한다 — mailkb 코어(stdlib-only)는 무변경.

설치:   pip install -U "anthropic[bedrock]"
등록:   data/config.toml 예시
        [ai.backends.bedrock-sonnet]
        cmd = ["python", "tools/bedrock_run.py", "--model", "anthropic.claude-sonnet-5"]
        [ai.backends.bedrock-haiku]
        cmd = ["python", "tools/bedrock_run.py", "--model", "anthropic.claude-haiku-4-5"]
리전:   --region > AWS_REGION > 기본 ap-northeast-2(서울).
        config 오버라이드 = cmd 에 "--region", "<리전>" 을 덧붙인다.
자격증명: 표준 AWS 체인(환경변수 → 프로필/SSO → 역할). SSO 는 `aws sso login` 선행.
"""
from __future__ import annotations

import argparse
import os
import sys

DEFAULT_REGION = "ap-northeast-2"           # 서울 — config(cmd --region)로 오버라이드
DEFAULT_MODEL = "anthropic.claude-sonnet-5"

# 오류 문구에 이게 보이면 자격증명 문제일 가능성이 높다 — SSO 만료가 흔한 원인.
_CRED_HINTS = ("credential", "token", "expired", "sso",
               "unauthorized", "accessdenied", "access denied")


def resolve_region(cli_region: str | None, env: dict | None = None) -> str:
    """--region > AWS_REGION > 기본(서울). 빈 문자열은 미지정으로 취급."""
    e = os.environ if env is None else env
    return cli_region or e.get("AWS_REGION") or DEFAULT_REGION


def _make_client(region: str):
    """지연 임포트 — anthropic 미설치 환경에서도 모듈 임포트·테스트가 가능하게."""
    from anthropic import AnthropicBedrockMantle
    return AnthropicBedrockMantle(aws_region=region)


def main(argv: list[str] | None = None) -> int:
    # Windows 콘솔/파이프 기본 인코딩(cp949)은 메일 본문의 이모지에서 죽는다 —
    # 부모(ai_run)는 utf-8 로 읽으므로 자식도 utf-8 로 맞춘다.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="mailkb Bedrock 백엔드 어댑터")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--region", default=None,
                    help=f"기본: AWS_REGION 또는 {DEFAULT_REGION}")
    ap.add_argument("--max-tokens", type=int, default=4096)
    args = ap.parse_args(argv)

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("빈 프롬프트", file=sys.stderr)
        return 2

    region = resolve_region(args.region)
    try:
        client = _make_client(region)
        msg = client.messages.create(
            model=args.model,
            max_tokens=args.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except ModuleNotFoundError:
        print('anthropic 미설치 — pip install -U "anthropic[bedrock]"',
              file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 비0 종료 + 원인이 계약
        # APIConnectionError 류는 진짜 원인(SSL·DNS·프록시)이 안에 감싸여 있다 —
        # 예외 체인을 펼쳐야 사내망에서 원인을 특정할 수 있다.
        chain, cur = [], e
        while cur is not None and len(chain) < 5:
            chain.append(f"{type(cur).__name__}: {cur}")
            cur = cur.__cause__ or cur.__context__
        print(f"Bedrock 호출 실패({region}): " + " <- ".join(chain),
              file=sys.stderr)
        low = " ".join(chain).lower()
        if any(k in low for k in _CRED_HINTS):
            print("자격증명 문제로 보임 — aws sso login(또는 키/프로필 설정) 후 재시도",
                  file=sys.stderr)
        if "ssl" in low or "certificate" in low:
            print("사내 TLS 검사(프록시 CA) 가능성 — 회사 CA 번들을 "
                  "SSL_CERT_FILE 환경변수로 지정", file=sys.stderr)
        elif any(k in low for k in ("connect", "getaddrinfo", "timed out",
                                    "timeout", "proxy", "unreachable")):
            print("네트워크 경로 문제 — ① HTTPS_PROXY 환경변수(사내 프록시) "
                  "② 방화벽의 *.api.aws 허용 여부 확인 "
                  "(claude CLI 가 되는 것과 별개 — 호스트·프록시 경로가 다름)",
                  file=sys.stderr)
        return 1

    text = "".join(getattr(b, "text", "") for b in msg.content
                   if getattr(b, "type", "") == "text")
    if not text.strip():
        print("응답에 텍스트 블록 없음", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    u = getattr(msg, "usage", None)     # 진단용 — 성공 시 mailkb 는 stdout 만 읽는다
    if u is not None:
        print(f"usage: in={getattr(u, 'input_tokens', '?')} "
              f"out={getattr(u, 'output_tokens', '?')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
