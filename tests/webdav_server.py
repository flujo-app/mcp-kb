"""A real WebDAV server for the tests: wsgidav over cheroot, on a loopback port.

It **requires** Basic auth. Nothing in this suite may reach it anonymously,
because that is how E3 shipped a git source that never authenticated at all:
every test passed against a remote that asked for nothing, and a private
repository could not be cloned in production. A server that refuses an
unauthenticated request makes "the credentials are right" a fact under test
rather than an assumption.

Every request is recorded, so a test can assert what the backend did and did
not ask of the network -- that an unchanged folder is not downloaded again,
and that a config error happens before the first request rather than as a 401
on the wire.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cheroot import wsgi
from wsgidav.wsgidav_app import WsgiDAVApp

USERNAME = "dr-k"
PASSWORD = "s3cret-app-password"

# What the fixture serves before a test edits it: one skill and one pack-level
# file, the same shape tests/test_git_source.py's origin repository has.
SKILL = "---\nname: {name}\ndescription: A skill.\n---\n\n{body}\n"


@dataclass
class Webdav:
    """A running server: where it is, what it serves, what has been asked of it."""

    url: str
    root: Path
    requests: list[tuple[str, str]] = field(default_factory=list)
    # Set by the fixture, and callable from a test: a live source has to go on
    # serving when the server it revalidates against stops answering, and the
    # only honest way to test that is to stop the server.
    stop: Callable[[], None] = lambda: None

    def method(self, verb: str) -> list[str]:
        """The paths ``verb`` has been requested on, in order."""
        return [path for method, path in self.requests if method == verb]

    def write(self, rel: str, text: str) -> Path:
        """Write a file into the served folder, making its parents."""
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def skill(self, name: str, body: str) -> Path:
        return self.write(f"skills/{name}/SKILL.md", SKILL.format(name=name, body=body))


class _Recorder:
    """WSGI middleware: one entry per request, before authentication decides."""

    def __init__(self, app, requests: list[tuple[str, str]]) -> None:
        self._app = app
        self._requests = requests

    def __call__(self, environ, start_response):
        self._requests.append((environ["REQUEST_METHOD"], environ.get("PATH_INFO", "")))
        return self._app(environ, start_response)


def _app(root: Path, requests: list[tuple[str, str]]) -> _Recorder:
    dav = WsgiDAVApp(
        {
            "provider_mapping": {"/": str(root)},
            # No "anonymous" entry: every request is authenticated or refused.
            "simple_dc": {"user_mapping": {"*": {USERNAME: {"password": PASSWORD}}}},
            "http_authenticator": {
                "accept_basic": True,
                "accept_digest": False,
                "default_to_digest": False,
            },
            "dir_browser": {"enable": False},
            "lock_storage": False,
            "logging": {"enable": False},
            "verbose": 1,
        }
    )
    return _Recorder(dav, requests)


@pytest.fixture
def webdav(tmp_path_factory) -> Iterator[Webdav]:
    """A served folder holding one skill and one pack-level file.

    Function-scoped: a test edits what the server serves, and the next test
    must not inherit the edit.
    """
    root = tmp_path_factory.mktemp("webdav")
    served = Webdav(url="", root=root)
    served.skill("x", "first")
    served.write("docs/guide.md", "pack-level guidance\n")

    server = wsgi.Server(("127.0.0.1", 0), _app(root, served.requests))
    server.prepare()
    served.url = f"webdav+http://127.0.0.1:{server.bind_addr[1]}"
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()

    stopped = False

    def stop() -> None:
        # Idempotent: a test may stop the server itself, and teardown still runs.
        nonlocal stopped
        if not stopped:
            stopped = True
            server.stop()
            thread.join(timeout=5)

    served.stop = stop
    try:
        yield served
    finally:
        stop()
