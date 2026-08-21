#!/usr/bin/env python3
"""mailkb AI 백엔드 — Amazon Bedrock 어댑터. **미유지(2026-08-20 확정).**

지금 이 어댑터를 쓰는 곳은 없고, 마지막 실기기 검증은 2026-07-28 이다. 당분간
쓸 계획도 없다 — 그래서 설정 예시(`[ai.backends.bedrock-*]`)를 여기서 뺐다.
머리말에 `config:` 조리법을 두면 **지원하는 백엔드 목록처럼 읽히고**, 그대로
붙여 넣으면 웹 설정의 'AI 백엔드 상태' 목록에도 올라와 [응답 시험]이 실제 과금
호출을 보낸다. 쓰는 백엔드는 claude CLI 쪽이다(내장 sonnet·haiku·opus).

ai_run 계약: 프롬프트를 stdin(utf-8)으로 받아 응답 텍스트만 stdout 에 쓴다
(실패는 비0 종료 + stderr). anthropic 은 이 스크립트만 의존(코어 무변경).
계약이 그대로라 되살리는 데 코어 수정은 필요 없다.

**되살린다면 먼저 확인할 것** — 아래 값은 전부 2026-07-28 사내망 실측이고 그
뒤로 아무도 확인하지 않았다:
  · `pip install -U "anthropic[bedrock]"` 로 의존을 깐다.
  · 레거시 엔드포인트 bedrock-runtime.*.amazonaws.com (AnthropicBedrock) —
    당시 신 .api.aws 는 그 망에서 막혀 있었다.
  · TLS 검증 끔이 기본이다(중간 프록시가 재서명한 인증서 전제). 켜려면 --secure.
  · 모델은 별칭 sonnet·opus 둘만 안다. **그 밖의 이름(haiku 등)은 전체 모델 ID
    로 그대로 넘어가 ValidationException 이 난다** — 별칭을 늘리거나 전체 ID 를
    직접 지정해야 한다.
  · 프록시는 --proxy 로 명시할 때만(env HTTPS_PROXY 는 자동으로 읽힘).
"""
import argparse
import os
import sys

DEFAULT_MODEL = "global.anthropic.claude-sonnet-5"
DEFAULT_REGION = "ap-northeast-2"           # 서울

# 짧은 별칭 → 전체 모델 ID. claude CLI 백엔드(--model opus)와 같은 이름이
# Bedrock 에서도 동작하게 한다. Bedrock 은 서버측 별칭이 없어 버전을 박아야
# 하고, opus 는 4.8 로 고정(사용자 확정 — 다른 버전은 전체 ID 로 직접 지정).
# opus 의 [1M] 접미는 사내 환경 확정 이름(2026-07-28) — AWS 공개 모델 카드에는
# 무접미(global.anthropic.claude-opus-4-8)만 실려 있으니 문서 기준으로 "고치지"
# 말 것. 다른 환경에서 ValidationException 이 나면 무접미 전체 ID 를 직접 지정.
_MODEL_ALIASES = {
    "sonnet": DEFAULT_MODEL,
    "opus": "global.anthropic.claude-opus-4-8[1M]",
}


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

    ap = argparse.ArgumentParser(
        description="mailkb Bedrock 백엔드 어댑터 "
                    "(미유지 — 2026-07-28 이후 검증하지 않았다)")
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

    model = _MODEL_ALIASES.get(args.model, args.model)
    region = args.region or os.environ.get("AWS_REGION") or DEFAULT_REGION
    if args.proxy:                           # botocore(자격증명)도 프록시를 타게
        os.environ["HTTPS_PROXY"] = args.proxy
        os.environ["HTTP_PROXY"] = args.proxy

    try:
        client = _make_client(region, args.proxy, insecure=not args.secure)
        msg = client.messages.create(
            model=model,
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
