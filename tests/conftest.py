from __future__ import annotations

import socket
from pathlib import Path

import pytest

from frame_art_mcp.ledger import DeviceIOError


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    """Fail closed if a test accidentally escapes its injected protocol boundary."""

    def forbidden(*args, **kwargs):
        raise AssertionError("Unit tests must not open network sockets")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)
    for key in ("FRAME_TV_DUID", "FRAME_TV_HOST"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FRAME_ART_STATE_DIR", str(tmp_path / "state"))


class FakeTV:
    """Stateful device protocol, including side effects followed by lost replies."""

    def __init__(self):
        self.mode = "art"
        self.current_id = "USER_BASE"
        self.images = {"USER_BASE", "USER_PHOTO"}
        self.calls = []
        self.after_upload = None
        self.before_probe = None
        self.before_delete = None
        self.upload_error = False
        self.select_error = False
        self.delete_confirmed = True
        self.counter = 0

    def probe(self, *, read_only=False):
        if self.before_probe:
            self.before_probe(self)
        self.calls.append(("probe", read_only, self.mode))
        return self.mode, None

    def current(self):
        self.calls.append(("current", self.current_id))
        return self.current_id

    def available(self):
        return self.images.copy()

    def upload(self, path: Path):
        assert self.mode == "art"
        assert path.read_bytes()
        self.counter += 1
        cid = f"MY_F{self.counter:04}"
        self.images.add(cid)
        self.calls.append(("upload", cid))
        if self.after_upload:
            self.after_upload(self)
        if self.upload_error:
            raise DeviceIOError("upload_reply_lost")
        return cid

    def select(self, content_id):
        assert self.mode == "art"
        assert content_id in self.images
        self.current_id = content_id
        self.calls.append(("select", content_id))
        if self.select_error:
            raise DeviceIOError("select_reply_lost")

    def delete(self, content_id):
        assert self.mode == "art"
        assert content_id != self.current_id
        assert content_id not in ("USER_BASE", "USER_PHOTO")
        if self.before_delete:
            self.before_delete(self)
        self.calls.append(("delete", content_id))
        if self.delete_confirmed:
            self.images.discard(content_id)
        return self.delete_confirmed

    def close(self):
        self.calls.append(("close",))


@pytest.fixture
def tv():
    return FakeTV()
