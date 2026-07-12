"""소스 레지스트리."""

from __future__ import annotations


def get_source(name: str):
    if name == "fake":
        from .fake import FakeSource

        return FakeSource()
    if name == "outlook":
        from .outlook_com import OutlookComSource

        return OutlookComSource()
    raise ValueError(f"알 수 없는 소스: {name} (fake | outlook)")
