# camada-python-example: a small FastAPI app wired with camada against a local edge-analyst.
# Setup: cp .env.example .env (paste the CAMADA_KEY printed by `npm run seed`), uv sync,
# uv run uvicorn server:app --port 3002 --no-proxy-headers.
from __future__ import annotations

import os
import re
import time
from urllib.parse import parse_qs

from camada.fastapi import CamadaMiddleware, script_tag, serve_challenge, track
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

# minimal .env loader so the example has zero extra dependencies
try:
    with open(".env", encoding="utf-8") as env_file:
        for line in env_file:
            m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
            if m and os.environ.get(m[1]) is None:
                os.environ[m[1]] = m[2]
except OSError:
    pass  # no .env: rely on the environment

app = FastAPI()
app.add_middleware(CamadaMiddleware)  # ← the two-line install; the engine builds itself on the first request


def page(request: Request, title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        status_code=status,
        content=f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>{script_tag(request)}</head>
<body style="font-family: system-ui; max-width: 40rem; margin: 3rem auto">
<nav><a href="/">home</a> · <a href="/pricing">pricing</a> · <a href="/login-form">login</a> · <a href="/challenge-me">challenge</a></nav>
<h1>{title}</h1>{body}</body></html>""",
    )


@app.get("/")
async def home(request: Request) -> HTMLResponse:
    return page(
        request,
        "camada example shop",
        """
  <p>Every request here is captured by camada; the beacon below fingerprints this browser first-party.</p>
  <p><button onclick="fetch('/api/data').then(r=>r.json()).then(d=>alert(JSON.stringify(d)))">call the API</button></p>""",
    )


@app.get("/pricing")
async def pricing(request: Request) -> HTMLResponse:
    return page(request, "Pricing", "<p>Free while unreleased.</p>")


@app.get("/login-form")
async def login_form(request: Request) -> HTMLResponse:
    return page(
        request,
        "Log in",
        """
  <form method="post" action="/login">
    <input name="user" placeholder="email"> <input name="pass" type="password"> <button>go</button>
  </form>""",
    )


@app.post("/login")
async def login(request: Request) -> HTMLResponse:
    # parse the urlencoded form by hand: request.form() needs python-multipart, an extra dependency
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    ok = form.get("user") == "demo@example.com" and form.get("pass") == "demo"
    track(request, "login_succeeded" if ok else "login_failed", user=form.get("user", ""))  # uid is HMAC-hashed in the SDK
    return page(request, "Welcome" if ok else "Nope", f"<p>login {'succeeded' if ok else 'failed'}</p>", status=200 if ok else 401)


@app.get("/api/data")
async def api_data() -> dict[str, object]:
    return {"ok": True, "at": int(time.time() * 1000)}


# SDK-04 demo: force the challenge for this route, whatever the snapshot says. In production
# the same page is served automatically for a `challenge` verdict. Once solved, the `_cch`
# cookie is good for an hour and this route renders normally.
@app.get("/challenge-me")
async def challenge_me(request: Request) -> HTMLResponse:
    return serve_challenge(request) or page(
        request,
        "Challenge passed",
        """
    <p>The <code>_cch</code> cookie is set for an hour. Clear it (or open a private window) to see the check again.</p>""",
    )


@app.exception_handler(404)
async def not_found(request: Request, _exc: Exception) -> HTMLResponse:
    return page(request, "404", "<p>Nothing here.</p>", status=404)


if __name__ == "__main__":
    import uvicorn

    # proxy_headers=False: uvicorn would otherwise rewrite the peer from X-Forwarded-For for any
    # 127.0.0.1 client, before camada's trusted-proxy rules get to see the header
    uvicorn.run("server:app", host="127.0.0.1", port=int(os.environ.get("PORT", "3002")), proxy_headers=False)
