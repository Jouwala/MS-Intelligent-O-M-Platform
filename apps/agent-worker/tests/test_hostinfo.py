"""主机信息与能力标签（TASK-11）。"""

from __future__ import annotations

import sys

from conftest import hi


def test_platform_label_matches_runtime():
    if sys.platform.startswith("win"):
        assert hi.platform_label() == "windows-worker"
    elif sys.platform.startswith("linux"):
        assert hi.platform_label() == "linux-worker"
    else:
        assert hi.platform_label().endswith("-worker")


def test_capabilities_include_python_and_platform_label():
    caps = hi.capabilities()
    assert "python" in caps
    assert hi.platform_label() in caps


def test_linux_capabilities(monkeypatch):
    monkeypatch.setattr(hi.sys, "platform", "linux")
    assert hi.platform_kind() == "linux"
    assert hi.platform_label() == "linux-worker"
    assert hi.capabilities() == ("python", "linux-worker")


def test_windows_capabilities(monkeypatch):
    monkeypatch.setattr(hi.sys, "platform", "win32")
    assert hi.platform_label() == "windows-worker"
    assert hi.capabilities() == ("python", "windows-worker")


def test_hostname_prefers_env(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "worker-linux-7")
    assert hi.hostname() == "worker-linux-7"


def test_hostname_falls_back_to_socket(monkeypatch):
    monkeypatch.delenv("HOSTNAME", raising=False)
    monkeypatch.setattr(hi.socket, "gethostname", lambda: "computed-host")
    assert hi.hostname() == "computed-host"


def test_hostname_empty_when_both_unavailable(monkeypatch):
    monkeypatch.delenv("HOSTNAME", raising=False)

    def boom():
        raise OSError("no hostname")

    monkeypatch.setattr(hi.socket, "gethostname", boom)
    assert hi.hostname() == ""


def test_collect_returns_registration_triplet():
    info = hi.collect(hostname_fn=lambda: "host-x", platform_fn=lambda: "linux-worker")
    assert info == {
        "hostname": "host-x",
        "platform": "linux-worker",
        "capabilities": ("python", "linux-worker"),
    }
