#!/usr/bin/env python3
"""mailkb AI 백엔드 — Amazon Bedrock(레거시 엔드포인트) 어댑터.

ai_run 계약: 프롬프트를 stdin(utf-8)으로 받아 응답 텍스트만 stdout 으로 쓴다.
실패는 비0 종료 + stderr. anthropic 은 이 스크립트만 의존(코어 무변경).

설치:   pip install -U "anthropic[bedrock]"
등록:   data/config.toml 예
        [ai.backends.bedrock-sonnet]
        cmd = ["python","tools/bedrock_run.py","--proxy","http://proxy.corp:8080","--insecure"]

사내망(확인된 조합 — 기본값에 반영):
  · 신 엔드포인트(bedrock-mantle.*.api.aws)는 방화벽 차단 → 레거시
    bedrock-runtime.*.amazonaws.com(AnthropicBedrock). 모델 ID 는 추론 프로파일
    접두사 필요 → 기본값 global.anthropic.claude-sonnet-5.
  · Windows 엔드포인트 보안이 TLS 를 자기 CA 로 재서명 → Python 이 검증 못 함
    → **기본으로 TLS 검증 끔**(사내 MITM 전제, NODE_TLS_REJECT_UNAUTHORIZED=0 등가).
    검증을 켜려면 --secure. 보안 저하 감수(사내 프록시 신뢰 전제).
  · 프록시는 **기본 미설정** — 필요할 때만 --proxy. env(HTTPS_PROXY)가 있으면
    httpx(trust_env)·botocore 가 자체적으로 읽으므로 명시 안 해도 된다.
따라서 기본 config 는 인자 없이 동작:
    cmd = ["python","tools/bedrock_run.py"]
"""
from __future__ import annotations

import argparse
import os
import sys

DEFAULT_MODEL = "global.anthropic.claude-sonnet-5"
DEFAULT_REGION = "ap-northeast-2"           # 서울


def _make_client(region: str, proxy: str | None = None, insecure: bool = True):
    """지연 임포트 — anthropic 미설치 환경에서도 모듈 임포트/테스트가 가능하게.

    insecure(기본 True)면 httpx TLS 검증을 끈다(사내 MITM 재서명 인증서). proxy 는
    주어질 때만 httpx.Client 에 '명시' 지정(커스텀 http_client 에서는 env 가 확실히
    안 먹음 — 실환경 확인). 프록시 미지정 시 httpx 는 trust_env 로 env 프록시를 읽는다."""
    from anthropic import AnthropicBedrock
    kw = {"aws_region": region}
    ckw = {}
    if insecure:
        ckw["verify"] = False
    if proxy:
        ckw["proxy"] = proxy
    if ckw:
        import httpx
        kw["http_client"] = httpx.Client(**ckw)
    return AnthropicBedrock(**kw)


def main(argv: list[str] | None = None) -> int:
    # Windows 콘솔/파이프 기본 인코딩(cp949)은 이모지에서 죽는다 — utf-8 로 맞춘다.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="mailkb Bedrock(레거시) 백엔드 어댑터")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--region", default=None,
                    help=f"기본: AWS_REGION 또는 {DEFAULT_REGION}")
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--proxy", default=None,
                    help="사내 명시 프록시 URL(기본 미설정 — 필요할 때만; "
                         "env HTTPS_PROXY 가 있으면 자동으로 읽힘)")
    ap.add_argument("--secure", action="store_true",
                    help="TLS 검증 켬(기본은 사내 MITM 전제로 검증 끔)")
    args = ap.parse_args(argv)

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("빈 프롬프트", file=sys.stderr)
        return 2

    region = args.region or os.environ.get("AWS_REGION") or DEFAULT_REGION
    proxy = args.proxy                       # 명시만 — env 폴백/강제 없음
    if proxy:                                # 명시 프록시는 botocore(자격증명)도 타게
        os.environ["HTTPS_PROXY"] = proxy
        os.environ["HTTP_PROXY"] = proxy

    try:
        client = _make_client(region, proxy, insecure=not args.secure)
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
        print(f"Bedrock 호출 실패({region}): {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1

    text = "".join(getattr(b, "text", "") for b in msg.content
                   if getattr(b, "type", "") == "text")
    if not text.strip():
        print("응답에 텍스트 블록 없음", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
