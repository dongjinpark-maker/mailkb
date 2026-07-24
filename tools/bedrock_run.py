#!/usr/bin/env python3
"""mailkb AI 백엔드 — Claude in Amazon Bedrock 어댑터.

ai_run 계약: 프롬프트를 stdin(utf-8)으로 받아 응답 텍스트만 stdout 으로 쓴다.
실패는 비0 종료 + stderr(호출부가 AIError 로 승격해 자동 재시도). anthropic 은
이 스크립트만 의존한다 — mailkb 코어(stdlib-only)는 무변경.

설치:   pip install -U "anthropic[bedrock]"
등록:   data/config.toml 예시 (신 엔드포인트)
        [ai.backends.bedrock-sonnet]
        cmd = ["python", "tools/bedrock_run.py", "--model", "anthropic.claude-sonnet-5"]
        [ai.backends.bedrock-haiku]
        cmd = ["python", "tools/bedrock_run.py", "--model", "anthropic.claude-haiku-4-5"]
레거시: 사내 방화벽이 .api.aws 를 막으면 --legacy(=bedrock-runtime.amazonaws.com).
        모델 ID 에 리전 추론 프로파일 접두사가 필요하다(서울=APAC → global. 또는 apac.):
        cmd = ["python", "tools/bedrock_run.py", "--legacy",
               "--model", "global.anthropic.claude-sonnet-5"]
리전:   --region > AWS_REGION > 기본 ap-northeast-2(서울).
        config 오버라이드 = cmd 에 "--region", "<리전>" 을 덧붙인다.
자격증명: 표준 AWS 체인(환경변수 → 프로필/SSO → 역할). SSO 는 `aws sso login` 선행.
사내 TLS: 가장 신뢰성 높은 방법은 `pip install truststore` — 있으면 어댑터가 OS
        검증 엔진(Windows=SChannel, claude CLI·브라우저와 동일)으로 검증한다. 없으면
        Windows 저장소 CA 를 수동 추가하는 폴백. 그래도 CERTIFICATE_VERIFY_FAILED 면
        회사 CA PEM 을 --ca-bundle(또는 AWS_CA_BUNDLE 환경변수)로 명시. 최후 수단은
        --insecure (TLS 검증 끔 — NODE_TLS_REJECT_UNAUTHORIZED=0 등가, 보안 저하).
        PEM(텍스트)만 — DER 이면 `openssl x509 -inform der -in c.crt -out c.pem`.
프록시: 사내 아웃바운드가 명시 프록시를 요구하면(pip 에 --proxy 가 필요한 환경) --proxy
        http://proxy.corp:8080 을 준다. 어댑터가 HTTPS_PROXY/HTTP_PROXY 로 env 에 실어
        httpx(Bedrock 호출)와 botocore(자격증명) 양쪽이 프록시를 타게 한다. 없으면
        HTTPS_PROXY 등 환경변수를 따른다. config 예:
        cmd = ["python","tools/bedrock_run.py","--ca-bundle","C:/x/ca.pem",
               "--proxy","http://proxy.corp:8080","--model","anthropic.claude-sonnet-5"]
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


# CA 탐색 환경변수 순서(--ca-bundle 다음). AWS_CA_BUNDLE 은 botocore(자격증명
# 계층)도 네이티브로 읽으므로, 이 하나만 설정하면 자격증명·Bedrock 호출 양쪽을 덮는다.
_CA_ENV_ORDER = ("MAILKB_BEDROCK_CA", "SSL_CERT_FILE",
                 "AWS_CA_BUNDLE", "REQUESTS_CA_BUNDLE")


def _resolve_ca(cli_ca: str | None, env: dict | None = None):
    """(경로, 출처) — 출처는 '--ca-bundle' 또는 환경변수명(진단용). 빈 문자열은 미지정."""
    e = os.environ if env is None else env
    if cli_ca:
        return cli_ca, "--ca-bundle"
    for name in _CA_ENV_ORDER:
        v = e.get(name)
        if v:
            return v, name
    return None, None


def resolve_ca_bundle(cli_ca: str | None, env: dict | None = None) -> str | None:
    """사내 TLS 검사 프록시용 CA PEM 경로. 순서: _CA_ENV_ORDER 참조."""
    return _resolve_ca(cli_ca, env)[0]


# 프록시 탐색 순서(--proxy 다음). 사내 아웃바운드가 명시 프록시를 요구하면(pip
# --proxy 가 필요한 환경) 이를 지정해야 AWS 로 나간다. httpx 는 trust_env=True 라
# HTTPS_PROXY 를 읽고, botocore 도 같은 변수를 읽으므로 env 에 실으면 양쪽을 덮는다.
_PROXY_ENV_ORDER = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                    "ALL_PROXY", "all_proxy")


def _resolve_proxy(cli_proxy: str | None, env: dict | None = None):
    """(url, 출처). 빈 문자열은 미지정으로 취급(없으면 (None, None))."""
    e = os.environ if env is None else env
    if cli_proxy:
        return cli_proxy, "--proxy"
    for name in _PROXY_ENV_ORDER:
        v = e.get(name)
        if v:
            return v, name
    return None, None


def _win_root_certs() -> list:
    """Windows 시스템 인증서 저장소(ROOT·CA)의 CA 를 DER 로 반환.

    회사 CA 는 보통 GPO 로 Windows ROOT 저장소에 배포된다 — claude CLI·브라우저가
    이걸 신뢰해 동작한다. Python httpx 는 certifi(공개 루트)만 봐 이 저장소를
    놓치므로, 여기서 명시적으로 긁어 온다. 비-Windows 는 빈 리스트."""
    if sys.platform != "win32":
        return []
    import ssl
    out = []
    for store in ("ROOT", "CA"):
        try:
            for cert, enc, _trust in ssl.enum_certificates(store):
                if enc == "x509_asn":            # DER 인코딩만
                    out.append(cert)
        except Exception:                        # 저장소 접근 실패는 무시(가능한 것만)
            pass
    return out


def _win_store_to_pem(enum_win=None) -> str | None:
    """Windows 저장소 CA 를 임시 PEM 파일로 내보내고 경로 반환(없으면 None).

    botocore(자격증명 계층)는 우리 SSLContext 를 안 쓰고 AWS_CA_BUNDLE/자체 번들만
    본다 — 이 PEM 을 AWS_CA_BUNDLE 로 지정하면 botocore 도 Windows 저장소의 회사
    CA(claude CLI 가 쓰는 그것)를 신뢰한다. atexit 로 정리(호출당 파일 누적 방지)."""
    import ssl
    ders = _win_root_certs() if enum_win is None else enum_win()
    pems = []
    for der in ders:
        try:
            # DER_cert_to_PEM_cert 는 검증 없이 base64 만 함 → load 로 유효성 확인 후 변환
            ssl.create_default_context().load_verify_locations(cadata=der)
            pems.append(ssl.DER_cert_to_PEM_cert(der))
        except (ssl.SSLError, ValueError, TypeError):
            pass
    if not pems:
        return None
    import atexit
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".pem", prefix="mailkb-winca-")
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write("".join(pems))
    atexit.register(lambda: os.path.exists(path) and os.remove(path))
    return path


def _ssl_context(ca_bundle: str | None = None, enum_win=None):
    """OS 검증 엔진(가능하면 truststore) 우선, 아니면 공개·시스템 루트 + Windows 저장소.

    claude CLI(Node)·브라우저는 OS 검증 엔진(Windows=SChannel)을 직접 호출해 체인
    구성·중간 인증서 처리를 완전하게 한다. truststore 가 설치돼 있으면 그걸 써
    동일하게 검증한다(가장 신뢰성 높음 — enum_certificates 수동 복사가 못 잡는
    중간 인증서·신뢰 플래그까지 OS 가 처리). 없으면 create_default_context 에
    Windows 저장소 CA 를 수동 추가하는 폴백. enum_win 은 테스트용 주입."""
    import ssl
    try:
        import truststore                        # OS 네이티브 검증(claude CLI 와 동일)
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        ctx = ssl.create_default_context()       # 폴백: 공개 + (플랫폼) 시스템 루트
        for der in (_win_root_certs() if enum_win is None else enum_win()):
            try:
                ctx.load_verify_locations(cadata=der)   # DER bytes = cadata
            except ssl.SSLError:                 # 불량/중복 인증서는 건너뜀
                pass
    if ca_bundle:
        try:
            ctx.load_verify_locations(cafile=ca_bundle)   # 명시 회사 CA(추가)
        except ssl.SSLError:
            pass
    return ctx


def _make_client(region: str, ca_bundle: str | None = None,
                 legacy: bool = False, insecure: bool = False,
                 proxy: str | None = None):
    """지연 임포트 — anthropic 미설치 환경에서도 모듈 임포트·테스트가 가능하게.

    legacy=True 면 AnthropicBedrock(레거시: bedrock-runtime.{region}.amazonaws.com,
    클래식 AWS 도메인)을, 아니면 AnthropicBedrockMantle(신: bedrock-mantle.{region}.
    api.aws)을 쓴다. 사내 방화벽이 .api.aws 를 막고 *.amazonaws.com 만 허용하면
    레거시가 필요하다. 두 클라이언트 모두 aws_region·http_client 를 받는다.

    verify: insecure→False(MITM 탈출구), ca_bundle/Windows→커스텀 SSLContext
    (truststore 우선), 그 외→기본(certifi).
    proxy: httpx.Client(proxy=)에 '명시' 지정한다 — 커스텀 http_client 에서는 env
    (HTTPS_PROXY)가 확실히 먹지 않아, 명시 지정이 신뢰성 있다(실환경 확인). botocore
    (자격증명)는 별도로 env 를 읽으므로 main 에서 env 도 함께 세팅한다."""
    if legacy:
        from anthropic import AnthropicBedrock as _Client
    else:
        from anthropic import AnthropicBedrockMantle as _Client
    kw = {"aws_region": region}
    if insecure:
        verify = False
    elif ca_bundle:
        # 실환경 확인: verify=<경로> 문자열을 httpx.Client 에 직접 전달(그 CA 만 신뢰).
        # _ssl_context(공개루트+Windows열거+truststore)로 감싸면 Windows 에서 OS
        # 검증이 거부하거나 열거가 간섭해 깨질 수 있다 — 검증된 단순 경로를 그대로 쓴다.
        verify = ca_bundle
    elif sys.platform == "win32":
        verify = _ssl_context(None)              # 자동: Windows 저장소/truststore
    else:
        verify = None                            # SDK 기본(certifi)
    if verify is not None or proxy:
        import httpx
        ckw = {}
        if verify is not None:
            ckw["verify"] = verify
        if proxy:
            ckw["proxy"] = proxy                 # env 아닌 명시 지정(핵심)
        kw["http_client"] = httpx.Client(**ckw)
    return _Client(**kw)


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
    ap.add_argument("--ca-bundle", default=None,
                    help="사내 TLS 검사용 CA PEM 경로 "
                         "(없으면 SSL_CERT_FILE 등 환경변수)")
    ap.add_argument("--legacy", action="store_true",
                    help="레거시 Bedrock(bedrock-runtime.amazonaws.com) 사용 — "
                         ".api.aws 가 막힌 사내망용. 모델 ID 는 global./apac. 접두사 필요")
    ap.add_argument("--proxy", default=None,
                    help="사내 명시 프록시 URL(예: http://proxy.corp:8080). "
                         "없으면 HTTPS_PROXY 등 환경변수")
    ap.add_argument("--insecure", action="store_true",
                    help="TLS 검증 끔(사내 MITM 프록시 탈출구, 최후 수단). "
                         "NODE_TLS_REJECT_UNAUTHORIZED=0 와 등가 — 보안 저하 주의")
    args = ap.parse_args(argv)

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("빈 프롬프트", file=sys.stderr)
        return 2

    region = resolve_region(args.region)
    ca, ca_src = _resolve_ca(args.ca_bundle)
    ca_note = f"{ca} ({ca_src})" if ca else "미지정(기본 certifi)"
    if ca and not os.path.isfile(ca):
        print(f"CA 번들 파일을 찾을 수 없음: {ca} ({ca_src})", file=sys.stderr)
        return 2
    if ca:
        # 자격증명 계층(botocore: SSO·STS)은 AWS_CA_BUNDLE 환경변수만 읽는다.
        # 해석한 CA 를 이 프로세스 env 에 실어, --ca-bundle 하나로 자격증명·Bedrock
        # 호출 두 계층을 다 덮는다(Windows 영구 환경변수 상속에 의존하지 않음).
        # anthropic/botocore 는 _make_client 안에서 지연 임포트되므로 여기 설정이 먼저.
        os.environ["AWS_CA_BUNDLE"] = ca
    elif args.insecure:
        # httpx(Bedrock 호출) TLS 검증만 끈다(_make_client). botocore(자격증명)는
        # SDK 가 verify 를 노출 안 해 그대로지만, 자격증명이 네트워크를 안 타면(캐시
        # SSO·정적 키) botocore TLS 검증 자체가 없어 문제되지 않는다.
        print("⚠ TLS 검증 비활성(--insecure) — 사내 MITM 프록시 전제. 보안 저하.",
              file=sys.stderr)
        ca_note = "검증 끔(--insecure)"
    elif sys.platform == "win32" and not os.environ.get("AWS_CA_BUNDLE"):
        # --ca-bundle 미지정 + Windows → 시스템 저장소를 임시 PEM 으로 내보내
        # botocore 도 회사 CA 를 신뢰(httpx 는 _ssl_context 가 이미 저장소를 신뢰).
        win_pem = _win_store_to_pem()
        if win_pem:
            os.environ["AWS_CA_BUNDLE"] = win_pem
            ca_note = f"{win_pem} (Windows 저장소)"

    proxy, proxy_src = _resolve_proxy(args.proxy)
    proxy_note = f"{proxy} ({proxy_src})" if proxy else "미지정(직접 연결)"
    if proxy:
        # httpx(trust_env)·botocore 둘 다 HTTPS_PROXY/HTTP_PROXY 를 읽으므로 env 에
        # 실으면 자격증명·Bedrock 호출 양쪽이 프록시를 탄다. 명시 지정이라 Windows
        # 환경변수 상속(CA 와 같은 문제)에 안 걸린다.
        os.environ["HTTPS_PROXY"] = proxy
        os.environ["HTTP_PROXY"] = proxy
    try:
        client = _make_client(region, ca, legacy=args.legacy,
                              insecure=args.insecure, proxy=proxy)
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
        endpoint = ("bedrock-runtime.*.amazonaws.com(레거시)" if args.legacy
                    else "bedrock-mantle.*.api.aws(신)")
        print(f"Bedrock 호출 실패({region}, {endpoint}): " + " <- ".join(chain),
              file=sys.stderr)
        print(f"CA: {ca_note}", file=sys.stderr)   # 어느 CA 를 잡았는지(미지정이면 상속 문제)
        print(f"프록시: {proxy_note}", file=sys.stderr)  # 미지정인데 pip 이 --proxy 필요 환경이면 의심
        low = " ".join(chain).lower()
        if any(k in low for k in ("inference profile", "on-demand throughput",
                                  "on demand throughput")):
            # 레거시 4.5+ 모델은 리전 추론 프로파일 접두사가 필요.
            print("모델 ID 에 추론 프로파일 접두사 필요 — --model 을 global.<모델> "
                  "또는 apac.<모델>(서울=APAC) 로 (예: global.anthropic.claude-sonnet-5)",
                  file=sys.stderr)
        if (any(k in low for k in ("connect", "getaddrinfo", "timed out",
                                   "timeout", "unreachable", "refused"))
                and not args.legacy):
            # .api.aws 도달 실패 — 사내 방화벽이 이 도메인을 막았을 가능성.
            print("신 엔드포인트(.api.aws) 연결 실패 — 사내 방화벽이 이 도메인을 "
                  "막았을 수 있다. --legacy(bedrock-runtime.amazonaws.com)로 재시도.",
                  file=sys.stderr)
        if any(k in low for k in _CRED_HINTS):
            print("자격증명 문제로 보임 — aws sso login(또는 키/프로필 설정) 후 재시도",
                  file=sys.stderr)
        if any(k in low for k in ("pem lib", "no start line",
                                  "unable to load certificate")):
            # CA 파일을 열긴 했으나 파싱 실패 — 보통 DER(바이너리) .crt/.cer 를 준 경우.
            print("CA 파일을 읽지 못함 — PEM(텍스트, '-----BEGIN CERTIFICATE-----')"
                  "이어야 한다. DER(바이너리 .crt/.cer)이면 변환: "
                  "openssl x509 -inform der -in corp.crt -out corp.pem", file=sys.stderr)
        elif "ssl" in low or "certificate" in low:
            print("인증서 검증 실패 — Windows 인증서 저장소 + 회사 CA 를 신뢰하는데도 "
                  "실패했다(claude CLI 와 같은 신뢰 집합).", file=sys.stderr)
            print("  · 남은 원인 후보: (a) botocore(자격증명 계층)가 별도 경로로 "
                  "인증서 검증에 실패 — AWS_CA_BUNDLE 로 회사 CA 지정 (b) 그 CA 가 "
                  "프록시 발급 체인의 루트+중간 전체가 아님 (c) 명시 프록시 미경유 "
                  "— pip 에 --proxy 가 필요한 환경이면 --proxy 함께 지정.",
                  file=sys.stderr)
        elif any(k in low for k in ("connect", "getaddrinfo", "timed out",
                                    "timeout", "proxy", "unreachable")):
            print("네트워크 경로 문제 — ① --proxy(사내 명시 프록시, pip --proxy 필요 "
                  "환경이면 거의 확실) ② 방화벽의 *.api.aws 허용 여부 "
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
