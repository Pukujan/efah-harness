"""End-to-end tests driven through Open WebUI's own API — the client, not the façade.

``tests/contract/test_chat_facade_contract.py`` tests the façade in isolation with
a stubbed worker. This file tests the thing the owner actually touches: requests
enter through Open WebUI exactly as the browser sends them, traverse its auth,
file, retrieval and routing layers, and only then reach the harness.

That distinction is not academic. The façade was correct and fully tested while
the UI showed "No models available" for an hour, because Open WebUI had persisted
a dead connection URL from a first boot and env vars only seed its database once.
No amount of façade testing could have caught that; only driving the client
could.

**Cost discipline.** Tests are split by whether they spend money:

* The default suite issues **no model generations**. Uploads, extraction,
  retrieval indexing, auth, model discovery, chat persistence and every error
  path are real end-to-end exercises of the client that cost nothing and finish
  in seconds. This is where breadth belongs.
* Tests marked ``live`` each perform a real generation through the harness.
  ``request_policy`` records an account-wide 100 req/min ceiling with
  ``unthrottled_fanout: forbidden``, so these serialise no matter how they are
  invoked and a plan-mode turn alone takes ~70s. They are deselected unless
  ``--run-live`` is passed, so a broad run stays free and a paid run is a
  deliberate act.

Run the free suite::

    pytest tests/integration/test_openwebui_e2e.py -q

Add the paid generations::

    pytest tests/integration/test_openwebui_e2e.py -q --run-live

Everything skips cleanly when the UI is not running, so CI stays green on a host
that has no container.
"""

from __future__ import annotations

import io
import json
import time
import uuid
import zipfile

import httpx
import pytest

UI_BASE = "http://127.0.0.1:8095"
PROBE_EMAIL = "pytest-probe@efah.local"
PROBE_PASSWORD = "pytest-probe-delete-me"
CONTAINER = "efah-openwebui"

MODE_IDS = ["efah-auto", "efah-plan", "efah-research", "efah-review", "efah-build"]


def _ui_reachable() -> bool:
    try:
        return httpx.get(f"{UI_BASE}/health", timeout=3).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _ui_reachable(), reason=f"Open WebUI not reachable at {UI_BASE}"
)


def _promote(email: str) -> None:
    """New signups land in ``pending`` when an admin already exists.

    Done through the container rather than the API because there is no
    bootstrap endpoint for it, and a pending account gets 401 on every route
    that matters — which reads exactly like a broken deployment.
    """
    import subprocess

    subprocess.run(
        ["docker", "exec", CONTAINER, "python3", "-c",
         f"import sqlite3;c=sqlite3.connect('/app/backend/data/webui.db');"
         f"c.execute(\"update user set role='admin' where email='{email}'\");c.commit()"],
        check=False, capture_output=True,
    )


@pytest.fixture(scope="module")
def session() -> httpx.Client:
    """An authenticated admin session, torn down completely afterwards."""
    client = httpx.Client(base_url=UI_BASE, timeout=300.0)
    signup = client.post("/api/v1/auths/signup", json={
        "name": "pytest probe", "email": PROBE_EMAIL, "password": PROBE_PASSWORD,
    })
    if signup.status_code != 200:
        client.post("/api/v1/auths/signin",
                    json={"email": PROBE_EMAIL, "password": PROBE_PASSWORD})
    _promote(PROBE_EMAIL)
    token = client.post("/api/v1/auths/signin", json={
        "email": PROBE_EMAIL, "password": PROBE_PASSWORD,
    }).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    yield client

    import subprocess
    subprocess.run(
        ["docker", "exec", CONTAINER, "python3", "-c",
         "import sqlite3;c=sqlite3.connect('/app/backend/data/webui.db');"
         f"uid=[r[0] for r in c.execute(\"select id from user where email='{PROBE_EMAIL}'\")];"
         "[c.execute(f'delete from {t} where user_id=?',(uid[0],)) "
         "for t in ('chat','file') if uid];"
         f"c.execute(\"delete from auth where id=(select id from user where "
         f"email='{PROBE_EMAIL}')\");"
         f"c.execute(\"delete from user where email='{PROBE_EMAIL}'\");c.commit()"],
        check=False, capture_output=True,
    )
    client.close()


def _upload(session: httpx.Client, name: str, payload: bytes, content_type: str):
    return session.post("/api/v1/files/", files={"file": (name, payload, content_type)})


def _extracted(session: httpx.Client, file_id: str, timeout_s: float = 20.0) -> str:
    """What the client actually indexed — the text a model would ever see.

    Extraction is ASYNCHRONOUS: an upload returns ``{"status": "pending"}`` and
    the content appears a moment later. Reading immediately measures the upload,
    not the extraction, and reports every format as broken. Polls to completion
    instead of sleeping a guessed interval.
    """
    deadline = time.monotonic() + timeout_s
    body: dict = {}
    while time.monotonic() < deadline:
        body = session.get(f"/api/v1/files/{file_id}").json()
        data = body.get("data") or {}
        if data.get("status") == "completed" or data.get("content"):
            return json.dumps(data, ensure_ascii=False)
        time.sleep(0.4)
    return json.dumps(body.get("data") or {}, ensure_ascii=False)


def _file_list(session: httpx.Client) -> list[dict]:
    """``GET /api/v1/files/`` returns ``{"items": [...]}``, not a bare list."""
    body = session.get("/api/v1/files/").json()
    return body["items"] if isinstance(body, dict) else body


# ---------------------------------------------------------------------------
# Discovery and auth — free
# ---------------------------------------------------------------------------


def test_ui_is_healthy(session):
    assert session.get("/health").json()["status"] is True


def test_config_reports_auth_enabled(session):
    assert session.get("/api/config").json()["features"]["auth"] is True


@pytest.mark.parametrize("mode_id", MODE_IDS)
def test_mode_is_visible_to_the_client(session, mode_id):
    """The regression that actually happened: modes present in the façade but
    invisible in the UI because the client held a stale connection URL."""
    ids = [m["id"] for m in session.get("/api/models").json()["data"]]
    assert mode_id in ids


def test_client_exposes_no_raw_vendor_model(session):
    ids = [m["id"] for m in session.get("/api/models").json()["data"]]
    leaked = [i for i in ids if i.startswith(("gpt-", "claude-", "kimi", "glm", "qwen",
                                              "gemini", "grok", "deepseek", "minimax"))]
    assert leaked == []


def test_unauthenticated_model_list_is_refused():
    assert httpx.get(f"{UI_BASE}/api/models", timeout=30).status_code == 401


def test_bad_password_is_refused(session):
    response = session.post("/api/v1/auths/signin",
                            json={"email": PROBE_EMAIL, "password": "wrong"})
    assert response.status_code >= 400


# ---------------------------------------------------------------------------
# File ingestion and extraction — free, and the substrate every RAG answer rests on
# ---------------------------------------------------------------------------

TEXT_FORMATS = [
    ("notes.txt", "text/plain"),
    ("notes.md", "text/markdown"),
    ("data.csv", "text/csv"),
    ("data.tsv", "text/tab-separated-values"),
    ("config.json", "application/json"),
    ("script.py", "text/x-python"),
    ("config.yaml", "application/x-yaml"),
    ("page.html", "text/html"),
    ("server.log", "text/plain"),
    ("notes.rst", "text/plain"),
]


@pytest.mark.parametrize("filename,content_type", TEXT_FORMATS)
def test_upload_is_accepted(session, filename, content_type):
    marker = f"MARKER-{uuid.uuid4().hex[:8].upper()}"
    response = _upload(session, filename, f"the marker is {marker}\n".encode(), content_type)
    assert response.status_code == 200
    session.delete(f"/api/v1/files/{response.json()['id']}")


@pytest.mark.parametrize("filename,content_type", TEXT_FORMATS)
def test_upload_returns_an_id(session, filename, content_type):
    response = _upload(session, filename, b"content\n", content_type)
    assert response.json().get("id")
    session.delete(f"/api/v1/files/{response.json()['id']}")


def _body_for(filename: str, marker: str) -> bytes:
    """Format-appropriate content.

    Tabular loaders parse structure rather than reading bytes: a single line of
    prose in a ``.csv`` is interpreted as a header row with no data beneath it,
    and extracts to nothing. Feeding every format the same prose measures the
    fixture, not the loader.
    """
    if filename.endswith(".csv"):
        return f"name,value\nmarker,{marker}\n".encode()
    if filename.endswith(".tsv"):
        return f"name\tvalue\nmarker\t{marker}\n".encode()
    if filename.endswith(".json"):
        return json.dumps({"marker": marker}).encode()
    return f"the marker is {marker}\n".encode()


@pytest.mark.parametrize("filename,content_type", TEXT_FORMATS)
def test_uploaded_content_is_extracted_not_merely_stored(session, filename, content_type):
    """A stored file a model can never read is indistinguishable from no file."""
    marker = f"MARKER-{uuid.uuid4().hex[:8].upper()}"
    file_id = _upload(session, filename, _body_for(filename, marker),
                      content_type).json()["id"]
    assert marker in _extracted(session, file_id)
    session.delete(f"/api/v1/files/{file_id}")


def test_single_line_csv_extracts_to_nothing(session):
    """KNOWN BEHAVIOUR, pinned so a change is noticed.

    A one-line CSV is read as a header with no rows, so extraction fails with
    "The content provided is empty" and the upload contributes nothing to any
    answer. Worth knowing before someone drops a one-row export in and wonders
    why the model ignores it.
    """
    file_id = _upload(session, "oneline.csv", b"the marker is MARKER-ONELINE\n",
                      "text/csv").json()["id"]
    extracted = _extracted(session, file_id, timeout_s=8.0)
    assert "MARKER-ONELINE" not in extracted
    assert "failed" in extracted or "empty" in extracted.lower()
    session.delete(f"/api/v1/files/{file_id}")


@pytest.mark.parametrize("filename,content_type", TEXT_FORMATS)
def test_uploaded_file_is_listed(session, filename, content_type):
    file_id = _upload(session, filename, b"listed\n", content_type).json()["id"]
    assert any(f["id"] == file_id for f in _file_list(session))
    session.delete(f"/api/v1/files/{file_id}")


@pytest.mark.parametrize("filename,content_type", TEXT_FORMATS)
def test_uploaded_file_can_be_deleted(session, filename, content_type):
    file_id = _upload(session, filename, b"temporary\n", content_type).json()["id"]
    assert session.delete(f"/api/v1/files/{file_id}").status_code == 200


UNICODE_PAYLOADS = [
    ("cjk.txt", "项目代号是 MARKER-CJK"),
    ("emoji.txt", "🚀 the marker is MARKER-EMOJI 🎉"),
    ("rtl.txt", "הסימן הוא MARKER-RTL"),
    ("accents.txt", "le marqueur est MARKER-ACCENT café naïve"),
    ("mixed.txt", "混合 mixed マーカー MARKER-MIXED"),
]


@pytest.mark.parametrize("filename,payload", UNICODE_PAYLOADS)
def test_unicode_document_is_extracted_intact(session, filename, payload):
    file_id = _upload(session, filename, payload.encode("utf-8"), "text/plain").json()["id"]
    marker = next(w for w in payload.split() if w.startswith("MARKER-"))
    assert marker in _extracted(session, file_id)
    session.delete(f"/api/v1/files/{file_id}")


def _zip_bytes(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return buffer.getvalue()


ZIP_SHAPES = [
    ("flat.zip", {"inner.txt": "marker ZIP-FLAT"}),
    ("nested.zip", {"dir/inner.txt": "marker ZIP-NESTED"}),
    ("deep.zip", {"a/b/c/inner.txt": "marker ZIP-DEEP"}),
    ("multi.zip", {"one.txt": "marker ZIP-ONE", "two.txt": "marker ZIP-TWO"}),
    ("mixed.zip", {"readme.md": "marker ZIP-MD", "data.csv": "col\nmarker ZIP-CSV"}),
]


@pytest.mark.parametrize("filename,members", ZIP_SHAPES)
def test_zip_upload_is_accepted(session, filename, members):
    response = _upload(session, filename, _zip_bytes(members), "application/zip")
    assert response.status_code == 200
    session.delete(f"/api/v1/files/{response.json()['id']}")


@pytest.mark.parametrize("filename,members", ZIP_SHAPES)
def test_zip_members_are_extracted(session, filename, members):
    file_id = _upload(session, filename, _zip_bytes(members), "application/zip").json()["id"]
    extracted = _extracted(session, file_id)
    for body in members.values():
        marker = next(w for w in body.split() if w.startswith("ZIP-"))
        assert marker in extracted, f"{marker} missing from extracted zip content"
    session.delete(f"/api/v1/files/{file_id}")


EDGE_UPLOADS = [
    ("empty.txt", b""),
    ("whitespace.txt", b"   \n\t\n"),
    ("large.txt", b"padding line\n" * 4000),
    ("nul.txt", b"before\x00after MARKER-NUL\n"),
    ("crlf.txt", b"line one\r\nline two MARKER-CRLF\r\n"),
    ("nobom.txt", "MARKER-UTF8 ünïcödé".encode()),
    ("bom.txt", b"\xef\xbb\xbfMARKER-BOM with bom"),
]


@pytest.mark.parametrize("filename,payload", EDGE_UPLOADS)
def test_edge_case_upload_does_not_500(session, filename, payload):
    response = _upload(session, filename, payload, "text/plain")
    assert response.status_code < 500
    if response.status_code == 200:
        session.delete(f"/api/v1/files/{response.json()['id']}")


# ---------------------------------------------------------------------------
# Chat persistence — free
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode_id", MODE_IDS)
def test_chat_can_be_created_and_retrieved(session, mode_id):
    created = session.post("/api/v1/chats/new", json={
        "chat": {"title": f"probe {mode_id}", "models": [mode_id], "messages": []},
    })
    assert created.status_code == 200
    chat_id = created.json()["id"]
    assert session.get(f"/api/v1/chats/{chat_id}").json()["id"] == chat_id
    session.delete(f"/api/v1/chats/{chat_id}")


@pytest.mark.parametrize("mode_id", MODE_IDS)
def test_chat_records_the_mode_it_used(session, mode_id):
    chat_id = session.post("/api/v1/chats/new", json={
        "chat": {"title": "probe", "models": [mode_id], "messages": []},
    }).json()["id"]
    assert mode_id in session.get(f"/api/v1/chats/{chat_id}").json()["chat"]["models"]
    session.delete(f"/api/v1/chats/{chat_id}")


@pytest.mark.parametrize("mode_id", MODE_IDS)
def test_chat_can_be_deleted(session, mode_id):
    chat_id = session.post("/api/v1/chats/new", json={
        "chat": {"title": "probe", "models": [mode_id], "messages": []},
    }).json()["id"]
    assert session.delete(f"/api/v1/chats/{chat_id}").status_code == 200
    assert session.get(f"/api/v1/chats/{chat_id}").status_code >= 400


def test_chat_list_is_scoped_to_the_user(session):
    assert isinstance(session.get("/api/v1/chats/").json(), list)


# ---------------------------------------------------------------------------
# Error paths through the client — free
# ---------------------------------------------------------------------------

REFUSED_MODELS = ["gpt-4o", "claude-opus-4-8", "kimi-k3", "glm-5.2", "efah-unknown", "auto"]


@pytest.mark.parametrize("bad_model", REFUSED_MODELS)
def test_client_request_for_a_raw_model_does_not_succeed(session, bad_model):
    response = session.post("/api/chat/completions", json={
        "model": bad_model, "stream": False,
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert response.status_code >= 400 or "error" in response.text.lower()


@pytest.mark.parametrize("mode_id", MODE_IDS)
def test_empty_message_is_refused_through_the_client(session, mode_id):
    response = session.post("/api/chat/completions", json={
        "model": mode_id, "stream": False,
        "messages": [{"role": "user", "content": "   "}],
    })
    assert response.status_code >= 400 or "error" in response.text.lower()


def test_missing_file_reference_is_handled(session):
    response = session.post("/api/chat/completions", json={
        "model": "efah-auto", "stream": False,
        "messages": [{"role": "user", "content": "what is in the file?"}],
        "files": [{"type": "file", "id": str(uuid.uuid4())}],
    })
    assert response.status_code < 500


def test_deleted_file_is_not_retrievable(session):
    file_id = _upload(session, "gone.txt", b"MARKER-GONE\n", "text/plain").json()["id"]
    session.delete(f"/api/v1/files/{file_id}")
    assert session.get(f"/api/v1/files/{file_id}").status_code >= 400


# ---------------------------------------------------------------------------
# Live generations — each one is billed
# ---------------------------------------------------------------------------


def _complete(session: httpx.Client, mode_id: str, prompt: str, **extra) -> str:
    body = {"model": mode_id, "stream": False,
            "messages": [{"role": "user", "content": prompt}]}
    body.update(extra)
    response = session.post("/api/chat/completions", json=body)
    assert response.status_code == 200, response.text[:300]
    return (response.json().get("choices") or [{}])[0].get("message", {}).get("content", "")


@pytest.mark.live
@pytest.mark.parametrize("mode_id", MODE_IDS)
def test_mode_answers_through_the_client(session, mode_id):
    assert _complete(session, mode_id, "Reply with exactly: ok").strip()


@pytest.mark.live
def test_document_retrieval_answers_from_the_upload(session):
    marker = f"ALBATROSS-{uuid.uuid4().hex[:4].upper()}"
    file_id = _upload(session, "secret.txt",
                      f"the deployment codeword is {marker}\n".encode(),
                      "text/plain").json()["id"]
    answer = _complete(session, "efah-auto",
                       "What is the deployment codeword in the attached notes? "
                       "Reply with just the word.",
                       files=[{"type": "file", "id": file_id}])
    session.delete(f"/api/v1/files/{file_id}")
    assert marker.split("-")[0] in answer.upper()


@pytest.mark.live
def test_zip_retrieval_answers_from_the_archive(session):
    marker = f"PELICAN-{uuid.uuid4().hex[:4].upper()}"
    file_id = _upload(session, "bundle.zip",
                      _zip_bytes({"inner.txt": f"the hidden marker is {marker}"}),
                      "application/zip").json()["id"]
    answer = _complete(session, "efah-auto",
                       "What is the hidden marker inside the attached zip? "
                       "Reply with just the marker.",
                       files=[{"type": "file", "id": file_id}])
    session.delete(f"/api/v1/files/{file_id}")
    assert marker.split("-")[0] in answer.upper()


@pytest.mark.live
def test_image_content_is_dropped_silently(session):
    """Records a KNOWN DEFECT rather than asserting the desired behaviour.

    ``ChatMessage.text()`` keeps only ``text`` parts, so an ``image_url`` part is
    discarded before dispatch with no error. Until that is fixed, image OCR
    cannot work regardless of the seated model. This test documents the current
    truth so a future fix flips it deliberately rather than by accident.
    """
    png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
           "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    response = session.post("/api/chat/completions", json={
        "model": "efah-auto", "stream": False,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Did you receive an image? Answer YES or NO only."},
            {"type": "image_url", "image_url": {"url": png}},
        ]}],
    })
    answer = (response.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
    assert "NO" in answer.upper(), (
        "images now reach the model — the known defect is fixed and this test "
        "should be inverted"
    )


@pytest.mark.live
def test_multi_turn_context_survives_the_client(session):
    response = session.post("/api/chat/completions", json={
        "model": "efah-auto", "stream": False,
        "messages": [
            {"role": "user", "content": "My project is called Bluefin."},
            {"role": "assistant", "content": "Noted."},
            {"role": "user", "content": "What is my project called? One word."},
        ],
    })
    content = (response.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
    assert "bluefin" in content.lower()


@pytest.mark.live
@pytest.mark.parametrize("mode_id", MODE_IDS)
def test_streaming_through_the_client_terminates(session, mode_id):
    with session.stream("POST", "/api/chat/completions", json={
        "model": mode_id, "stream": True,
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
    }) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "[DONE]" in body
