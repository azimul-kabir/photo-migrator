"""Local web GUI: access control, guarded actions, and an end-to-end import over HTTP."""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from photo_migrator.config import ConfigError
from photo_migrator.database import Database
from photo_migrator.gui.jobs import JobRunner
from photo_migrator.gui.server import GuiApp, make_server, serve

TOKEN = "test-token"


class Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        token: str | None = TOKEN,
        host: str | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict[str, str], Any]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = content_type
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        try:
            data: Any = json.loads(raw)
        except ValueError:
            data = raw.decode()
        return response.status, dict(response.getheaders()), data

    def wait_for_job(self, timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, _, state = self.request("GET", "/api/state")
            job = state["job"]
            if job and job["status"] != "running":
                return dict(job)
            time.sleep(0.05)
        raise AssertionError("job did not finish")

    def run(self, action: str, **extra: Any) -> dict[str, Any]:
        status, _, data = self.request("POST", "/api/jobs", {"action": action, **extra})
        assert status == 200, data
        job = self.wait_for_job()
        assert job["status"] == "succeeded", job
        return job


def _folders(tmp_path: Path) -> tuple[Path, Path, Path]:
    library, phone, laptop = tmp_path / "CleanLibrary", tmp_path / "phone", tmp_path / "laptop"
    for directory in (library, phone, laptop):
        directory.mkdir()
    (library / "kept.jpg").write_bytes(b"already here")
    (phone / "kept.jpg").write_bytes(b"already here")
    (phone / "IMG_1.jpg").write_bytes(b"phone one")
    (laptop / "IMG_1.jpg").write_bytes(b"laptop one!")
    (laptop / "copy.jpg").write_bytes(b"phone one")
    return library, phone, laptop


@pytest.fixture
def gui(tmp_path: Path) -> Iterator[tuple[Client, GuiApp]]:
    database = tmp_path / "photo.db"
    with Database(database) as inventory:
        inventory.initialize()
    app = GuiApp(database, tmp_path / "config.toml", TOKEN)
    server = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(server.server_address[1]), app
    finally:
        app.runner.cancel()
        app.runner.wait(10)
        server.shutdown()
        server.server_close()


def _create_config(client: Client, tmp_path: Path) -> None:
    library, phone, laptop = _folders(tmp_path)
    status, _, data = client.request(
        "POST",
        "/api/config",
        {
            "library_root": str(library),
            "default_directory": "Imports",
            "sources": [
                {"name": "phone", "path": str(phone), "priority": 90},
                {"name": "laptop", "path": str(laptop), "priority": 10},
            ],
        },
    )
    assert status == 200, data
    assert data["config"]["library_root"] == str(library.resolve())


def test_static_page_is_public_but_api_requires_the_token(
    gui: tuple[Client, GuiApp],
) -> None:
    client, _ = gui
    status, headers, body = client.request("GET", "/", token=None)
    assert status == 200 and "Photo Migrator" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert client.request("GET", "/api/state", token=None)[0] == 401
    assert client.request("GET", "/api/state", token="wrong")[0] == 401
    assert client.request("GET", "/api/state")[0] == 200
    assert client.request("GET", "/../etc/passwd", token=None)[0] == 404


def test_foreign_host_header_is_rejected(gui: tuple[Client, GuiApp]) -> None:
    client, _ = gui
    assert client.request("GET", "/api/state", host="evil.example:8765")[0] == 403
    assert client.request("GET", "/api/state", host="localhost:9999")[0] == 200
    assert client.request("GET", "/api/state", host="[::1]:9999")[0] == 200


def test_post_requires_json(gui: tuple[Client, GuiApp]) -> None:
    client, _ = gui
    status, _, _ = client.request(
        "POST", "/api/jobs", {"action": "import-scan"}, content_type="text/plain"
    )
    assert status == 415


def test_config_is_created_once_and_never_overwritten(
    gui: tuple[Client, GuiApp], tmp_path: Path
) -> None:
    client, app = gui
    _create_config(client, tmp_path)
    original = app.config_path.read_text()
    assert 'exclude_directory_names = ["@eaDir"' in original
    status, _, data = client.request(
        "POST",
        "/api/config",
        {"library_root": str(tmp_path), "sources": [{"name": "x", "path": "/", "priority": 1}]},
    )
    assert status == 409 and "already exists" in data["error"]
    assert app.config_path.read_text() == original


def test_invalid_config_is_rejected_without_writing(
    gui: tuple[Client, GuiApp], tmp_path: Path
) -> None:
    client, app = gui
    status, _, data = client.request(
        "POST",
        "/api/config",
        {
            "library_root": str(tmp_path / "missing"),
            "sources": [{"name": "a", "path": str(tmp_path), "priority": 1}],
        },
    )
    assert status == 400 and "does not exist" in data["error"]
    assert not app.config_path.exists()
    assert list(tmp_path.glob(".photo-migrator-config.*")) == []


def test_end_to_end_import_requires_dry_run_and_confirmation(
    gui: tuple[Client, GuiApp], tmp_path: Path
) -> None:
    client, _ = gui
    _create_config(client, tmp_path)
    sources = sorted((tmp_path / "phone").iterdir()) + sorted((tmp_path / "laptop").iterdir())
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}

    client.run("library-index")
    scanned = client.run("import-scan")
    assert scanned["result"]["indexed"] == 4
    assert scanned["progress"]["phase_name"] == "Scanning candidate sources"
    plan_id = client.run("import-plan")["result"]["plan_id"]

    _, _, state = client.request("GET", "/api/state")
    plan = state["plans"][0]
    assert (plan["new"], plan["existing"], plan["internal_duplicates"]) == (2, 1, 1)
    _, _, page = client.request("GET", f"/api/plans/{plan_id}/items?group=new")
    destinations = sorted(item["destination"] for item in page["items"])
    assert destinations[0] == "Imports/IMG_1.jpg"
    assert destinations[1].startswith("Imports/IMG_1__import_")
    _, _, page = client.request("GET", f"/api/plans/{plan_id}/items?group=internal")
    assert [Path(item["candidate_path"]).name for item in page["items"]] == ["copy.jpg"]

    status, _, data = client.request(
        "POST", "/api/jobs", {"action": "import-run", "plan_id": plan_id, "confirm": True}
    )
    assert status == 409 and "dry run" in data["error"]
    client.run("import-dry-run", plan_id=plan_id)
    status, _, data = client.request(
        "POST", "/api/jobs", {"action": "import-run", "plan_id": plan_id}
    )
    assert status == 400 and "confirm" in data["error"]
    client.run("import-run", plan_id=plan_id, confirm=True)

    _, _, state = client.request("GET", "/api/state")
    plan = state["plans"][0]
    assert plan["import_status"] == "completed"
    assert plan["import_run"]["copied_count"] == 2
    imported = sorted(path.name for path in (tmp_path / "CleanLibrary" / "Imports").iterdir())
    assert imported == [Path(destination).name for destination in destinations]
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources} == before


def test_one_job_at_a_time_and_cancel_is_resumable(
    gui: tuple[Client, GuiApp], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = gui
    _create_config(client, tmp_path)
    for index in range(30):
        (tmp_path / "CleanLibrary" / f"extra_{index}.jpg").write_bytes(b"x" * (index + 1))
    gate = threading.Event()
    original = JobRunner._listener

    def slow_listener(self: JobRunner, job: Any) -> Any:
        listen = original(self, job)

        def wrapped(snapshot: Any) -> None:
            gate.wait(5)
            listen(snapshot)

        return wrapped

    monkeypatch.setattr(JobRunner, "_listener", slow_listener)
    assert client.request("POST", "/api/jobs", {"action": "library-index"})[0] == 200
    status, _, data = client.request("POST", "/api/jobs", {"action": "import-scan"})
    assert status == 409 and "still running" in data["error"]
    assert client.request("POST", "/api/jobs/cancel", {})[2] == {"cancelled": True}
    gate.set()
    job = client.wait_for_job()
    assert job["status"] == "cancelled"
    assert any("cancelled" in line for line in job["logs"])

    monkeypatch.setattr(JobRunner, "_listener", original)
    assert client.run("library-index")["status"] == "succeeded"
    _, _, state = client.request("GET", "/api/state")
    assert state["library"]["assets"] == state["library"]["hashed"] == 31
    assert app.runner.running is False


def test_unknown_routes_and_bad_parameters(gui: tuple[Client, GuiApp]) -> None:
    client, _ = gui
    assert client.request("GET", "/api/nope")[0] == 404
    assert client.request("GET", "/api/plans/1/items")[0] == 404
    assert client.request("GET", "/api/plans/1/items?offset=-1")[0] == 400
    assert client.request("POST", "/api/jobs", {"action": "rm -rf"})[0] == 409
    status, _, data = client.request("POST", "/api/jobs", {"action": "import-dry-run"})
    assert status == 409 and "plan_id" in data["error"]


def test_serve_refuses_non_loopback_without_opt_in(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="non-loopback"):
        serve(tmp_path / "photo.db", tmp_path / "config.toml", host="0.0.0.0", open_browser=False)
    assert not (tmp_path / "photo.db").exists()
