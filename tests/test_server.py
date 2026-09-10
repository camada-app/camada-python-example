# The example against a configured engine that can never load a snapshot: the analyst URL is a
# closed port, so every request runs cold and falls open, and the routes the app gates itself
# (challenge) still work because the challenge kit needs no snapshot.
from __future__ import annotations

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
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    dead = f"http://127.0.0.1:{closed_port()}"
    monkeypatch.setenv("CAMADA_KEY", "tok-example.snap-example")
    monkeypatch.setenv("CAMADA_INGEST_URL", dead)
    monkeypatch.setenv("CAMADA_SNAPSHOT_URL", dead + "/snapshot")
    monkeypatch.setenv("CAMADA_TRUSTED_PROXY", "hops:1")
    monkeypatch.delenv("CAMADA_DISABLED", raising=False)
    monkeypatch.setattr(camada, "_default", None)  # the lazy first-request build, as in production
    import server

    yield TestClient(server.app)
    camada.get_default().stop()


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


def test_lockfile_records_the_sibling_sdk_version() -> None:
    """uv.lock pins the path dependency's version the way package-lock.json pins a file: dep; re-lock after a bump."""
    version_py = ROOT.parent / "camada-python" / "src" / "camada" / "version.py"
    if not version_py.exists():
        pytest.fail(f"no camada-python checkout beside this repo ({version_py})")
    shipped = re.search(r'^__version__ = "([^"]+)"', version_py.read_text(), re.M)
    lock = (ROOT / "uv.lock").read_text()
    locked = re.search(r'name = "camada"\nversion = "([^"]+)"\nsource = \{ editable = "\.\./camada-python" \}', lock)
    assert shipped and locked and locked[1] == shipped[1], "uv.lock is behind camada-python: run `uv lock`"
