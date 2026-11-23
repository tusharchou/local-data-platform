"""Shared pytest fixtures.

Every fixture is deterministic and offline, and writes only under ``tmp_path``.

Fixtures:
    make_table: A factory ``make_table(n=6, start_id=1, fare_offset=0.0, tz=None)``
        that builds a small rides table.
    sample_table: ``make_table()``, six rides with a naive ``timestamp[ns]`` column.
    sample_table_tz: ``make_table(tz="Asia/Bangkok")``, the same rides with a tz-aware
        ``timestamp[ns, tz=Asia/Bangkok]`` column.
    catalog_config: An Iceberg catalog config dict whose warehouse is under ``tmp_path``.
    lineage_server: A local HTTP server that records the JSON that is posted to it.
    web_server: A local HTTP server that answers ``GET`` with the replies that a test sets, such as a sheet that
        is shared by link, and records every request.

Tests marked ``dbt`` need the ``dbt`` extra (dbt-core and the dbt-duckdb adapter). They skip when
dbt cannot be imported, so ``make test`` passes on a plain install, and the ``dbt`` CI job runs
them with ``pytest -m dbt``.
"""

import datetime as dt
import http.server
import importlib.util
import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field

import pyarrow as pa
import pytest

CITIES = ("NYC", "BKK", "LDN")
BASE_TS = dt.datetime(2024, 1, 1, 8, 0, 0)
BASE_TS_NS = int(BASE_TS.replace(tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000


def build_rides(n: int = 6, start_id: int = 1, fare_offset: float = 0.0, tz: str | None = None) -> pa.Table:
    """Build a deterministic rides table.

    Columns: ``ride_id`` (int64), ``city`` (string), ``fare`` (float64) and
    ``pickup_ts`` (``timestamp[ns]``, or ``timestamp[ns, tz]`` when ``tz`` is set).
    Ride ``i`` is picked up ``3 * i`` hours and ``i`` nanoseconds after 2024-01-01 08:00 UTC,
    so the default six rides span two days and every timestamp has sub-microsecond precision.

    Args:
        n: Number of rows.
        start_id: The first ``ride_id``.
        fare_offset: Added to every fare, to make changed rows for upserts.
        tz: Timezone for ``pickup_ts``; ``None`` for a naive timestamp.

    Returns:
        The table.
    """
    ids = list(range(start_id, start_id + n))
    pickup = [BASE_TS_NS + (i * 3600 * 1_000_000_000) * 3 + i for i in ids]
    return pa.table({
        "ride_id": pa.array(ids, pa.int64()),
        "city": pa.array([CITIES[i % len(CITIES)] for i in ids], pa.string()),
        "fare": pa.array([round(10.0 + i * 1.5 + fare_offset, 2) for i in ids], pa.float64()),
        "pickup_ts": pa.array(pickup, pa.timestamp("ns", tz=tz)),
    })


def dbt_is_installed() -> bool:
    """Whether dbt-core and the dbt-duckdb adapter can be imported, which is what the ``dbt`` extra installs."""
    try:
        return all(importlib.util.find_spec(name) is not None for name in ("dbt.cli.main", "dbt.adapters.duckdb"))
    except ModuleNotFoundError:
        return False


def pytest_collection_modifyitems(config, items):
    """Skip every test marked ``dbt`` when the dbt extra is not installed."""
    if dbt_is_installed():
        return
    skip = pytest.mark.skip(reason="needs the dbt extra: pip install \"local-data-platform[dbt]\"")
    for item in items:
        if "dbt" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def network_trap():
    """A fake HTTP proxy on localhost. Yields ``(environment, connections)``.

    Run a subprocess with ``environment`` and ``connections`` gets one item per connection it opens,
    so an empty list means it made no network request that honours the proxy variables (requests,
    urllib and httpx do). The ``dbt`` tests use it to prove that dbt runs offline.
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    connections: list[bytes] = []

    def serve() -> None:
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return  # the fixture closed the socket
            connections.append(client.recv(200))
            client.close()

    threading.Thread(target=serve, daemon=True).start()
    proxy = f"http://127.0.0.1:{server.getsockname()[1]}"
    env = dict(os.environ, NO_PROXY="", no_proxy="")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[name] = proxy
    try:
        yield env, connections
    finally:
        server.close()


class _LineageHandler(http.server.BaseHTTPRequestHandler):
    received: list = []
    status = 201
    replies: list = []
    content_types: list = []
    raw: list = []

    def do_POST(self):  # noqa: N802 - http.server's naming
        body = self.rfile.read(int(self.headers["Content-Length"]))
        server = type(self)
        server.received.append((self.path, self.headers.get("Authorization"), json.loads(body)))
        server.content_types.append(self.headers.get("Content-Type"))
        server.raw.append(body)
        status, headers, text = server.replies.pop(0) if server.replies else (server.status, {}, "")
        answer = text.encode("utf-8")
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    """The notifier sends a post again after HTTP 429 or a 5xx answer, and waits a second and more first. No test waits.

    The fixture is on for every test and gives the list of the waits that the notifier asked for, in seconds, so a test
    that cares can say how long it would have waited.
    """
    from local_data_platform import notify

    waits: list[float] = []
    monkeypatch.setattr(notify, "_sleep", waits.append)
    return waits


@pytest.fixture
def lineage_server():
    """A local HTTP server that takes any JSON ``POST``. Yields ``(url, handler)``.

    ``handler.received`` is a list of ``(path, Authorization header, parsed body)`` in the order that the requests came,
    and ``handler.status`` is the status that the server answers with, 201 by default. ``handler.replies`` is a list of
    ``(status, headers, text)`` that the next requests are answered with, one for each, before ``handler.status``
    takes over, so a test can answer 429 with a ``Retry-After`` and then 200. ``handler.content_types`` and
    ``handler.raw`` say the ``Content-Type`` and the bytes of each request. The OpenLineage sink, the alert channels
    and the report delivery post here, so a test sends nothing outside the machine.
    """
    _LineageHandler.received = []
    _LineageHandler.status = 201
    _LineageHandler.replies = []
    _LineageHandler.content_types = []
    _LineageHandler.raw = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LineageHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)  # a fast stop
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", _LineageHandler
    server.shutdown()
    server.server_close()


@dataclass
class Reply:
    """What ``WebServer`` answers for a path: the status, the headers and the body, and how it is sent."""

    status: int = 200
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    content_type: str | None = "text/csv"
    delay: float = 0.0
    pause: float = 0.0
    piece: int = 1
    chunked: bool = False


class WebServer:
    """The server of the ``web_server`` fixture. ``url`` is its address, with no slash at the end.

    ``serve(path, body)`` sets the reply for a path, and a later call for the same path replaces it, so a test can edit
    a sheet between two ticks. A path with a query, such as ``/export?gid=1``, is the reply for that query, and a path
    without one is the reply for any query. A path that nothing was set for answers 404. ``seen`` lists every request in
    the order that it came, as ``(path with its query, headers)``.
    """

    def __init__(self) -> None:
        self.replies: dict[str, Reply] = {}
        self.once: dict[str, list[Reply]] = {}
        self.seen: list[tuple[str, dict]] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's naming
                owner._answer(self)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def serve(self, path: str, body: bytes | str = b"", *, status: int = 200, headers: dict | None = None,
              content_type: str | None = "text/csv", delay: float = 0.0, pause: float = 0.0, piece: int = 1,
              chunked: bool = False) -> None:
        """Answer ``GET path`` with ``status``, ``headers`` and ``body``.

        ``content_type`` is the ``Content-Type`` header, or none when it is ``None``. ``delay`` is the seconds before
        the answer starts, and with a ``pause`` the body goes out ``piece`` bytes at a time with that many seconds
        between them, so a test can make a slow server. A ``Content-Length`` in ``headers`` is sent as it is. With
        ``chunked`` the answer has no length and goes out in chunks of ``piece`` bytes, as a large export does.
        """
        text = body.encode("utf-8") if isinstance(body, str) else body
        self.replies[path] = Reply(status, text, dict(headers or {}), content_type, delay, pause, piece, chunked)

    def serve_once(self, path: str, body: bytes | str = b"", *, status: int = 200, **reply) -> None:
        """Answer the next ``GET path`` with this reply, and later ones with what ``serve`` set. Calls queue up."""
        text = body.encode("utf-8") if isinstance(body, str) else body
        self.once.setdefault(path, []).append(Reply(status, text, **reply))

    def _answer(self, handler) -> None:
        self.seen.append((handler.path, dict(handler.headers)))
        queued = self.once.get(handler.path) or self.once.get(handler.path.split("?")[0])
        reply = (queued.pop(0) if queued else self.replies.get(handler.path)
                 or self.replies.get(handler.path.split("?")[0]) or Reply(404, b"not found", content_type="text/plain"))
        time.sleep(reply.delay)
        headers = dict(reply.headers)
        if reply.content_type is not None:
            headers.setdefault("Content-Type", reply.content_type)
        if reply.chunked:
            headers["Transfer-Encoding"] = "chunked"
        else:
            headers.setdefault("Content-Length", str(len(reply.body)))
        try:
            handler.send_response(reply.status)
            for name, value in headers.items():
                handler.send_header(name, value)
            handler.end_headers()
            if reply.chunked:
                for start in range(0, len(reply.body), reply.piece):
                    part = reply.body[start:start + reply.piece]
                    handler.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                handler.wfile.write(b"0\r\n\r\n")
            elif reply.pause:
                for start in range(0, len(reply.body), reply.piece):
                    handler.wfile.write(reply.body[start:start + reply.piece])
                    handler.wfile.flush()
                    time.sleep(reply.pause)
            else:
                handler.wfile.write(reply.body)
        except OSError:
            pass  # the client left, as a test of a timeout makes it


@pytest.fixture
def web_server():
    """A local HTTP server on 127.0.0.1 that answers ``GET`` with the replies that a test sets. Yields a ``WebServer``.

    A CSV source that is read over http, the sheet of a shop that is shared by link, is served here, so a test reads
    nothing outside the machine.
    """
    server = WebServer()
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


@pytest.fixture
def make_table():
    """Return the :func:`build_rides` factory."""
    return build_rides


@pytest.fixture
def sample_table() -> pa.Table:
    """Six rides with a naive nanosecond timestamp column."""
    return build_rides()


@pytest.fixture
def sample_table_tz() -> pa.Table:
    """Six rides with a tz-aware (Asia/Bangkok) nanosecond timestamp column."""
    return build_rides(tz="Asia/Bangkok")


@pytest.fixture
def catalog_config(tmp_path) -> dict:
    """An Iceberg catalog config ``{"identifier", "warehouse_path"}`` under ``tmp_path``."""
    return {"identifier": "test_ns", "warehouse_path": str(tmp_path / "warehouse")}
