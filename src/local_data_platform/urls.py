"""Reading a file over ``http://`` and ``https://``: which addresses may be read, and the download.

A ``CSV`` source may be an ``https://`` address, such as the export link of a Google Sheet that is shared by link::

    from local_data_platform.format.csv import CSV

    table = CSV("sales", "https://docs.google.com/spreadsheets/d/SHEET_ID/export?format=csv&gid=0").get()

This module holds the rules for the address and the download. It needs only the standard library when it is imported,
and imports ``requests`` when a download starts.

**The address.** It has to be ``https://``. Plain ``http://`` is only for 127.0.0.1 and localhost, where a test or a
stub on this machine listens. It has no user name and no password in it, and in a spec its query and its fragment have
no parameter that looks like a secret, see :func:`secret_parameter`. A text that has a space or a line break in it is
not an address.

**The download.** It sends no credentials of its own and nothing from ``~/.netrc``, and it keeps no cookie after it
ends. It follows at most 5 redirects, and each has to go to an ``https://`` address, so an answer cannot send it back
to plain http, or from a public host to a service on this machine. The whole download has ``timeout`` seconds, 30 by
default. The host has that long to connect and to send each part of the answer, and the time of the whole download is
checked as each part of the body arrives, so a body that comes too slowly ends at the first part after the time is up.
The body has at most ``max_bytes`` bytes after it is decompressed, 50 MiB by default, and a download that goes past
that stops there. An answer that is a web page is refused, because a sheet that is not shared by link answers with a
sign-in page, and its text would otherwise be read as rows.

**What it raises.** An error that another try can fix is an ``OSError``, so a workflow that has ``retries`` tries
again. That is a connection that fails, a timeout, an answer that breaks off, and the HTTP statuses 408, 425, 429 and
5xx. Any other failure is a :class:`~local_data_platform.exceptions.ConfigError`, which is never retried: an address
that is not allowed, a status such as 403 or 404, a web page, a body over ``max_bytes`` and too many redirects. No
message holds the query of the address, or a user name and a password, and none holds the text of an error of
``requests``, which repeats the address.
"""

import re
import time
import urllib.parse

from local_data_platform.exceptions import ConfigError

LOCAL_HOSTS = ("127.0.0.1", "localhost")
"""The only hosts that may be reached over plain ``http://``. The alert channels follow the same rule."""

DEFAULT_TIMEOUT_S = 30.0
"""The seconds that a download may take, unless the source sets ``timeout``."""

DEFAULT_MAX_BYTES = 50 * 1024 * 1024
"""The bytes that a download may have after it is decompressed, unless the source sets ``max_bytes``."""

MAX_REDIRECTS = 5
"""The redirects that a download follows. The export link of a Google Sheet has one."""

_CHUNK = 16 * 1024
_TRANSIENT_STATUSES = (408, 425, 429)
_PAGES = ("text/html", "application/xhtml+xml")
_SCHEME = re.compile(r"https?://", re.IGNORECASE)
_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f]")
_SECRET_WORDS = ("password", "passwd", "secret", "token", "credential", "api_key", "apikey", "private_key", "signature")
_SECRET_NAMES = ("key", "sig", "auth", "pwd", "access_key", "accesskey")
_NO_PROXIES = {"http": None, "https": None, "all": None}  # a value of None takes the proxy of the environment away


def is_http(uri: object) -> bool:
    """Whether ``uri`` is a text that starts with ``http://`` or ``https://``, in any case."""
    return isinstance(uri, str) and _SCHEME.match(uri) is not None


def _host_of(url: str) -> str | None:
    try:
        return urllib.parse.urlsplit(url).hostname
    except ValueError:
        return None


def label(url: str) -> str:
    """The address as an error or a log line shows it, with no user name, password, query or fragment."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "the address"
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}{port}{parts.path}"


def problem(url: str, *, local_ok: bool = True) -> str | None:
    """Why an address may not be read, as words that follow ``the address``, or ``None`` when it may.

    It has to be ``https://``. Plain ``http://`` is for 127.0.0.1 and localhost only, and only when ``local_ok`` is
    true, which a redirect from a public address makes false. The reason never holds the address.
    """
    if _UNSAFE.search(url):
        return "has a space, a line break or another control character in it"
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # reading it raises ValueError for a port that is not a number or is out of range
    except ValueError:
        return "is not valid"
    if parts.scheme not in ("http", "https"):
        return "does not start with https://"
    if not parts.hostname:
        return "has no host name"
    if "@" in parts.netloc:
        return "has a user name or a password in it, and a secret is not written in an address"
    if parts.scheme == "http":
        if parts.hostname not in LOCAL_HOSTS:
            return "is plain http, which is only for 127.0.0.1 and localhost"
        if not local_ok:
            return "is plain http on this machine, which a redirect from another host may not go to"
    return None


def secret_parameter(url: str) -> str | None:
    """The name of the first parameter, in the query or the fragment, that looks like a secret, or ``None``.

    A name is read in lower case with every run of other characters as one ``_``. It looks like a secret when it holds
    ``password``, ``passwd``, ``secret``, ``token``, ``credential``, ``api_key``, ``apikey``, ``private_key`` or
    ``signature``, as ``access_token`` and ``X-Amz-Signature`` do, or is ``key``, ``sig``, ``auth``, ``pwd``,
    ``access_key`` or ``accesskey``. A parameter such as ``format``, ``gid`` or ``primary_key`` does not.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    for text in (parts.query, parts.fragment):
        for name, _ in urllib.parse.parse_qsl(text, keep_blank_values=True):
            normal = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
            if normal in _SECRET_NAMES or any(word in normal for word in _SECRET_WORDS):
                return name
    return None


def fetch(url: str, *, timeout: float | None = None, max_bytes: int | None = None) -> bytes:
    """Download ``url`` and return its body, as the module says.

    Args:
        url: An ``https://`` address, or ``http://`` for 127.0.0.1 and localhost.
        timeout: The seconds that the whole download may take. :data:`DEFAULT_TIMEOUT_S` by default.
        max_bytes: The most bytes of the body, after it is decompressed. :data:`DEFAULT_MAX_BYTES` by default.

    Returns:
        The bytes of the answer.

    Raises:
        ConfigError: If the address is not allowed, the answer is a status that another try does not fix, a web page,
            more than ``max_bytes`` bytes, or too many redirects.
        TimeoutError: If the download took more than ``timeout`` seconds.
        ConnectionError: If the host could not be reached, the answer broke off, or the status was 408, 425, 429 or 5xx.
    """
    import requests

    seconds = DEFAULT_TIMEOUT_S if timeout is None else timeout
    limit = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
    reason = problem(url)
    if reason:
        raise ConfigError(f"cannot read {label(url)}, because the address {reason}")
    deadline = time.monotonic() + seconds
    local = _host_of(url) in LOCAL_HOSTS
    current = url
    with requests.Session() as session:
        session.auth = _no_auth  # a netrc entry is not sent
        for _ in range(MAX_REDIRECTS + 1):
            response = _get(session, current, deadline, seconds)
            with response:
                target = session.get_redirect_target(response)
                if target is None:
                    return _body(response, current, deadline, seconds, limit)
                target = urllib.parse.urljoin(current, target)
                reason = problem(target, local_ok=local)
                if reason:
                    raise ConfigError(f"{label(current)} redirected to {label(target)}, but the address {reason}")
                current = target
    raise ConfigError(f"{label(url)} redirected more than {MAX_REDIRECTS} times")


def _no_auth(request):
    return request


def _get(session, url: str, deadline: float, seconds: float):
    import requests

    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError(f"{label(url)} was not read in {seconds:g} seconds")
    proxies = _NO_PROXIES if _host_of(url) in LOCAL_HOSTS else None  # this machine is not reached through a proxy
    try:
        return session.get(url, stream=True, allow_redirects=False, timeout=left, proxies=proxies)
    except requests.Timeout:
        raise TimeoutError(f"{label(url)} did not answer in {seconds:g} seconds") from None
    except ValueError:  # what requests raises for an address that it cannot use
        raise ConfigError(f"cannot read {label(url)}, because the address is not valid") from None
    except requests.RequestException as error:
        raise ConnectionError(f"cannot reach {label(url)} ({type(error).__name__})") from None


def _body(response, url: str, deadline: float, seconds: float, limit: int) -> bytes:
    import requests

    code = response.status_code
    where = label(url)
    if not 200 <= code < 300:
        if code in _TRANSIENT_STATUSES or 500 <= code <= 599:
            raise ConnectionError(f"{where} answered HTTP {code}, so another try may work")
        raise ConfigError(f"{where} answered HTTP {code}. Check the address, and that the file is shared with anyone "
                          "who has the link")
    kind = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if kind in _PAGES:
        raise ConfigError(f"{where} answered with a web page, not a CSV file. A Google Sheet that is not shared by "
                          "link answers with a sign-in page, so share it with anyone who has the link as a viewer")
    pieces: list[bytes] = []
    size = 0
    try:
        for piece in response.iter_content(chunk_size=_CHUNK):
            size += len(piece)
            if size > limit:
                raise ConfigError(f"{where} has more than max_bytes, which is {limit} bytes. Raise max_bytes if the "
                                  "file is meant to be that large")
            pieces.append(piece)
            if time.monotonic() > deadline:
                raise TimeoutError(f"{where} was not read in {seconds:g} seconds")
    except requests.RequestException as error:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{where} was not read in {seconds:g} seconds") from None
        raise ConnectionError(f"the answer of {where} broke off ({type(error).__name__})") from None
    return b"".join(pieces)


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT_S",
    "LOCAL_HOSTS",
    "MAX_REDIRECTS",
    "fetch",
    "is_http",
    "label",
    "problem",
    "secret_parameter",
]
