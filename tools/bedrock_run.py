#!/usr/bin/env python3
"""mailkb AI 백엔드 — Amazon Bedrock 어댑터.

ai_run 계약: 프롬프트를 stdin(utf-8)으로 받아 응답 텍스트만 stdout 에 쓴다
(실패는 비0 종료 + stderr). anthropic 은 이 스크립트만 의존(코어 무변경).

설치:   pip install -U "anthropic[bedrock]"
config: [ai.backends.bedrock-sonnet]
        cmd = ["python", "tools/bedrock_run.py"]

기본값(사내망 확정 조합):
  · 레거시 엔드포인트 bedrock-runtime.*.amazonaws.com (AnthropicBedrock) —
    신 .api.aws 는 방화벽 차단.
  · TLS 검증 끔 — 사내 MITM 프록시가 재서명한 인증서 전제(켜려면 --secure).
  · 모델 global.anthropic.claude-sonnet-5 — 레거시는 추론 프로파일 접두사 필요.
  · 프록시는 --proxy 로 명시할 때만(env HTTPS_PROXY 는 자동으로 읽힘).
"""
import argparse
import os
import sys

DEFAULT_MODEL = "global.anthropic.claude-sonnet-5"
DEFAULT_REGION = "ap-northeast-2"           # 서울


def _make_client(region, proxy=None, insecure=True):
    """anthropic 지연 임포트 — 미설치 환경에서도 모듈 임포트/테스트가 가능하게.
    insecure→httpx TLS 검증 끔, proxy→httpx.Client 에 명시 지정."""
    from anthropic import AnthropicBedrock
    ckw = {}
    if insecure:
        ckw["verify"] = False
    if proxy:
        ckw["proxy"] = proxy
    kw = {"aws_region": region}
    if ckw:
        import httpx
        kw["http_client"] = httpx.Client(**ckw)
    return AnthropicBedrock(**kw)


def main(argv=None):
    # Windows 파이프 기본 인코딩(cp949)이 이모지에서 죽는 것 방지
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="mailkb Bedrock 백엔드 어댑터")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--region", default=None, help=f"기본: AWS_REGION 또는 {DEFAULT_REGION}")
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--proxy", default=None, help="사내 명시 프록시 URL(기본 미설정)")
    ap.add_argument("--secure", action="store_true", help="TLS 검증 켬(기본은 끔)")
    args = ap.parse_args(argv)

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("빈 프롬프트", file=sys.stderr)
        return 2

    region = args.region or os.environ.get("AWS_REGION") or DEFAULT_REGION
    if args.proxy:                           # botocore(자격증명)도 프록시를 타게
        os.environ["HTTPS_PROXY"] = args.proxy
        os.environ["HTTP_PROXY"] = args.proxy

    try:
        client = _make_client(region, args.proxy, insecure=not args.secure)
        msg = client.messages.create(
            model=args.model,
            max_tokens=args.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except ModuleNotFoundError:
        print('anthropic 미설치 — pip install -U "anthropic[bedrock]"', file=sys.stderr)
        return 2
    except Exception as e:                    # 어떤 실패든 원인 한 줄 + 비0 종료가 계약
        print(f"Bedrock 호출 실패({region}): {type(e).__name__}: {e}", file=sys.stderr)
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
