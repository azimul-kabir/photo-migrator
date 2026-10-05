"""Local web GUI: a small token-protected JSON API plus static pages, standard library only."""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from photo_migrator.config import ConfigError, load_config
from photo_migrator.database import Database
from photo_migrator.gui.jobs import JobConflict, JobRunner

LOGGER = logging.getLogger(__name__)

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
ITEM_GROUPS = {
    "new": ("new",),
    "existing": ("duplicate_existing", "reuse_destination"),
    "internal": ("duplicate_candidate",),
    "review": ("review",),
}
DEFAULT_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".heic",
    ".heif",
    ".png",
    ".gif",
    ".tif",
    ".tiff",
    ".dng",
    ".webp",
    ".mov",
    ".mp4",
    ".m4v",
)
MAX_BODY_BYTES = 64 * 1024
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_name(header: str) -> str:
    """Return the host part of an HTTP Host header, without port or IPv6 brackets."""
    if header.startswith("["):
        return header[1 : header.find("]")] if "]" in header else header
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def _toml_string(value: str) -> str:
    # JSON string escapes are a subset of TOML basic-string escapes when non-ASCII stays raw.
    return json.dumps(value, ensure_ascii=False)


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class GuiApp:
    """State and actions behind the HTTP handler; independent of the transport for tests."""

    def __init__(
        self, database_path: Path, config_path: Path, token: str, allow_remote: bool = False
    ) -> None:
        self.database_path = database_path.absolute()
        self.config_path = config_path.absolute()
        self.token = token
        self.allow_remote = allow_remote
        self.runner = JobRunner(self.database_path, self.config_path)

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        uri = self.database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def config_summary(self) -> tuple[dict[str, Any] | None, str | None]:
        if not self.config_path.exists():
            return None, None
        try:
            config = load_config(self.config_path)
        except ConfigError as exc:
            return None, str(exc)
        if config.library is None or config.imports is None:
            return None, "the GUI needs [library] and [imports] sections in the configuration"
        return {
            "library_root": str(config.library.root),
            "sources": [
                {"name": source.name, "path": str(source.path), "priority": source.priority}
                for source in config.sources
            ],
            "default_directory": config.imports.default_directory.as_posix(),
            "preserve_source_subdirectories": config.imports.preserve_source_subdirectories,
            "source_destinations": {
                name: path.as_posix() for name, path in config.imports.source_destinations
            },
        }, None

    def state(self) -> dict[str, Any]:
        config, config_error = self.config_summary()
        with self._read() as connection:
            library = connection.execute(
                """SELECT COUNT(*) AS assets, COALESCE(SUM(size_bytes),0) AS bytes,
                COALESCE(SUM(hash_status='completed' AND sha256 IS NOT NULL),0) AS hashed,
                COALESCE(SUM(hash_status='failed'),0) AS failed
                FROM assets WHERE asset_role='canonical' AND scan_status='available'"""
            ).fetchone()
            candidates = connection.execute(
                """SELECT COUNT(*) AS assets, COALESCE(SUM(size_bytes),0) AS bytes,
                COUNT(DISTINCT source_name) AS sources
                FROM assets WHERE asset_role='candidate' AND scan_status='available'"""
            ).fetchone()
            plans = connection.execute(
                """SELECT p.*,
                (SELECT status FROM import_runs WHERE plan_id=p.id AND dry_run=1
                    ORDER BY id DESC LIMIT 1) AS dry_run_status,
                (SELECT id FROM import_runs WHERE plan_id=p.id AND dry_run=0
                    ORDER BY id DESC LIMIT 1) AS import_run_id,
                (SELECT status FROM import_runs WHERE plan_id=p.id AND dry_run=0
                    ORDER BY id DESC LIMIT 1) AS import_status,
                (SELECT COALESCE(SUM(expected_size_bytes),0) FROM import_plan_items
                    WHERE plan_id=p.id AND action='new') AS new_bytes
                FROM import_plans p ORDER BY p.id DESC LIMIT 10"""
            ).fetchall()
            runs = {
                row["id"]: dict(row)
                for row in connection.execute(
                    """SELECT id,status,copied_count,reused_count,failed_count,bytes_written
                    FROM import_runs WHERE dry_run=0 ORDER BY id DESC LIMIT 10"""
                )
            }
        return {
            "database": str(self.database_path),
            "config_path": str(self.config_path),
            "config_exists": self.config_path.exists(),
            "config": config,
            "config_error": config_error,
            "library": dict(library),
            "candidates": dict(candidates),
            "plans": [
                {
                    "id": plan["id"],
                    "created_at": plan["created_at"],
                    "status": plan["status"],
                    "source_filter": plan["source_filter"],
                    "candidates": plan["candidate_count"],
                    "new": plan["new_count"],
                    "new_bytes": plan["new_bytes"],
                    "existing": plan["duplicate_count"],
                    "internal_duplicates": plan["internal_duplicate_count"],
                    "review": plan["review_count"],
                    "collisions": plan["collision_count"],
                    "bytes_avoided": plan["bytes_avoided"],
                    "dry_run_status": plan["dry_run_status"],
                    "import_status": plan["import_status"],
                    "import_run": runs.get(plan["import_run_id"]),
                }
                for plan in plans
            ],
            "job": self.runner.current(),
        }

    def plan_items(self, plan_id: int, group: str, offset: int, limit: int) -> dict[str, Any]:
        if group not in ITEM_GROUPS:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"unknown item group: {group}")
        actions = ITEM_GROUPS[group]
        placeholders = ",".join("?" for _ in actions)
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        where = f"i.plan_id=? AND i.action IN ({placeholders})"
        with self._read() as connection:
            if not connection.execute(
                "SELECT 1 FROM import_plans WHERE id=?", (plan_id,)
            ).fetchone():
                raise ApiError(HTTPStatus.NOT_FOUND, f"import plan {plan_id} does not exist")
            total = connection.execute(
                f"SELECT COUNT(*) FROM import_plan_items i WHERE {where}", (plan_id, *actions)
            ).fetchone()[0]
            rows = connection.execute(
                f"""SELECT i.action,i.destination_relative_path,i.matching_canonical_path,
                i.expected_size_bytes,i.reason,a.absolute_path,a.source_name
                FROM import_plan_items i JOIN assets a ON a.id=i.candidate_asset_id
                WHERE {where} ORDER BY i.id LIMIT ? OFFSET ?""",
                (plan_id, *actions, limit, offset),
            ).fetchall()
        return {
            "plan_id": plan_id,
            "group": group,
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": [
                {
                    "action": row["action"],
                    "source": row["source_name"],
                    "candidate_path": row["absolute_path"],
                    "destination": row["destination_relative_path"],
                    "matching_path": row["matching_canonical_path"],
                    "size_bytes": row["expected_size_bytes"],
                    "reason": row["reason"],
                }
                for row in rows
            ],
        }

    def start_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        plan_id = payload.get("plan_id")
        if not isinstance(action, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "action must be a string")
        if plan_id is not None and (not isinstance(plan_id, int) or isinstance(plan_id, bool)):
            raise ApiError(HTTPStatus.BAD_REQUEST, "plan_id must be an integer")
        if action == "import-run" and payload.get("confirm") is not True:
            raise ApiError(HTTPStatus.BAD_REQUEST, "a real import requires confirm=true")
        try:
            return self.runner.start(action, plan_id)
        except JobConflict as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
        except ValueError as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc

    def create_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write a new configuration file; never overwrites an existing one."""
        if self.config_path.exists():
            raise ApiError(HTTPStatus.CONFLICT, "configuration already exists; edit it directly")
        library_root = payload.get("library_root")
        default_directory = payload.get("default_directory") or "Camera Imports/Unsorted"
        preserve = payload.get("preserve_source_subdirectories", False)
        raw_sources = payload.get("sources")
        if not isinstance(library_root, str) or not library_root.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "library_root is required")
        if not isinstance(default_directory, str) or not isinstance(preserve, bool):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid import destination settings")
        if not isinstance(raw_sources, list) or not 1 <= len(raw_sources) <= 50:
            raise ApiError(HTTPStatus.BAD_REQUEST, "between 1 and 50 sources are required")
        sources = []
        for source in raw_sources:
            if not isinstance(source, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "each source must be an object")
            name, path, priority = source.get("name"), source.get("path"), source.get("priority")
            if not isinstance(name, str) or not isinstance(path, str):
                raise ApiError(HTTPStatus.BAD_REQUEST, "each source needs a name and a path")
            if not isinstance(priority, int) or isinstance(priority, bool):
                raise ApiError(HTTPStatus.BAD_REQUEST, f"source {name} priority must be a number")
            sources.append((name.strip(), path.strip(), priority))
        library_name = Path(library_root.strip()).name
        excluded = ["@eaDir", "#recycle", "Photos Library.photoslibrary"]
        if library_name and library_name not in excluded:
            excluded.append(library_name)
        lines = [
            "# Created by photo-migrator gui. Edit freely; the GUI will not overwrite it.",
            "[library]",
            f"root = {_toml_string(library_root.strip())}",
            "",
            "[imports]",
            f"default_directory = {_toml_string(default_directory.strip())}",
            f"preserve_source_subdirectories = {'true' if preserve else 'false'}",
            "",
            "[scan]",
            "extensions = [" + ", ".join(_toml_string(ext) for ext in DEFAULT_EXTENSIONS) + "]",
            "exclude_directory_names = [" + ", ".join(_toml_string(n) for n in excluded) + "]",
            'exclude_filename_suffixes = ["@SynoEAStream"]',
            'exclude_filename_prefixes = ["SYNOINDEX_"]',
        ]
        for name, path, priority in sources:
            lines += [
                "",
                "[[sources]]",
                f"name = {_toml_string(name)}",
                f"path = {_toml_string(path)}",
                f"priority = {priority}",
            ]
        text = "\n".join(lines) + "\n"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".photo-migrator-config.", suffix=".toml", dir=self.config_path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                load_config(temporary)
            except ConfigError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
            try:
                os.link(temporary, self.config_path)  # fails rather than overwrite
            except FileExistsError as exc:
                raise ApiError(HTTPStatus.CONFLICT, "configuration already exists") from exc
        finally:
            temporary.unlink(missing_ok=True)
        LOGGER.info("Wrote configuration %s", self.config_path)
        summary, error = self.config_summary()
        return {"config": summary, "config_error": error}


class _Handler(BaseHTTPRequestHandler):
    server_version = "photo-migrator"
    app: GuiApp

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.debug("%s %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            if not self._host_allowed():
                raise ApiError(HTTPStatus.FORBIDDEN, "unexpected Host header")
            url = urlsplit(self.path)
            if method == "GET" and url.path in STATIC_FILES:
                self._static(*STATIC_FILES[url.path])
                return
            if not url.path.startswith("/api/"):
                raise ApiError(HTTPStatus.NOT_FOUND, "not found")
            if not self._authorized():
                raise ApiError(HTTPStatus.UNAUTHORIZED, "missing or invalid access token")
            self._json(HTTPStatus.OK, self._api(method, url.path, parse_qs(url.query)))
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception as exc:
            LOGGER.exception("GUI request failed")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})

    def _api(self, method: str, path: str, query: dict[str, list[str]]) -> Any:
        app = self.app
        parts = path.strip("/").split("/")
        if method == "GET" and parts == ["api", "state"]:
            return app.state()
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "plans"]:
            if parts[3] != "items" or not parts[2].isdigit():
                raise ApiError(HTTPStatus.NOT_FOUND, "not found")
            return app.plan_items(
                int(parts[2]),
                query.get("group", ["new"])[0],
                self._int(query, "offset", 0),
                self._int(query, "limit", 50),
            )
        if method == "POST" and parts == ["api", "jobs"]:
            return app.start_job(self._body())
        if method == "POST" and parts == ["api", "jobs", "cancel"]:
            self._body()
            return {"cancelled": app.runner.cancel()}
        if method == "POST" and parts == ["api", "config"]:
            return app.create_config(self._body())
        raise ApiError(HTTPStatus.NOT_FOUND, "not found")

    @staticmethod
    def _int(query: dict[str, list[str]], name: str, default: int) -> int:
        value = query.get(name, [str(default)])[0]
        if not value.isdigit():
            raise ApiError(HTTPStatus.BAD_REQUEST, f"{name} must be a non-negative integer")
        return int(value)

    def _host_allowed(self) -> bool:
        if self.app.allow_remote:
            return True
        # Rejecting other names defends against DNS rebinding; any port allows SSH tunnels.
        return _host_name(self.headers.get("Host", "")) in LOOPBACK_NAMES

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, supplied = header.partition(" ")
        return scheme == "Bearer" and hmac.compare_digest(
            supplied.encode(), self.app.token.encode()
        )

    def _body(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid Content-Length") from exc
        if not 0 <= length <= MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "expected a JSON object")
        return payload

    def _static(self, name: str, content_type: str) -> None:
        body = resources.files("photo_migrator.gui").joinpath("static").joinpath(name).read_bytes()
        self._send(HTTPStatus.OK, body, content_type)

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload, sort_keys=True, default=str).encode()
        self._send(status, body, "application/json")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for header, value in SECURITY_HEADERS.items():
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(body)


def make_server(app: GuiApp, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("PhotoMigratorHandler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(
    database: Path,
    config: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    allow_remote: bool = False,
) -> int:
    if not is_loopback(host) and not allow_remote:
        raise ConfigError(
            f"refusing to listen on non-loopback address {host}; use an SSH tunnel or pass "
            "--allow-remote on a trusted network"
        )
    database.absolute().parent.mkdir(parents=True, exist_ok=True)
    with Database(database) as inventory:
        inventory.initialize()
    app = GuiApp(database, config, secrets.token_urlsafe(24), allow_remote)
    server = make_server(app, host, port)
    shown_host = f"[{host}]" if ":" in host else host
    url = f"http://{shown_host}:{server.server_address[1]}/#token={app.token}"
    print(f"Photo Migrator GUI for {app.database_path}")
    print(f"Open: {url}")
    print("Keep this link private: it grants control of imports. Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping; waiting for the current step to reach a safe point...")
        app.runner.cancel()
        app.runner.wait()
    finally:
        server.server_close()
    return 0
