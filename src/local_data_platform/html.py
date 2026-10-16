"""The one page shell for the HTML that this project writes, and the helpers that build its markup.

``ldp gold docs`` writes the gold catalog as one page, and ``ldp status --html`` (0.1.5) and the reports of 0.1.6 are
written the same way, so they look alike and keep the same promises.

* **One file.** The CSS is inline, and so is the one small script. Nothing is loaded from another file or another host,
  and the page carries a Content-Security-Policy that makes the browser refuse to try. It opens from a folder, from an
  e-mail or from a wiki attachment with no network.
* **Light and dark.** The colours are tokens on ``:root``, and the dark ones follow ``prefers-color-scheme``.
* **Every value is escaped.** Markup is built with :func:`h`, which escapes every attribute value and every text child
  unless it is :class:`Raw`, which is markup that this module made. A value that a person typed, such as the description
  of a table, can never become markup.

::

    from local_data_platform import html

    body = html.h("p", "Owned by ", html.h("b", owner))
    text = html.page("Gold catalog", body, subtitle="3 tables")

The module imports nothing outside the standard library.
"""

from __future__ import annotations

import base64
import hashlib
import html as _html
import re
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from local_data_platform import __version__

CSS = """\
:root{color-scheme:light dark;--bg:#f6f6f3;--surface:#ffffff;--fg:#1c1c1a;--muted:#5b5b56;--border:#d9d9d2;\
--accent:#1d5fbf;--ok:#17692f;--ok-bg:#e3f3e8;--warn:#7d5200;--warn-bg:#fbefd0;--bad:#a6231b;--bad-bg:#fbe3e0;--code-bg:#eeeeea;--radius:8px;\
--font:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;\
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#131312;--surface:#1c1c1a;--fg:#ecece8;--muted:#a4a49d;--border:#363632;\
--accent:#80aaff;--ok:#74d48c;--ok-bg:#15301c;--warn:#f1c45e;--warn-bg:#392c0b;--bad:#ff8f86;--bad-bg:#3c1613;--code-bg:#262623}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 var(--font)}
.wrap{max-width:72rem;margin:0 auto;padding:0 16px}
header.top{background:var(--surface);border-bottom:1px solid var(--border);padding:20px 0}
h1{margin:0;font-size:1.6rem;line-height:1.2}
h2{margin:0 0 8px;font-size:1.2rem;line-height:1.3;overflow-wrap:anywhere}
p{margin:8px 0}
.sub,.muted{color:var(--muted)}
main{padding-bottom:32px}
footer{padding:16px;font-size:.85rem}
a{color:var(--accent)}
code,pre{font-family:var(--mono);font-size:.9em}
code{background:var(--code-bg);border-radius:4px;padding:1px 5px}
pre{background:var(--code-bg);border-radius:var(--radius);padding:10px 12px;overflow-x:auto;margin:8px 0}
pre code{background:none;padding:0}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin:16px 0}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.chip{display:inline-block;border:1px solid var(--border);border-radius:999px;padding:1px 10px;font-size:.85rem;\
background:var(--code-bg);color:var(--fg)}
.chip.ok{color:var(--ok);background:var(--ok-bg);border-color:transparent}
.chip.warn{color:var(--warn);background:var(--warn-bg);border-color:transparent}
.chip.bad{color:var(--bad);background:var(--bad-bg);border-color:transparent}
dl.facts{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:8px 0}
dl.facts dt{color:var(--muted)}
dl.facts dd{margin:0;overflow-wrap:anywhere}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.9rem}
caption{text-align:left;color:var(--muted);padding:4px 0}
th,td{text-align:left;vertical-align:top;padding:6px 10px;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:600;white-space:nowrap}
th.num{text-align:right}
td.num{text-align:right;font-variant-numeric:tabular-nums}
details{margin:12px 0}
summary{cursor:pointer;font-weight:600}
.search{display:flex;align-items:center;gap:12px;margin:16px 0}
.search input{flex:1;min-width:0;padding:8px 12px;border:1px solid var(--border);border-radius:var(--radius);\
background:var(--surface);color:var(--fg);font:inherit}
.sr{position:absolute;left:-9999px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
[hidden]{display:none!important}
@media (max-width:40rem){dl.facts{grid-template-columns:1fr}dl.facts dt{margin-top:6px}}
@media print{body{background:#fff;color:#000}.search{display:none}.card{break-inside:avoid}}
"""
"""The stylesheet of every page. It has no ``url(`` and no ``@import``, so it asks for nothing."""

FILTER_SCRIPT = """\
(function () {
  var box = document.getElementById("q"), count = document.getElementById("count");
  var items = Array.prototype.slice.call(document.querySelectorAll("[data-search]"));
  if (!box) return;
  function filter() {
    var words = box.value.toLowerCase().split(/\\s+/).filter(Boolean), shown = 0;
    items.forEach(function (item) {
      var hit = words.every(function (word) { return item.getAttribute("data-search").indexOf(word) !== -1; });
      item.hidden = !hit;
      if (hit) shown += 1;
    });
    if (count) count.textContent = shown + " of " + items.length + " shown";
  }
  box.addEventListener("input", filter);
  filter();
})();
"""
"""The script of a page with a search box. It hides the elements whose ``data-search`` text lacks any word typed."""

CHIP_KINDS = ("ok", "warn", "bad")
"""The colours of a chip, besides the neutral one."""

VOID_TAGS = frozenset({"area", "base", "br", "col", "hr", "img", "input", "link", "meta", "source", "wbr"})
"""Elements that have no content and no closing tag."""

_NAME = re.compile(r"[a-z][a-z0-9-]*")


class Raw(str):
    """Markup that is already safe, so :func:`h` does not escape it again.

    Only this module makes it. Never wrap a value that a person typed.
    """

    __slots__ = ()


def escape(value: Any) -> str:
    """``value`` as text that is safe inside an element and inside a quoted attribute, with quotes escaped.

    ``None`` is empty text. :class:`Raw` is escaped too, because a value that is escaped here is a value and not markup.
    """
    return "" if value is None else _html.escape(str(value), quote=True)


def _check_name(name: str, what: str) -> str:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        article = "an" if what[0] in "aeiou" else "a"
        raise ValueError(f"{name!r} is not {article} {what} name. Use lower case letters, digits and '-'")
    return name


def _attribute(name: str, value: Any) -> str:
    """`` name="value"`` for an attribute. A trailing ``_`` is dropped and the other ``_`` become ``-``."""
    name = _check_name(name[:-1] if name.endswith("_") else name.replace("_", "-"), "attribute")
    if value is None or value is False:
        return ""
    if value is True:
        return f" {name}"
    return f' {name}="{escape(value)}"'


def _render(child: Any) -> str:
    """A child as markup. :class:`Raw` is kept, lists and tuples are joined, and anything else is escaped text."""
    if child is None or child is False:
        return ""
    if isinstance(child, Raw):
        return str(child)
    if isinstance(child, (list, tuple, Iterator)):
        return "".join(_render(item) for item in child)
    return escape(child)


def h(tag: str, *children: Any, **attributes: Any) -> Raw:
    """One element.

    Args:
        tag: The element name, such as ``"p"``.
        children: Its content. Text is escaped, :class:`Raw` is kept, and lists, tuples and generators are flattened.
        attributes: Its attributes. A value is always escaped. ``True`` writes the bare attribute, and ``None`` or
            ``False`` leave it out. ``class_`` is ``class``, ``for_`` is ``for`` and ``data_search`` is ``data-search``.

    Returns:
        The element, as :class:`Raw`.

    Raises:
        ValueError: If a name is not a plain lower case name, or an element that has no content is given some.
    """
    _check_name(tag, "tag")
    parts = "".join(_attribute(name, value) for name, value in attributes.items())
    if tag in VOID_TAGS:
        if any(child is not None and child is not False for child in children):
            raise ValueError(f"<{tag}> has no content")
        return Raw(f"<{tag}{parts}>")
    return Raw(f"<{tag}{parts}>{''.join(_render(child) for child in children)}</{tag}>")


def join(*items: Any, separator: str = "\n") -> Raw:
    """Several children as one :class:`Raw`, one on each line. Text is escaped, as in :func:`h`."""
    return Raw(separator.join(rendered for rendered in (_render(item) for item in items) if rendered))


def chip(text: Any, kind: str | None = None) -> Raw:
    """A small label. ``kind`` is ``ok``, ``warn`` or ``bad``, or ``None`` for the neutral one."""
    if kind is not None and kind not in CHIP_KINDS:
        raise ValueError(f"a chip is one of {list(CHIP_KINDS)} or neutral, not {kind!r}")
    return h("span", text, class_=f"chip {kind}" if kind else "chip")


def facts(pairs: Iterable[tuple[str, Any]]) -> Raw:
    """A definition list of ``(label, value)``. A pair whose value is ``None`` or empty text is left out."""
    items = []
    for label, value in pairs:
        if value is None or (isinstance(value, str) and not value):
            continue
        items.append(h("dt", label))
        items.append(h("dd", value))
    return h("dl", items, class_="facts")


def details(summary: Any, *children: Any, open: bool = False) -> Raw:  # noqa: A002 - the attribute is called open
    """A section that opens and closes, closed unless ``open``."""
    return h("details", h("summary", summary), *children, open=open)


def table(headers: Sequence[Any], rows: Iterable[Sequence[Any]], *, caption: Any = None,
          right: Iterable[int] | None = None) -> Raw:
    """A table in a box that scrolls sideways on a narrow screen.

    Args:
        headers: The column headings.
        rows: The rows, each a sequence of cells. A cell is text, :class:`Raw` or a number.
        caption: A caption above the table.
        right: The columns to right-align, by position. By default a cell that is a number is right-aligned.
    """
    def number(cell: Any) -> bool:
        return isinstance(cell, (int, float)) and not isinstance(cell, bool)

    aligned = None if right is None else set(right)
    head = h("tr", [h("th", header, scope="col", class_="num" if aligned is not None and index in aligned else None)
                    for index, header in enumerate(headers)])
    body = [h("tr", [h("td", cell, class_="num" if (number(cell) if aligned is None else index in aligned) else None)
                     for index, cell in enumerate(row)]) for row in rows]
    return h("div", h("table", h("caption", caption) if caption is not None else None, h("thead", head),
                      h("tbody", body)), class_="scroll")


def search_box(label: str = "Search", placeholder: str = "Type words to filter") -> Raw:
    """A search box and a count of what it shows, for :data:`FILTER_SCRIPT` and items that have ``data_search``."""
    return h("div", h("label", label, for_="q", class_="sr"),
             h("input", type="search", id="q", placeholder=placeholder, autocomplete="off"),
             h("span", "", id="count", class_="muted", aria_live="polite"), class_="search")


def _hash(text: str) -> str:
    """The Content-Security-Policy source of an inline element, which is the hash of its exact text."""
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def page(title: str, *body: Any, subtitle: str = "", description: str = "", script: str = "", footer: str = "") -> str:
    """A whole page.

    The page has a header with the title, the body in ``<main>`` and a footer, and nothing else. Its policy
    (``default-src 'none'``) lets the browser run only this page's own stylesheet and script, so no request leaves it.

    Args:
        title: The title of the page and its heading. Text, which is escaped.
        body: The content: :class:`Raw` from :func:`h` and the helpers, or text, which is escaped.
        subtitle: A line under the heading.
        description: What the page is, for the ``description`` meta tag.
        script: The text of the one inline script, such as :data:`FILTER_SCRIPT`. It is allowed by its hash.
        footer: A line for the footer, before the name of this package and its version.

    Returns:
        The page as text, starting with the doctype.

    Raises:
        ValueError: If the script could end its element.
    """
    if "</" in script:
        raise ValueError("a script that contains '</' could end its element early")
    policy = ["default-src 'none'", f"style-src {_hash(CSS)}", "img-src data:", "base-uri 'none'", "form-action 'none'"]
    if script:
        policy.insert(2, f"script-src {_hash(script)}")
    head = join(
        h("meta", charset="utf-8"),
        h("meta", name="viewport", content="width=device-width, initial-scale=1"),
        h("meta", http_equiv="Content-Security-Policy", content="; ".join(policy)),
        h("meta", name="color-scheme", content="light dark"),
        h("meta", name="generator", content=f"local-data-platform {__version__}"),
        h("meta", name="description", content=description) if description else None,
        h("title", title),
        Raw(f"<style>{CSS}</style>"))
    stamp = f"local-data-platform {__version__}"
    shell = join(
        h("header", h("div", h("h1", title), h("p", subtitle, class_="sub") if subtitle else None, class_="wrap"),
          class_="top"),
        h("main", join(*body), class_="wrap"),
        h("footer", f"{footer} {stamp}".strip(), class_="wrap muted"),
        Raw(f"<script>{script}</script>") if script else None)
    document = h("html", Raw("\n"), join(h("head", Raw("\n" + head + "\n")), h("body", Raw("\n" + shell + "\n"))),
                 Raw("\n"), lang="en")
    return f"<!doctype html>\n{document}\n"


__all__ = ["CHIP_KINDS", "CSS", "FILTER_SCRIPT", "Raw", "VOID_TAGS", "chip", "details", "escape", "facts", "h", "join",
           "page", "search_box", "table"]
