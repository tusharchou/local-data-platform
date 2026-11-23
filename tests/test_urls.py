"""The address rules and the download of ``local_data_platform.urls``.

The real sockets of these tests are on 127.0.0.1, served by the ``web_server`` fixture, so nothing leaves the machine.
What only https can show, such as a redirect from one https address to another, goes through a scripted adapter that
answers for ``requests`` and opens no socket.
"""

import gzip
import io
import socket
import time
import types

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from local_data_platform import notify, urls
from local_data_platform.exceptions import ConfigError
from local_data_platform.orchestration.retries import is_retriable

SECRET = "S3CR3T-VALUE"


# ====================================================================== the rules for an address


@pytest.mark.parametrize("text, expected", [
    ("https://docs.google.com/x", True),
    ("http://127.0.0.1:8000/x.csv", True),
    ("HTTPS://EXAMPLE.COM/x", True),
    ("Http://example.com", True),
    ("ftp://example.com/x", False),
    ("s3://bucket/key.csv", False),
    ("gs://bucket/key.csv", False),
    ("file:///tmp/x.csv", False),
    ("/tmp/x.csv", False),
    ("data/https://x.csv", False),
    ("https:/example.com", False),
    (" https://example.com", False),
    ("", False),
    (None, False),
    (42, False),
])
def test_is_http(text, expected):
    assert urls.is_http(text) is expected


@pytest.mark.parametrize("url, expected", [
    ("https://docs.google.com/spreadsheets/d/ID/export?format=csv&gid=0",
     "https://docs.google.com/spreadsheets/d/ID/export"),
    ("https://user:pw@example.com/a/b.csv", "https://example.com/a/b.csv"),
    ("https://token@example.com/a.csv", "https://example.com/a.csv"),
    ("https://example.com:8443/a.csv#gid=1", "https://example.com:8443/a.csv"),
    ("HTTPS://Example.COM/A.csv", "https://example.com/A.csv"),
    ("http://127.0.0.1:9/x?key=1", "http://127.0.0.1:9/x"),
    ("http://[::1]:8000/x", "http://[::1]:8000/x"),
    ("https://example.com", "https://example.com"),
    ("https://example.com/orders_{window_start:%Y-%m-%d}.csv",
     "https://example.com/orders_{window_start:%Y-%m-%d}.csv"),
    ("https://example.com:99999/x", "the address"),
])
def test_label_has_no_user_query_or_fragment(url, expected):
    assert urls.label(url) == expected


@pytest.mark.parametrize("url", [
    "https://example.com/a.csv",
    "https://example.com:8443/a.csv?format=csv&gid=0#gid=0",
    "https://docs.google.com/spreadsheets/d/ID/export?format=csv&gid=0",
    "http://127.0.0.1/a.csv",
    "http://127.0.0.1:8000/a.csv",
    "http://localhost:8000/a.csv",
    "HTTP://LOCALHOST/a.csv",
    "https://127.0.0.1/a.csv",
    "https://example.com/orders_{window_start:%Y-%m-%d}.csv",
])
def test_problem_none_for_an_address_that_may_be_read(url):
    assert urls.problem(url) is None


@pytest.mark.parametrize("url, words", [
    ("http://example.com/a.csv", "plain http, which is only for 127.0.0.1 and localhost"),
    ("http://192.168.1.5/a.csv", "plain http"),
    ("http://127.0.0.2/a.csv", "plain http"),
    ("http://[::1]:8000/a.csv", "plain http"),
    ("http://localhost.example.com/a.csv", "plain http"),
    ("ftp://example.com/a.csv", "does not start with https://"),
    ("file:///tmp/a.csv", "does not start with https://"),
    ("https://", "has no host name"),
    ("https:///a.csv", "has no host name"),
    ("https://user:password@example.com/a.csv", "user name or a password"),
    ("https://token@example.com/a.csv", "user name or a password"),
    ("https://@example.com/a.csv", "user name or a password"),
    ("http://user:pw@127.0.0.1/a.csv", "user name or a password"),
    ("https://example.com:99999/a.csv", "is not valid"),
    ("https://example.com:abc/a.csv", "is not valid"),
    ("https://example.com/a b.csv", "control character"),
    ("https://example.com/a.csv\n", "control character"),
    ("https://example.com/a.csv\r\nHost: evil", "control character"),
    ("https://example.com/\x00a.csv", "control character"),
    ("https://example.com/\x7fa.csv", "control character"),
    ("https://example.com/a\tb.csv", "control character"),
    ("https://example.com/a\u00a0b.csv", "control character"),
    ("https://example.com/a\u2028b.csv", "control character"),
    ("https://example.com/a\x0bb.csv", "control character"),
    ("https://example.com/a\x1fb.csv", "control character"),
])
def test_problem_says_why(url, words):
    reason = urls.problem(url)

    assert reason is not None and words in reason


@pytest.mark.parametrize("url", [
    "https://user:SECRET-A@example.com/path-B/file-C.csv?token=SECRET-D",
    "http://example.com/path-B/file-C.csv?token=SECRET-D",
    "ftp://example.com/path-B/file-C.csv?token=SECRET-D",
    "https://example.com:99999/path-B/file-C.csv?token=SECRET-D",
    "https://example.com/path-B/file C.csv?token=SECRET-D",
])
def test_the_reason_never_holds_the_address(url):
    reason = urls.problem(url)

    assert reason
    for part in ("SECRET-A", "SECRET-D", "path-B", "file-C", "example.com"):
        assert part not in reason


def test_a_redirect_from_a_public_host_may_not_go_to_plain_http_on_this_machine():
    assert urls.problem("http://127.0.0.1:8000/a.csv", local_ok=False) == (
        "is plain http on this machine, which a redirect from another host may not go to")
    assert urls.problem("http://127.0.0.1:8000/a.csv", local_ok=True) is None
    assert urls.problem("https://example.com/a.csv", local_ok=False) is None
    assert urls.problem("http://example.com/a.csv", local_ok=False) == (
        "is plain http, which is only for 127.0.0.1 and localhost")


def test_the_local_hosts_are_the_ones_of_the_alert_channels():
    assert urls.LOCAL_HOSTS == ("127.0.0.1", "localhost")
    assert notify.LOCAL_HOSTS is urls.LOCAL_HOSTS


@pytest.mark.parametrize("url, name", [
    ("https://x.test/a.csv?key=1", "key"),
    ("https://x.test/a.csv?KEY=1", "KEY"),
    ("https://x.test/a.csv?api_key=1", "api_key"),
    ("https://x.test/a.csv?API-KEY=1", "API-KEY"),
    ("https://x.test/a.csv?apikey=1", "apikey"),
    ("https://x.test/a.csv?access_token=1", "access_token"),
    ("https://x.test/a.csv?Token=1", "Token"),
    ("https://x.test/a.csv?id_token=1", "id_token"),
    ("https://x.test/a.csv?password=1", "password"),
    ("https://x.test/a.csv?passwd=1", "passwd"),
    ("https://x.test/a.csv?pwd=1", "pwd"),
    ("https://x.test/a.csv?secret=1", "secret"),
    ("https://x.test/a.csv?client_secret=1", "client_secret"),
    ("https://x.test/a.csv?credential=1", "credential"),
    ("https://x.test/a.csv?X-Amz-Credential=1", "X-Amz-Credential"),
    ("https://x.test/a.csv?X-Amz-Signature=1", "X-Amz-Signature"),
    ("https://x.test/a.csv?X-Goog-Signature=1", "X-Goog-Signature"),
    ("https://x.test/a.csv?X-Amz-Security-Token=1", "X-Amz-Security-Token"),
    ("https://x.test/a.csv?sig=1", "sig"),
    ("https://x.test/a.csv?auth=1", "auth"),
    ("https://x.test/a.csv?private_key=1", "private_key"),
    ("https://x.test/a.csv?access_key=1", "access_key"),
    ("https://x.test/a.csv?accesskey=1", "accesskey"),
    ("https://x.test/a.csv?key", "key"),
    ("https://x.test/a.csv?key=", "key"),
    ("https://x.test/a.csv?format=csv&token=1", "token"),
    ("https://x.test/a.csv?format=csv&gid=0&key=abc&single=true", "key"),
    ("https://x.test/a.csv#access_token=1", "access_token"),
    ("https://x.test/a.csv?format=csv#key=1&gid=2", "key"),
    ("https://x.test/a.csv?a=1&token=1&key=2", "token"),
    ("https://x.test/a.csv?-key-=1", "-key-"),
    ("https://x.test/a.csv?api--key=1", "api--key"),
    ("https://x.test/a.csv?X.Amz.Signature=1", "X.Amz.Signature"),
])
def test_secret_parameter_names_the_parameter(url, name):
    assert urls.secret_parameter(url) == name


@pytest.mark.parametrize("url", [
    "https://x.test/a.csv",
    "https://x.test/a.csv?format=csv&gid=0",
    "https://x.test/a.csv?format=csv&gid=0&single=true&output=csv",
    "https://x.test/a.csv#gid=0",
    "https://x.test/a.csv?gid=0#range=A1:C9",
    "https://x.test/a.csv?primary_key=id",
    "https://x.test/a.csv?keyword=pad+thai",
    "https://x.test/a.csv?monkey=1",
    "https://x.test/a.csv?author=ann",
    "https://x.test/a.csv?signed=1",
    "https://x.test/token/a.csv",
    "https://x.test/key/a.csv?id=1",
])
def test_secret_parameter_is_none_for_other_parameters(url):
    assert urls.secret_parameter(url) is None


# ====================================================================== a download from a local server


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def test_fetch_returns_the_body(web_server):
    web_server.serve("/sheet.csv", "day,dish\n2026-07-01,pad thai\n")

    assert urls.fetch(f"{web_server.url}/sheet.csv") == b"day,dish\n2026-07-01,pad thai\n"
    assert [path for path, _ in web_server.seen] == ["/sheet.csv"]


def test_fetch_sends_the_query_as_it_is(web_server):
    web_server.serve("/export", "a\n1\n")

    urls.fetch(f"{web_server.url}/export?format=csv&gid=42")

    assert web_server.seen[0][0] == "/export?format=csv&gid=42"


def test_fetch_returns_an_empty_body_as_empty_bytes(web_server):
    web_server.serve("/empty.csv", b"")

    assert urls.fetch(f"{web_server.url}/empty.csv") == b""


def test_fetch_reads_a_body_in_many_chunks(web_server):
    body = ("day,n\n" + "".join(f"2026-07-01,{n}\n" for n in range(40_000))).encode()
    assert len(body) > 3 * 64 * 1024
    web_server.serve("/big.csv", body)

    assert urls.fetch(f"{web_server.url}/big.csv") == body


def test_fetch_reads_an_answer_that_has_no_length_and_comes_in_chunks(web_server):
    body = ("day,n\n" + "".join(f"2026-07-01,{n}\n" for n in range(30_000))).encode()
    web_server.serve("/big.csv", body, chunked=True, piece=50_000)

    assert urls.fetch(f"{web_server.url}/big.csv") == body


def test_fetch_stops_an_answer_in_chunks_that_goes_over_max_bytes(web_server):
    web_server.serve("/big.csv", b"x" * 400_000, chunked=True, piece=100_000)

    with pytest.raises(ConfigError, match="has more than max_bytes, which is 150000 bytes"):
        urls.fetch(f"{web_server.url}/big.csv", max_bytes=150_000)


def test_fetch_reads_a_compressed_answer_in_chunks(web_server):
    body = ("day,n\n" + "".join(f"2026-07-01,{n}\n" for n in range(20_000))).encode()
    web_server.serve("/big.csv", gzip.compress(body), chunked=True, piece=5_000, headers={"Content-Encoding": "gzip"})

    assert urls.fetch(f"{web_server.url}/big.csv") == body


@pytest.mark.parametrize("content_type", ["text/csv", "text/csv; charset=utf-8", "text/plain", "application/csv",
                                          "application/octet-stream", "application/json", None])
def test_fetch_takes_any_type_but_a_web_page(web_server, content_type):
    web_server.serve("/a.csv", "a\n1\n", content_type=content_type)

    assert urls.fetch(f"{web_server.url}/a.csv") == b"a\n1\n"


@pytest.mark.parametrize("content_type", ["text/html", "text/html; charset=utf-8", "TEXT/HTML",
                                          " text/html ;charset=UTF-8", "application/xhtml+xml"])
def test_fetch_refuses_a_web_page_and_says_what_to_do(web_server, content_type):
    web_server.serve("/signin", "<html>Sign in</html>", content_type=content_type)

    with pytest.raises(ConfigError) as caught:
        urls.fetch(f"{web_server.url}/signin")

    message = str(caught.value)
    assert "answered with a web page, not a CSV file" in message
    assert "not shared by link answers with a sign-in page" in message
    assert message.endswith("so share it with anyone who has the link as a viewer")
    assert f"{web_server.url}/signin" in message
    assert "Sign in" not in message


# ---------------------------------------------------------------------- the answer is not a 2xx


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 451])
def test_a_status_that_another_try_does_not_fix_is_a_config_error(web_server, status):
    web_server.serve("/a.csv", "gone", status=status)

    with pytest.raises(ConfigError) as caught:
        urls.fetch(f"{web_server.url}/a.csv")

    assert f"answered HTTP {status}" in str(caught.value)
    assert "shared with anyone who has the link" in str(caught.value)
    assert not isinstance(caught.value, OSError)


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504, 599])
def test_a_status_that_may_pass_is_an_os_error(web_server, status):
    web_server.serve("/a.csv", "try later", status=status)

    with pytest.raises(ConnectionError) as caught:
        urls.fetch(f"{web_server.url}/a.csv")

    assert f"answered HTTP {status}" in str(caught.value)
    assert isinstance(caught.value, OSError)


@pytest.mark.parametrize("status", [300, 304, 600])
def test_a_status_that_is_not_a_2xx_is_not_read_as_a_file(web_server, status):
    web_server.serve("/a.csv", b"", status=status)

    with pytest.raises((ConfigError, ConnectionError, OSError)) as caught:
        urls.fetch(f"{web_server.url}/a.csv")

    assert f"HTTP {status}" in str(caught.value)


@pytest.mark.parametrize("status", [200, 201, 203, 206, 299])
def test_a_2xx_is_read(web_server, status):
    web_server.serve("/a.csv", "a\n1\n", status=status)

    assert urls.fetch(f"{web_server.url}/a.csv") == b"a\n1\n"


@pytest.mark.parametrize("reply, retriable", [
    (dict(status=503), True), (dict(status=500), True), (dict(status=429), True), (dict(status=408), True),
    (dict(status=403), False), (dict(status=404), False),
    (dict(content_type="text/html"), False),
    (dict(delay=0.5), True),
])
def test_a_workflow_retries_what_may_pass_and_not_what_does_not(web_server, reply, retriable):
    web_server.serve("/a.csv", "a\n1\n", **reply)

    with pytest.raises((ConfigError, OSError)) as caught:
        urls.fetch(f"{web_server.url}/a.csv", timeout=0.2)

    assert is_retriable(caught.value) is retriable


def test_a_body_over_max_bytes_and_a_redirect_that_is_not_allowed_are_not_retried(web_server):
    web_server.serve("/big.csv", "x" * 100)
    web_server.serve("/a", b"", status=302, headers={"Location": "http://example.invalid/x.csv"})

    with pytest.raises(ConfigError) as too_big:
        urls.fetch(f"{web_server.url}/big.csv", max_bytes=10)
    with pytest.raises(ConfigError) as redirected:
        urls.fetch(f"{web_server.url}/a")

    assert not is_retriable(too_big.value) and not is_retriable(redirected.value)


def test_a_host_that_cannot_be_reached_and_an_answer_that_breaks_off_are_retried(web_server):
    web_server.serve("/cut.csv", "a,b\n", headers={"Content-Length": "1000"})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    with pytest.raises(ConnectionError) as refused:
        urls.fetch(f"http://127.0.0.1:{port}/a.csv")
    with pytest.raises(ConnectionError) as cut:
        urls.fetch(f"{web_server.url}/cut.csv")

    assert is_retriable(refused.value) and is_retriable(cut.value)


# ---------------------------------------------------------------------- redirects


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_followed_with_a_get(web_server, status):
    web_server.serve("/export", b"", status=status, headers={"Location": "/real?part=2"})
    web_server.serve("/real", "a\n1\n")

    assert urls.fetch(f"{web_server.url}/export?format=csv") == b"a\n1\n"
    assert [path for path, _ in web_server.seen] == ["/export?format=csv", "/real?part=2"]


def test_a_redirect_to_an_absolute_address_is_followed(web_server):
    web_server.serve("/export", b"", status=307, headers={"Location": f"{web_server.url}/real"})
    web_server.serve("/real", "a\n1\n")

    assert urls.fetch(f"{web_server.url}/export") == b"a\n1\n"


def test_a_chain_of_redirects_is_followed_up_to_the_limit(web_server):
    assert urls.MAX_REDIRECTS == 5
    for number in range(urls.MAX_REDIRECTS):
        web_server.serve(f"/hop{number}", b"", status=302, headers={"Location": f"/hop{number + 1}"})
    web_server.serve(f"/hop{urls.MAX_REDIRECTS}", "a\n1\n")

    assert urls.fetch(f"{web_server.url}/hop0") == b"a\n1\n"
    assert len(web_server.seen) == urls.MAX_REDIRECTS + 1


def test_one_redirect_too_many_is_a_config_error(web_server):
    for number in range(urls.MAX_REDIRECTS + 1):
        web_server.serve(f"/hop{number}", b"", status=302, headers={"Location": f"/hop{number + 1}"})
    web_server.serve(f"/hop{urls.MAX_REDIRECTS + 1}", "a\n1\n")

    with pytest.raises(ConfigError) as caught:
        urls.fetch(f"{web_server.url}/hop0")

    assert f"redirected more than {urls.MAX_REDIRECTS} times" in str(caught.value)
    assert len(web_server.seen) == urls.MAX_REDIRECTS + 1


def test_a_redirect_loop_ends(web_server):
    web_server.serve("/a", b"", status=302, headers={"Location": "/b"})
    web_server.serve("/b", b"", status=302, headers={"Location": "/a"})

    with pytest.raises(ConfigError, match="redirected more than 5 times"):
        urls.fetch(f"{web_server.url}/a")


def test_a_redirect_with_no_location_is_an_answer_that_is_not_read(web_server):
    web_server.serve("/a", b"", status=302)

    with pytest.raises(ConfigError, match="answered HTTP 302"):
        urls.fetch(f"{web_server.url}/a")


@pytest.mark.parametrize("target, words", [
    ("http://example.invalid/x.csv", "plain http, which is only for 127.0.0.1 and localhost"),
    ("ftp://example.invalid/x.csv", "does not start with https://"),
    ("https://user:pw@example.invalid/x.csv", "user name or a password"),
    ("http://user:pw@127.0.0.1/x.csv", "user name or a password"),
])
def test_a_redirect_to_an_address_that_is_not_allowed_is_refused_before_it_is_followed(web_server, target, words):
    web_server.serve("/a", b"", status=302, headers={"Location": target})

    with pytest.raises(ConfigError) as caught:
        urls.fetch(f"{web_server.url}/a")

    message = str(caught.value)
    assert "redirected to" in message and words in message
    assert "pw" not in message
    assert [path for path, _ in web_server.seen] == ["/a"]


def test_a_redirect_to_another_host_without_a_scheme_takes_the_scheme_of_the_address(web_server):
    web_server.serve("/a", b"", status=302, headers={"Location": "//example.invalid/x.csv"})

    with pytest.raises(ConfigError) as caught:
        urls.fetch(f"{web_server.url}/a")

    assert "redirected to http://example.invalid/x.csv, but the address is plain http" in str(caught.value)
    assert [path for path, _ in web_server.seen] == ["/a"]


def test_a_redirect_message_has_no_query_of_either_address(web_server):
    web_server.serve("/a", b"", status=302, headers={"Location": f"http://example.invalid/x.csv?token={SECRET}"})

    with pytest.raises(ConfigError) as caught:
        urls.fetch(f"{web_server.url}/a?key={SECRET}")

    assert SECRET not in str(caught.value)
    assert "http://example.invalid/x.csv" in str(caught.value) and f"{web_server.url}/a" in str(caught.value)


# ---------------------------------------------------------------------- the size and the time


def test_a_body_of_max_bytes_is_read(web_server):
    web_server.serve("/a.csv", "x" * 1000)

    assert urls.fetch(f"{web_server.url}/a.csv", max_bytes=1000) == b"x" * 1000


def test_a_body_over_max_bytes_is_refused_and_the_message_says_the_limit(web_server):
    web_server.serve("/a.csv", "x" * 1001)

    with pytest.raises(ConfigError) as caught:
        urls.fetch(f"{web_server.url}/a.csv", max_bytes=1000)

    assert "has more than max_bytes, which is 1000 bytes" in str(caught.value)
    assert "Raise max_bytes" in str(caught.value)


def test_a_body_over_max_bytes_stops_the_download_at_the_limit(web_server):
    body = b"x" * (5 * 1024 * 1024)
    web_server.serve("/big.csv", body)

    with pytest.raises(ConfigError, match="max_bytes"):
        urls.fetch(f"{web_server.url}/big.csv", max_bytes=100_000)


def test_max_bytes_counts_the_decompressed_body(web_server):
    bomb = gzip.compress(b"0" * 5_000_000)
    assert len(bomb) < 10_000
    web_server.serve("/bomb.csv", bomb, headers={"Content-Encoding": "gzip"})

    with pytest.raises(ConfigError, match="max_bytes"):
        urls.fetch(f"{web_server.url}/bomb.csv", max_bytes=1_000_000)


def test_a_compressed_answer_is_decompressed(web_server):
    web_server.serve("/a.csv", gzip.compress(b"a\n1\n"), headers={"Content-Encoding": "gzip"})

    assert urls.fetch(f"{web_server.url}/a.csv") == b"a\n1\n"


def test_the_defaults_are_30_seconds_and_50_mebibytes():
    assert urls.DEFAULT_TIMEOUT_S == 30.0
    assert urls.DEFAULT_MAX_BYTES == 50 * 1024 * 1024


def test_a_server_that_is_slow_to_answer_times_out(web_server):
    web_server.serve("/slow.csv", "a\n1\n", delay=1.0)
    began = time.monotonic()

    with pytest.raises(TimeoutError) as caught:
        urls.fetch(f"{web_server.url}/slow.csv", timeout=0.2)

    assert time.monotonic() - began < 0.9
    assert str(caught.value) == f"{web_server.url}/slow.csv did not answer in 0.2 seconds"
    assert isinstance(caught.value, OSError)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_server_that_sends_a_little_at_a_time_times_out_as_a_whole(web_server, monkeypatch):
    monkeypatch.setattr(urls, "_CHUNK", 4)  # so that every 4 bytes are a part of the body
    web_server.serve("/drip.csv", "a,b\n" * 50, pause=0.05, piece=4)  # about 2.5 seconds in all
    began = time.monotonic()

    with pytest.raises(TimeoutError) as caught:
        urls.fetch(f"{web_server.url}/drip.csv", timeout=0.4)

    assert 0.3 < time.monotonic() - began < 1.5
    assert "was not read in 0.4 seconds" in str(caught.value)


def test_a_download_within_the_timeout_is_read(web_server):
    web_server.serve("/slowish.csv", "a,b\n" * 10, pause=0.01, piece=4)

    assert urls.fetch(f"{web_server.url}/slowish.csv", timeout=5) == b"a,b\n" * 10


def test_a_timeout_of_the_whole_download_counts_the_redirects(web_server):
    web_server.serve("/a", b"", status=302, headers={"Location": "/b"}, delay=0.3)
    web_server.serve("/b", "a\n1\n", delay=0.3)

    with pytest.raises(TimeoutError):
        urls.fetch(f"{web_server.url}/a", timeout=0.5)


def test_a_timeout_that_has_run_out_before_a_hop_starts_is_a_timeout(web_server, monkeypatch):
    clock = iter([100.0, 100.2, 105.0])  # the deadline, the first hop, the second hop
    monkeypatch.setattr(urls, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))
    web_server.serve("/a", b"", status=302, headers={"Location": "/b"})
    web_server.serve("/b", "a\n1\n")

    with pytest.raises(TimeoutError, match="was not read in 1 seconds"):
        urls.fetch(f"{web_server.url}/a", timeout=1)

    assert [path for path, _ in web_server.seen] == ["/a"]


def test_a_hop_that_starts_with_no_time_left_is_a_timeout(scripted, monkeypatch):
    adapter = scripted({SHEET: (302, {"Location": "https://files.example.com/a.csv"}, b"")})
    clock = iter([0.0, 1.0, 10.0] + [10.0] * 10)  # the second hop starts at the very moment that the time is up
    monkeypatch.setattr(urls, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))

    with pytest.raises(TimeoutError, match="was not read in 10 seconds"):
        urls.fetch(SHEET, timeout=10)

    assert [url for url, _ in adapter.sent] == [SHEET]


def test_a_part_of_the_body_that_arrives_at_the_moment_that_the_time_is_up_is_still_read(scripted, monkeypatch):
    scripted({SHEET: (200, {}, "a\n1\n")})
    clock = iter([0.0, 1.0] + [10.0] * 10)  # the deadline is at 10, and the part arrives at 10
    monkeypatch.setattr(urls, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))

    assert urls.fetch(SHEET, timeout=10) == b"a\n1\n"


def test_a_part_of_the_body_that_arrives_after_the_time_is_up_is_a_timeout(scripted, monkeypatch):
    scripted({SHEET: (200, {}, "a\n1\n")})
    clock = iter([0.0, 1.0] + [10.5] * 10)
    monkeypatch.setattr(urls, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))

    with pytest.raises(TimeoutError, match="was not read in 10 seconds"):
        urls.fetch(SHEET, timeout=10)


def test_a_failure_of_the_body_at_the_moment_that_the_time_is_up_is_a_timeout(scripted, monkeypatch):
    class Broken(io.BytesIO):
        def read(self, size=-1):
            raise requests.exceptions.ConnectionError("reset")

    adapter = scripted({SHEET: (200, {}, b"")})
    original = adapter.send

    def send(request, **kwargs):
        response = original(request, **kwargs)
        response.raw = Broken()
        return response

    adapter.send = send
    clock = iter([0.0, 1.0] + [10.0] * 10)  # the deadline is at 10, and the answer breaks off at 10
    monkeypatch.setattr(urls, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))

    with pytest.raises(TimeoutError, match="was not read in 10 seconds"):
        urls.fetch(SHEET, timeout=10)


def test_the_answer_that_breaks_off_is_an_os_error(web_server):
    web_server.serve("/cut.csv", "a,b\n1,2\n", headers={"Content-Length": "1000"})

    with pytest.raises(ConnectionError) as caught:
        urls.fetch(f"{web_server.url}/cut.csv")

    assert str(caught.value) == f"the answer of {web_server.url}/cut.csv broke off (ChunkedEncodingError)"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_host_that_cannot_be_reached_is_an_os_error_that_does_not_repeat_the_address():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # nothing listens here once the socket is closed

    with pytest.raises(ConnectionError) as caught:
        urls.fetch(f"http://127.0.0.1:{port}/sheet.csv?token={SECRET}")

    message = str(caught.value)
    assert message.startswith(f"cannot reach http://127.0.0.1:{port}/sheet.csv (")
    assert SECRET not in message and "Max retries" not in message
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


# ---------------------------------------------------------------------- what is sent, and what is not


def test_nothing_from_a_netrc_is_sent(web_server, tmp_path, monkeypatch):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine 127.0.0.1 login ann password hunter2\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    assert requests.utils.get_netrc_auth(f"{web_server.url}/a.csv") == ("ann", "hunter2"), "requests reads the file"
    web_server.serve("/a.csv", "a\n1\n")

    urls.fetch(f"{web_server.url}/a.csv")

    sent = {name.lower() for name in web_server.seen[0][1]}
    assert "authorization" not in sent


def test_no_cookie_is_kept_after_a_download(web_server):
    web_server.serve("/a.csv", "a\n1\n", headers={"Set-Cookie": "session=abc; Path=/"})

    urls.fetch(f"{web_server.url}/a.csv")
    urls.fetch(f"{web_server.url}/a.csv")

    assert all("Cookie" not in headers for _, headers in web_server.seen)


def test_this_machine_is_not_reached_through_a_proxy_of_the_environment(web_server, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    web_server.serve("/a.csv", "a\n1\n")

    assert urls.fetch(f"{web_server.url}/a.csv") == b"a\n1\n"
    assert len(web_server.seen) == 1


# ====================================================================== https, through a scripted adapter


class Scripted(requests.adapters.BaseAdapter):
    """Answers for ``requests`` from a script, ``{url: (status, headers, body)}``, and opens no socket."""

    def __init__(self, script):
        super().__init__()
        self.script = script
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append((request.url, kwargs))
        status, headers, body = self.script[request.url]
        response = requests.Response()
        response.status_code = status
        response.headers = CaseInsensitiveDict({"Content-Type": "text/csv", **headers})
        response.raw = io.BytesIO(body if isinstance(body, bytes) else body.encode())
        response.url = request.url
        response.request = request
        return response

    def close(self):
        pass


@pytest.fixture
def scripted(monkeypatch):
    def install(script):
        adapter = Scripted(script)
        monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: adapter)
        return adapter

    return install


SHEET = "https://docs.example.com/spreadsheets/d/ID/export?format=csv&gid=0"


def test_an_https_address_is_read(scripted):
    adapter = scripted({SHEET: (200, {}, "a\n1\n")})

    assert urls.fetch(SHEET) == b"a\n1\n"
    assert [url for url, _ in adapter.sent] == [SHEET]


def test_the_certificate_of_an_https_host_is_checked(scripted):
    adapter = scripted({SHEET: (200, {}, "a\n1\n")})

    urls.fetch(SHEET)

    kwargs = adapter.sent[0][1]
    assert kwargs["verify"] is True
    assert not kwargs.get("cert")


def test_the_proxy_of_the_environment_is_used_for_a_public_host_and_not_for_this_machine(scripted, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.com:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.com:3128")
    local = "http://127.0.0.1:8080/a.csv"
    adapter = scripted({SHEET: (200, {}, "a\n1\n"), local: (200, {}, "a\n1\n")})

    urls.fetch(SHEET)
    urls.fetch(local)

    public_proxies, local_proxies = adapter.sent[0][1]["proxies"], adapter.sent[1][1]["proxies"]
    assert public_proxies.get("https") == "http://proxy.example.com:3128"
    assert not local_proxies.get("http") and not local_proxies.get("https")


def test_a_google_style_redirect_to_another_https_host_is_followed(scripted):
    final = "https://doc-0k-sheets.googleusercontent.example/export/abc/def?id=ID&format=csv"
    adapter = scripted({SHEET: (307, {"Location": final}, b""), final: (200, {}, "a\n1\n")})

    assert urls.fetch(SHEET) == b"a\n1\n"
    assert [url for url, _ in adapter.sent] == [SHEET, final]


def test_a_relative_redirect_of_an_https_address_stays_on_its_host(scripted):
    adapter = scripted({SHEET: (302, {"Location": "/next/part.csv"}, b""),
                        "https://docs.example.com/next/part.csv": (200, {}, "a\n1\n")})

    assert urls.fetch(SHEET) == b"a\n1\n"
    assert adapter.sent[1][0] == "https://docs.example.com/next/part.csv"


@pytest.mark.parametrize("target", ["http://127.0.0.1:8080/secret.csv", "http://localhost:8080/secret.csv"])
def test_an_https_address_cannot_redirect_to_a_service_on_this_machine(scripted, target):
    adapter = scripted({SHEET: (302, {"Location": target}, b""), target: (200, {}, "a\n1\n")})

    with pytest.raises(ConfigError) as caught:
        urls.fetch(SHEET)

    assert "which a redirect from another host may not go to" in str(caught.value)
    assert [url for url, _ in adapter.sent] == [SHEET]


def test_an_https_address_cannot_redirect_to_plain_http(scripted):
    target = "http://files.example.com/a.csv"
    adapter = scripted({SHEET: (301, {"Location": target}, b""), target: (200, {}, "a\n1\n")})

    with pytest.raises(ConfigError, match="plain http, which is only for 127.0.0.1 and localhost"):
        urls.fetch(SHEET)

    assert [url for url, _ in adapter.sent] == [SHEET]


def test_this_machine_may_redirect_to_itself_over_http(web_server):
    web_server.serve("/a", b"", status=302, headers={"Location": f"{web_server.url}/b"})
    web_server.serve("/b", "a\n1\n")

    assert urls.fetch(f"{web_server.url}/a") == b"a\n1\n"


def test_a_local_address_may_redirect_to_an_https_address(scripted):
    start, target = "http://127.0.0.1:8080/a", "https://files.example.com/a.csv"
    adapter = scripted({start: (302, {"Location": target}, b""), target: (200, {}, "a\n1\n")})

    assert urls.fetch(start) == b"a\n1\n"
    assert [url for url, _ in adapter.sent] == [start, target]


@pytest.mark.parametrize("error", [requests.exceptions.SSLError("certificate verify failed for /ID?token=" + SECRET),
                                   requests.exceptions.ConnectionError("Max retries, url /ID?token=" + SECRET),
                                   requests.exceptions.ChunkedEncodingError("x"),
                                   requests.exceptions.TooManyRedirects("y"),
                                   requests.exceptions.ProxyError("z")])
def test_an_error_of_requests_is_an_os_error_with_its_type_and_not_its_text(monkeypatch, error):
    class Broken(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            raise error

        def close(self):
            pass

    monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: Broken())

    with pytest.raises(ConnectionError) as caught:
        urls.fetch(f"{SHEET}&token={SECRET}")

    kind = type(error).__name__
    assert str(caught.value) == f"cannot reach https://docs.example.com/spreadsheets/d/ID/export ({kind})"
    assert SECRET not in _error_text(caught.value)


@pytest.mark.parametrize("error", [requests.exceptions.ConnectTimeout("a"), requests.exceptions.ReadTimeout("b"),
                                   requests.exceptions.Timeout("c")])
def test_a_timeout_of_requests_is_a_timeout(monkeypatch, error):
    class Slow(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            raise error

        def close(self):
            pass

    monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: Slow())

    with pytest.raises(TimeoutError, match="did not answer in 7 seconds"):
        urls.fetch(SHEET, timeout=7)


@pytest.mark.parametrize("error", [requests.exceptions.InvalidURL("bad"), requests.exceptions.InvalidSchema("bad"),
                                   requests.exceptions.MissingSchema("bad")])
def test_an_address_that_requests_refuses_is_a_config_error(monkeypatch, error):
    class Refusing(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            raise error

        def close(self):
            pass

    monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: Refusing())

    with pytest.raises(ConfigError, match="the address is not valid") as caught:
        urls.fetch(SHEET)

    assert not isinstance(caught.value, OSError)
    assert str(caught.value) == ("cannot read https://docs.example.com/spreadsheets/d/ID/export, because the address "
                                 "is not valid")
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_failure_while_the_body_is_read_that_comes_after_the_deadline_is_a_timeout(scripted, monkeypatch):
    class Slow(io.BytesIO):
        def read(self, size=-1):
            raise requests.exceptions.ConnectionError("read timed out")

    adapter = scripted({SHEET: (200, {}, b"")})
    original = adapter.send

    def send(request, **kwargs):
        response = original(request, **kwargs)
        response.raw = Slow()
        return response

    adapter.send = send
    clock = iter([0.0, 0.1, 50.0])  # the deadline, the request, the failure of the body
    monkeypatch.setattr(urls, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))

    with pytest.raises(TimeoutError, match="was not read in 5 seconds"):
        urls.fetch(SHEET, timeout=5)


def test_a_failure_while_the_body_is_read_before_the_deadline_is_a_broken_answer(scripted):
    class Broken(io.BytesIO):
        def read(self, size=-1):
            raise requests.exceptions.ConnectionError("reset")

    adapter = scripted({SHEET: (200, {}, b"")})
    original = adapter.send

    def send(request, **kwargs):
        response = original(request, **kwargs)
        response.raw = Broken()
        return response

    adapter.send = send

    with pytest.raises(ConnectionError, match="the answer of https://docs.example.com/.*/export broke off"):
        urls.fetch(SHEET, timeout=500)


class Endless(io.BytesIO):
    """A body that has no end. The test fails when more than 50 parts of it are read."""

    def __init__(self):
        super().__init__()
        self.reads = 0

    def read(self, size=-1):
        self.reads += 1
        assert self.reads < 50, "the body was read as a whole, and not as a stream that max_bytes can stop"
        return b"x" * (size if size and size > 0 else 16 * 1024)


def test_the_body_is_read_as_a_stream_so_that_max_bytes_stops_it(scripted):
    adapter = scripted({SHEET: (200, {}, b"")})
    original, endless = adapter.send, Endless()

    def send(request, **kwargs):
        response = original(request, **kwargs)
        response.raw = endless
        return response

    adapter.send = send

    with pytest.raises(ConfigError, match="has more than max_bytes, which is 100000 bytes"):
        urls.fetch(SHEET, max_bytes=100_000)

    assert endless.reads == 7, "7 parts of 16 KiB are over 100000 bytes, and the next part is never read"


def test_each_hop_waits_for_what_is_left_of_the_timeout(scripted, monkeypatch):
    final = "https://files.example.com/a.csv"
    adapter = scripted({SHEET: (302, {"Location": final}, b""), final: (200, {}, "a\n1\n")})
    clock = iter([0.0, 1.0, 3.0] + [3.5] * 10)  # the deadline is at 10, the first hop starts at 1 and the second at 3
    monkeypatch.setattr(urls, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))

    urls.fetch(SHEET, timeout=10)

    assert [kwargs["timeout"] for _, kwargs in adapter.sent] == [9.0, 7.0]


# ====================================================================== an address that is not allowed


@pytest.mark.parametrize("url", [f"http://example.invalid/a.csv?token={SECRET}",
                                 f"ftp://example.invalid/a.csv?token={SECRET}",
                                 f"https://u:p@example.invalid/a.csv?token={SECRET}",
                                 f"https://example.invalid/a b.csv?token={SECRET}",
                                 f"https://example.invalid:99999/a.csv?token={SECRET}"])
def test_fetch_refuses_an_address_that_is_not_allowed_before_any_request(scripted, url):
    adapter = scripted({})

    with pytest.raises(ConfigError) as caught:
        urls.fetch(url)

    assert adapter.sent == []
    assert "cannot read" in str(caught.value) and "because the address" in str(caught.value)
    assert SECRET not in str(caught.value) and "p@" not in str(caught.value)


def test_the_message_of_an_address_that_is_not_allowed_is_exactly_what_it_says(scripted):
    scripted({})

    with pytest.raises(ConfigError) as caught:
        urls.fetch("http://example.invalid/a.csv?token=" + SECRET)

    assert str(caught.value) == ("cannot read http://example.invalid/a.csv, because the address is plain http, which "
                                 "is only for 127.0.0.1 and localhost")


def test_the_whole_module_needs_only_the_standard_library_to_import():
    import subprocess
    import sys

    code = "import sys, local_data_platform.urls; sys.exit(1 if 'requests' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
