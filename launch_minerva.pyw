"""Minerva 아이콘 실행기 — 클릭 한 번에:
  1) 옛 서버 종료 (PID 파일 — 재시작 시 기존 서버 kill 후 새로)
  2) 콘솔 없이 `serve --app` 시작
창 열기와 **창-서버 수명**은 서버가 맡는다(docs/ARCHITECTURE.md §7.11) — 마지막
앱 창을 닫으면 서버가 스스로 끝난다. 다시 아이콘을 누르면 새 서버로 뜬다.
최신 코드 반영은 매 실행마다 pull 하지 않는다(시간 낭비) — 설정의 '최신으로 업데이트'
(또는 수동 git pull) 후 창을 닫았다 다시 열면 적용된다.

여기서 Edge 창을 직접 띄우고 그 프로세스가 끝나기를 기다리던 코드는 2026-08-09 에
제거했다: Windows Edge 는 창을 이미 떠 있는 인스턴스에 넘기고 우리가 띄운 프로세스는
0.0초 만에 끝나(전용 프로필을 줘도 그렇다) 그 방식이 성립하지 않았다 — 창을 닫아도
서버가 남았다. 지금은 페이지가 서버에 창 상태를 알린다.

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
PORT = 8765                                     # serve 기본 포트


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


def main() -> None:
    _kill_old()
    # 띄우고 빠진다 — 창을 열고, 그 창이 닫히면 종료하는 일은 서버가 한다.
    subprocess.Popen([_pythonw(), "-m", "mailkb", "serve", "--app"], cwd=str(HERE))


if __name__ == "__main__":
    main()
