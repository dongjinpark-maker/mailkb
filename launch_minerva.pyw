"""Minerva 아이콘 실행기 — 클릭 한 번에:
  1) git pull      (최신 소스 준비, 오프라인·충돌이면 현재 코드로 계속)
  2) 옛 서버 종료   (PID 파일 — 결정: 재시작 시 기존 서버 kill 후 새로)
  3) 서버 시작      (콘솔 없이)
  4) Edge 앱 창 열기 (고유 임시 프로필 → 창 프로세스를 정확히 추적)
그리고 **그 창이 닫히면 서버도 함께 종료**한다. 재시작·종료 버튼이 따로 없다 —
다시 아이콘을 누르면 pull 후 새 서버로 뜬다.

콘솔 없이 쓰려면 바로 가기 대상을 python.exe 가 아니라 **pythonw.exe** 로 둔다:
  pythonw.exe "<경로>\\launch_minerva.pyw"
"""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # 리포 루트 (mailkb/ 의 부모)
PORT = 8765
URL = f"http://127.0.0.1:{PORT}/"


def _pythonw() -> str:
    """콘솔 없는 파이썬 실행기 경로 (없으면 현재 실행기)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        cand = exe.with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return sys.executable


def _home() -> Path:
    """serve 와 동일한 home: MAILKB_HOME > <코드폴더>/data."""
    env = os.environ.get("MAILKB_HOME")
    return Path(env).expanduser() if env else (HERE / "data")


def _port_open() -> bool:
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.3).close()
        return True
    except OSError:
        return False


def _git_pull() -> None:
    import shutil
    git = shutil.which("git")
    if not git:
        return
    try:
        subprocess.run([git, "pull", "--ff-only"], cwd=str(HERE), timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass                                    # 오프라인·충돌 → 현재 코드로 진행


def _kill_old() -> None:
    """PID 파일의 옛 서버를 종료하고 포트가 풀릴 때까지 잠깐 대기."""
    pidfile = _home() / "minerva.pid"
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGTERM)            # Windows: TerminateProcess 로 매핑
    except (ProcessLookupError, PermissionError, OSError):
        return
    for _ in range(40):
        if not _port_open():
            return
        time.sleep(0.1)


def _find_edge() -> str | None:
    """msedge.exe 탐색 (web._find_msedge 와 동일 로직 — 런처 독립성 위해 인라인)."""
    import shutil
    exe = shutil.which("msedge")
    if exe:
        return exe
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cand = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if cand.is_file():
                return str(cand)
    return None


def _open_window(profile_dir: str):
    """Edge 앱 창을 고유 프로필로 열고 Popen 반환. Edge 없으면 기본 브라우저(None)."""
    edge = _find_edge()
    if edge:
        try:
            return subprocess.Popen([edge, f"--app={URL}",
                                     f"--user-data-dir={profile_dir}",
                                     "--no-first-run", "--no-default-browser-check"])
        except OSError:
            pass
    import webbrowser
    webbrowser.open(URL)                         # 폴백: 창-종료 신호 없음
    return None


def main() -> None:
    _git_pull()
    _kill_old()
    server = subprocess.Popen([_pythonw(), "-m", "mailkb", "serve"], cwd=str(HERE))

    ready = False
    t0 = time.time()
    while time.time() - t0 < 20:
        if _port_open():
            ready = True
            break
        time.sleep(0.1)
    if not ready:
        return                                   # 서버 안 뜸 — 창 안 열고 종료

    # 고정 Edge 프로필 재사용 — 매 실행 새 프로필을 만들면(콜드 스타트) 창이 늦게 뜬다.
    # 첫 실행만 프로필 생성 비용을 치르고, 이후엔 빠르게 뜬다.
    profile = _home() / "edge-profile"
    profile.mkdir(parents=True, exist_ok=True)
    win = _open_window(str(profile))
    if win is None:
        return                                   # Edge 없음 — 창 추적 불가, 서버는 남김
    t0 = time.time()
    win.wait()                                   # ← 핵심: 창이 닫힐 때까지 대기
    if time.time() - t0 < 3.0:
        # 너무 빨리 반환 = 이미 열려 있던 Minerva 창으로 핸드오프됨(다른 창이 살아있음).
        # 그 창을 방해하지 않도록 서버를 죽이지 않고 종료.
        return
    server.terminate()                           # 창 닫힘 → 서버 종료
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
    # 서버가 신호로 죽으면 자기 finally 가 안 돌 수 있어 PID 파일을 여기서 정리
    pidfile = _home() / "minerva.pid"
    try:
        if pidfile.read_text(encoding="utf-8").strip() == str(server.pid):
            pidfile.unlink()
    except OSError:
        pass


if __name__ == "__main__":
    main()
