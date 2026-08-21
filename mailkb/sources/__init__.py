"""소스 레지스트리."""

from __future__ import annotations


def get_source(name: str, cfg=None, known_folders: set | None = None):
    """소스 생성. cfg·known_folders 는 Outlook 전용(폴더 범위·백필 판단)이라
    fake 는 무시한다 — 1-인자 호출은 종전 그대로 동작한다."""
    if name == "fake":
        from .fake import FakeSource

        return FakeSource()
    if name == "outlook":
        from .outlook_com import OutlookComSource

        return OutlookComSource(cfg=cfg, known_folders=known_folders)
    raise ValueError(f"알 수 없는 소스: {name} (fake | outlook)")


def folder_labels(source) -> list:
    """이번 수집에서 실제로 연 폴더 라벨 — 범위를 벗어난 폴더를 상태에서 빼는 데
    쓴다. 폴더 개념이 없는 소스(fake)는 빈 목록이라 아무것도 지우지 않는다."""
    plan = getattr(source, "folder_plan", None)
    if plan is None:
        return []
    try:
        return [s.label for s in plan().specs]
    except Exception:
        return []


def remember_folder_plan(store, source) -> None:
    """마지막 수집의 폴더 목록을 저장 — 웹 설정 화면이 **COM 없이** 폴더를
    보여 주려고 쓴다(페이지 로드마다 Outlook 을 여는 것을 피한다).

    실패해도 수집을 깨뜨리지 않는다. 화면이 '마지막 수집 기준'이라고 적으므로
    옛 목록이 남는 것은 거짓말이 아니다 — 수집 자체를 실패시키는 쪽이 나쁘다.
    """
    plan = getattr(source, "folder_plan", None)
    if plan is None:                       # fake 등 폴더 개념이 없는 소스
        return
    try:
        store.set_folder_view(plan().as_rows())
    except Exception:
        pass
