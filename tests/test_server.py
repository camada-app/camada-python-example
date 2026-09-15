# The example against a configured engine that can never load a snapshot: the analyst URL is a
# closed port, so every request runs cold and falls open, and the routes the app gates itself
# (challenge) still work because the challenge kit needs no snapshot.
from __future__ import annotations

import os
import re
import socket
from collections.abc import Iterator
from pathlib import Path

import camada
import pytest
from fastapi.testclient import TestClient

BLOCKED_IP = "203.0.113.66"
BROWSER = {"x-forwarded-for": "198.51.100.7", "accept": "text/html", "sec-fetch-dest": "document"}
ROOT = Path(__file__).resolve().parents[1]


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    dead = f"http://127.0.0.1:{closed_port()}"
    monkeypatch.setenv("CAMADA_KEY", "tok-example.snap-example")
    monkeypatch.setenv("CAMADA_INGEST_URL", dead)
    monkeypatch.setenv("CAMADA_SNAPSHOT_URL", dead + "/snapshot")
    monkeypatch.setenv("CAMADA_TRUSTED_PROXY", "hops:1")
    monkeypatch.delenv("CAMADA_DISABLED", raising=False)
    monkeypatch.chdir(tmp_path)  # server's .env loader fills unset variables: a developer .env must not reach the suite
    monkeypatch.setattr(camada, "_default", None)  # the lazy first-request build, as in production
    import server

    # the middleware instance caches its engine, and Starlette builds the stack once: rebuild it so this
    # test's first request builds its own engine instead of running through the previous test's stopped one
    server.app.middleware_stack = None
    yield TestClient(server.app)
    engine = camada._default  # the one the middleware built (None only if the test made no request)
    if engine is not None:
        engine.stop()


def test_pages_render_with_the_first_party_beacon(client: TestClient) -> None:
    first = client.get("/")
    assert first.status_code == 200 and first.headers["set-cookie"].startswith("_sfp=")
    for path in ("/", "/pricing", "/login-form"):
        r = client.get(path)
        assert r.status_code == 200 and re.fullmatch(r"[0-9a-f-]{36}", r.headers["x-rid"])
        assert f'/_cam/b.js?r={r.headers["x-rid"]}' in r.text
    assert client.get("/_cam/b.js").headers["content-type"] == "application/javascript"


def test_api_answers_json(client: TestClient) -> None:
    assert client.get("/api/data").json()["ok"] is True


def test_login_reports_the_outcome(client: TestClient) -> None:
    bad = client.post("/login", data={"user": "demo@example.com", "pass": "nope"})
    assert bad.status_code == 401 and "login failed" in bad.text
    ok = client.post("/login", data={"user": "demo@example.com", "pass": "demo"})
    assert ok.status_code == 200 and "login succeeded" in ok.text
    # a body that is not UTF-8 is a failed login, not a 500
    raw = client.post("/login", content=b"user=\xff&pass=x", headers={"content-type": "application/x-www-form-urlencoded"})
    assert raw.status_code == 401


def test_each_test_builds_its_own_engine(client: TestClient) -> None:
    """The fixture's reset reaches the middleware: the first request builds an engine from this test's env."""
    assert camada._default is None
    client.get("/")
    engine = camada._default
    assert engine is not None and engine.env is not None
    assert engine.env.snapshot_url == os.environ["CAMADA_SNAPSHOT_URL"]


def test_a_cold_snapshot_falls_open(client: TestClient) -> None:
    r = client.get("/", headers={"x-forwarded-for": BLOCKED_IP})
    assert r.status_code == 200 and "x-block-reason" not in r.headers and "x-rid" in r.headers


def test_challenge_me_serves_the_page_to_a_browser(client: TestClient) -> None:
    r = client.get("/challenge-me", headers=BROWSER)
    assert r.status_code == 403 and r.headers["x-camada-challenge"] == "1"
    assert r.headers["content-type"].startswith("text/html") and "/__camada/challenge" in r.text


def test_challenge_me_answers_json_to_an_api_client(client: TestClient) -> None:
    r = client.get("/challenge-me", headers={"x-forwarded-for": "198.51.100.7"})
    assert r.status_code == 403 and r.json() == {"error": "challenge_required"}


def test_unknown_path_renders_the_404_page(client: TestClient) -> None:
    r = client.get("/nope")
    assert r.status_code == 404 and "Nothing here" in r.text and "x-rid" in r.headers


def test_lockfile_records_the_sibling_sdk() -> None:
    """uv.lock records the path dependency as the sibling editable checkout, the way package-lock.json
    records a file: dep. camada-python's version is dynamic (hatch reads version.py), so current uv writes
    the entry without a version line and there is nothing to drift; a lock written by an older uv still
    carries one, and then it must match what the sibling ships: re-lock after a bump."""
    version_py = ROOT.parent / "camada-python" / "src" / "camada" / "version.py"
    if not version_py.exists():
        pytest.fail(f"no camada-python checkout beside this repo ({version_py})")
    shipped = re.search(r'^__version__ = "([^"]+)"', version_py.read_text(), re.M)
    assert shipped, f"no __version__ literal in {version_py}"
    lock = (ROOT / "uv.lock").read_text()
    locked = re.search(r'name = "camada"\n(?:version = "([^"]+)"\n)?source = \{ editable = "\.\./camada-python" \}', lock)
    assert locked, "uv.lock does not record the sibling camada-python checkout: run `uv lock`"
    assert locked[1] in (None, shipped[1]), "uv.lock is behind camada-python: run `uv lock`"
