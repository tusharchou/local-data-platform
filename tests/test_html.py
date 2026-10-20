"""The page shell of the project's HTML output: one file, light and dark, no network, every value escaped.

``ldp gold docs`` and the pages of later milestones are built on :mod:`local_data_platform.html`, so what it promises
is tested here on its own. The pages of the gold catalog are tested in ``test_gold_catalog.py``.
"""

import ast
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

from local_data_platform import html as ldp_html
from local_data_platform.html import Raw, chip, details, escape, facts, h, join, page, search_box, table

HOSTILE = ("<script>alert(1)</script> \"quoted\" & 'single' <img src=http://evil.example/x onerror=alert(1)> "
           "</td></table><a href=\"javascript:alert(1)\">x</a>")
EXPECTED_TAGS = {"html", "head", "meta", "title", "style", "body", "header", "div", "h1", "p", "main", "footer",
                 "script", "section", "h2", "span", "dl", "dt", "dd", "table", "caption", "thead", "tbody", "tr", "th",
                 "td", "label", "input", "details", "summary", "pre", "code", "ul", "li", "b"}


class Parsed(HTMLParser):
    """Every start tag of a page with its attributes, and the text of its elements."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []
        self._raw: list[str] = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self._raw.append(tag)

    def handle_endtag(self, tag):
        if self._raw and self._raw[-1] == tag:
            self._raw.pop()

    def handle_data(self, data):
        if not self._raw or self._raw[-1] not in ("style", "script"):
            self.text.append(data)

    def attributes(self, name: str) -> list[str | None]:
        return [attrs[name] for _, attrs in self.tags if name in attrs]

    def named(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for name, attrs in self.tags if name == tag]


def sha(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


# --------------------------------------------------------------------------- the markup builder


def test_text_and_attribute_values_are_escaped():
    markup = h("p", HOSTILE, title=HOSTILE)

    parsed = Parsed(str(markup))

    assert [tag for tag, _ in parsed.tags] == ["p"]
    assert parsed.tags[0][1] == {"title": HOSTILE}
    assert "".join(parsed.text) == HOSTILE
    assert "<script" not in markup and "<img" not in markup and "<a " not in markup


def test_escape_handles_none_numbers_and_quotes():
    assert escape(None) == ""
    assert escape(7) == "7"
    assert escape("a & b < c > d \" e ' f") == "a &amp; b &lt; c &gt; d &quot; e &#x27; f"


def test_a_raw_child_is_kept_and_a_nested_element_is_not_escaped_twice():
    inner = h("b", "5 < 6")

    outer = h("p", "see ", inner, " & more")

    assert outer == '<p>see <b>5 &lt; 6</b> &amp; more</p>'
    assert isinstance(outer, Raw) and isinstance(inner, Raw)


def test_a_raw_value_in_an_attribute_is_still_escaped():
    assert h("p", title=h("b", "x")) == '<p title="&lt;b&gt;x&lt;/b&gt;"></p>'


def test_none_false_and_empty_children_are_skipped_and_numbers_are_text():
    assert h("p", None, False, "", 0, 1.5) == "<p>01.5</p>"


def test_lists_tuples_and_generators_are_flattened_in_order():
    items = (h("li", number) for number in range(2))

    assert h("ul", items, [h("li", "a"), (h("li", "b"),)]) == "<ul><li>0</li><li>1</li><li>a</li><li>b</li></ul>"


@pytest.mark.parametrize("name, value, written", [
    ("class_", "x", ' class="x"'),
    ("for_", "q", ' for="q"'),
    ("data_search", "a b", ' data-search="a b"'),
    ("aria_live", "polite", ' aria-live="polite"'),
    ("http_equiv", "refresh", ' http-equiv="refresh"'),
    ("hidden", True, " hidden"),
    ("hidden", False, ""),
    ("hidden", None, ""),
    ("title", "", ' title=""'),
])
def test_attribute_names_and_values_are_written_as_html_does(name, value, written):
    assert h("p", **{name: value}) == f"<p{written}></p>"


@pytest.mark.parametrize("name", ['x" onclick="alert(1)', "onclick=alert(1)", "a b", "", "A", "1a", "a<b"])
def test_an_attribute_name_that_is_not_a_plain_name_is_refused(name):
    with pytest.raises(ValueError, match="not an attribute name"):
        h("p", **{name: "1"})


@pytest.mark.parametrize("tag", ["p onclick=alert(1)", "script>", "P", "", "a/b", "1p"])
def test_a_tag_name_that_is_not_a_plain_name_is_refused(tag):
    with pytest.raises(ValueError, match="not a tag name"):
        h(tag, "x")


def test_a_void_element_has_no_content_and_no_closing_tag():
    assert h("input", type="search", id="q") == '<input type="search" id="q">'
    assert h("br") == "<br>"
    with pytest.raises(ValueError, match="<input> has no content"):
        h("input", "text")


def test_join_puts_each_item_on_its_own_line_and_skips_empty_ones():
    assert join(h("p", "a"), None, "x < y", "", h("p", "b")) == "<p>a</p>\nx &lt; y\n<p>b</p>"
    assert join() == ""


@pytest.mark.parametrize("make", [
    lambda value: h("p", value),
    lambda value: h("p", title=value),
    lambda value: chip(value),
    lambda value: chip(value, "bad"),
    lambda value: facts([(value, value)]),
    lambda value: details(value, value, open=True),
    lambda value: table([value], [[value]], caption=value),
    lambda value: search_box(value, value),
    lambda value: join(value, h("p", value)),
])
def test_no_helper_lets_a_typed_value_become_markup(make):
    parsed = Parsed(str(make(HOSTILE)))

    assert {tag for tag, _ in parsed.tags} <= EXPECTED_TAGS
    assert not [name for _, attrs in parsed.tags for name in attrs if name.startswith("on")]
    assert not [value for _, attrs in parsed.tags for name, value in attrs.items()
                if name in ("href", "src") and value]


# --------------------------------------------------------------------------- the helpers


def test_a_chip_is_neutral_or_ok_warn_or_bad_and_nothing_else():
    assert chip("x") == '<span class="chip">x</span>'
    assert [chip("x", kind) for kind in ("ok", "warn", "bad")] == [f'<span class="chip {kind}">x</span>' for kind in
                                                                   ("ok", "warn", "bad")]
    with pytest.raises(ValueError, match="not 'loud'"):
        chip("x", "loud")


def test_facts_leave_out_a_pair_with_no_value():
    out = facts([("Owner", "ops"), ("Grain", None), ("Refresh", ""), ("Tags", "a, b")])

    assert out == '<dl class="facts"><dt>Owner</dt><dd>ops</dd><dt>Tags</dt><dd>a, b</dd></dl>'


def test_details_are_closed_unless_they_are_asked_to_be_open():
    assert details("More", "x") == "<details><summary>More</summary>x</details>"
    assert details("More", "x", open=True) == "<details open><summary>More</summary>x</details>"


def test_a_table_right_aligns_numbers_and_not_booleans_or_text():
    out = table(["name", "rows", "ok"], [["a", 5, True], ["b", 2.5, False]], caption="Things")

    parsed = Parsed(str(out))
    cells = parsed.named("td")

    assert [cell.get("class") for cell in cells] == [None, "num", None, None, "num", None]
    assert parsed.named("th") == [{"scope": "col"}] * 3
    assert str(out).startswith('<div class="scroll"><table><caption>Things</caption><thead>')
    assert "<caption" not in str(table(["a"], [["b"]]))


def test_a_table_can_be_told_which_columns_to_right_align_and_the_others_are_left_alone():
    rows = [["x", 1, "y"]]

    chosen = Parsed(str(table(["a", "b", "c"], rows, right=[2])))
    none = Parsed(str(table(["a", "b", "c"], rows, right=[])))
    automatic = Parsed(str(table(["a", "b", "c"], rows)))

    assert [cell.get("class") for cell in chosen.named("td")] == [None, None, "num"]
    assert [cell.get("class") for cell in chosen.named("th")] == [None, None, "num"]  # the heading follows its column
    assert [cell.get("class") for cell in none.named("td")] == [None, None, None]
    assert [cell.get("class") for cell in automatic.named("td")] == [None, "num", None]
    assert [cell.get("class") for cell in automatic.named("th")] == [None, None, None]
    assert "th.num{text-align:right}" in ldp_html.CSS


def test_the_search_box_has_the_ids_that_the_script_looks_for():
    box = Parsed(str(search_box("Find a table")))

    assert {"q", "count"} <= set(box.attributes("id"))
    assert 'getElementById("q")' in ldp_html.FILTER_SCRIPT and 'getElementById("count")' in ldp_html.FILTER_SCRIPT
    assert box.named("label") == [{"for": "q", "class": "sr"}]


# --------------------------------------------------------------------------- the page


def render(**keywords) -> str:
    return page("Gold catalog", h("p", "Hello"), **keywords)


def test_a_page_is_one_html_document_with_a_header_a_main_and_a_footer():
    text = render(subtitle="3 tables", description="What is here", footer="Made today.")

    parsed = Parsed(text)

    assert text.startswith("<!doctype html>\n<html lang=\"en\">\n<head>\n") and text.endswith("</html>\n")
    assert [tag for tag, _ in parsed.tags if tag in ("head", "body", "header", "main", "footer", "h1")] == [
        "head", "body", "header", "h1", "main", "footer"]
    assert {"charset": "utf-8"} in parsed.named("meta")
    assert {"name": "viewport", "content": "width=device-width, initial-scale=1"} in parsed.named("meta")
    assert {"name": "description", "content": "What is here"} in parsed.named("meta")
    assert {"name": "color-scheme", "content": "light dark"} in parsed.named("meta")
    assert "".join(parsed.text).count("Gold catalog") == 2  # the title and the heading
    assert f"Made today. local-data-platform {ldp_html.__version__}" in "".join(parsed.text)
    assert "3 tables" in "".join(parsed.text) and "Hello" in "".join(parsed.text)


def test_a_page_without_the_optional_parts_leaves_them_out():
    parsed = Parsed(render())

    assert [attrs.get("name") for attrs in parsed.named("meta") if attrs.get("name") == "description"] == []
    assert parsed.named("script") == []
    assert [attrs for attrs in parsed.named("p") if attrs.get("class") == "sub"] == []


def test_the_policy_allows_the_one_stylesheet_and_nothing_else_when_there_is_no_script():
    policy = dict(part.strip().split(" ", 1) for part in Parsed(render()).named("meta")[2]["content"].split(";"))

    assert policy == {"default-src": "'none'", "style-src": sha(ldp_html.CSS), "img-src": "data:",
                      "base-uri": "'none'", "form-action": "'none'"}


def test_the_policy_allows_the_one_script_by_its_hash():
    script = "document.title = 'x';"
    parsed = Parsed(render(script=script))

    policy = dict(part.strip().split(" ", 1) for part in parsed.named("meta")[2]["content"].split(";"))

    assert policy["script-src"] == sha(script) and policy["default-src"] == "'none'"
    assert f"<script>{script}</script>" in render(script=script)


def test_the_hash_in_the_policy_is_the_hash_of_the_text_between_the_tags():
    text = render(script=ldp_html.FILTER_SCRIPT)

    style = re.search(r"<style>(.*?)</style>", text, re.S).group(1)
    script = re.search(r"<script>(.*?)</script>", text, re.S).group(1)

    assert escape(sha(style)) in text and escape(sha(script)) in text  # the quotes are entities in an attribute
    assert style == ldp_html.CSS and script == ldp_html.FILTER_SCRIPT


def test_a_script_that_could_end_its_element_is_refused():
    with pytest.raises(ValueError, match="could end its element early"):
        render(script="var s = '</script><b>';")


def test_a_page_asks_for_nothing_from_the_network():
    section = h("section", h("pre", h("code", "ldp query x")), id="t1")
    text = page("T", search_box(), h("a", "in page", href="#t1"), section, script=ldp_html.FILTER_SCRIPT)
    parsed = Parsed(text)

    assert not parsed.named("link") and not parsed.named("img") and not parsed.named("iframe")
    assert all(value is None or value.startswith("#") for value in parsed.attributes("href") + parsed.attributes("src"))
    assert "url(" not in ldp_html.CSS and "@import" not in ldp_html.CSS
    for word in ("http://", "https://", "fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "import("):
        assert word not in text, word


def test_the_stylesheet_has_light_tokens_and_dark_tokens_for_the_same_names():
    light = re.search(r":root\{(.*?)\}\n@media", ldp_html.CSS, re.S).group(1)
    dark = re.search(r"prefers-color-scheme:dark\)\{:root\{(.*?)\}\}", ldp_html.CSS, re.S).group(1)

    def names(block: str) -> set[str]:
        return set(re.findall(r"--([a-z-]+):", block))

    colours = {"bg", "surface", "fg", "muted", "border", "accent", "ok", "ok-bg", "warn", "warn-bg", "bad", "bad-bg"}
    assert colours <= names(light)
    assert names(dark) <= names(light) and colours <= names(dark)
    assert "color-scheme:light dark" in ldp_html.CSS


def test_a_value_in_the_title_the_heading_the_subtitle_the_description_and_the_footer_is_escaped():
    text = page(HOSTILE, h("p", "x"), subtitle=HOSTILE, description=HOSTILE, footer=HOSTILE)
    parsed = Parsed(text)

    assert {tag for tag, _ in parsed.tags} <= EXPECTED_TAGS
    assert [attrs["content"] for attrs in parsed.named("meta") if attrs.get("name") == "description"] == [HOSTILE]
    assert "".join(parsed.text).count(HOSTILE) >= 4
    assert "<script>alert" not in text


def test_a_page_is_the_same_text_every_time_it_is_made():
    assert render(subtitle="x", script=ldp_html.FILTER_SCRIPT) == render(subtitle="x", script=ldp_html.FILTER_SCRIPT)


def test_the_filter_script_is_short_and_reads_only_the_page():
    lines = [line for line in ldp_html.FILTER_SCRIPT.splitlines() if line.strip()]

    assert len(lines) <= 20
    assert 'querySelectorAll("[data-search]")' in ldp_html.FILTER_SCRIPT
    assert "http" not in ldp_html.FILTER_SCRIPT and "eval" not in ldp_html.FILTER_SCRIPT


def test_the_module_imports_only_the_standard_library_and_the_package_itself():
    tree = ast.parse(Path(ldp_html.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").split(".")[0])

    assert imported <= set(sys.stdlib_module_names) | {"local_data_platform"}, imported


# --------------------------------------------------------------------------- the script, run for real

FILTER_HARNESS = r"""
const [script, casesJson, textsJson, dropJson] = process.argv.slice(1);
const drop = JSON.parse(dropJson);
const results = [];
for (const typed of JSON.parse(casesJson)) {
  const items = JSON.parse(textsJson).map(text => ({
    hidden: false,
    getAttribute(name) { return name === "data-search" ? text : null; },
  }));
  const listeners = {};
  const box = {value: "", addEventListener(type, handler) { listeners[type] = handler; }};
  const count = {textContent: ""};
  global.document = {
    getElementById(id) { return drop.includes(id) ? null : ({q: box, count: count}[id] || null); },
    querySelectorAll(selector) { return selector === "[data-search]" ? items : []; },
  };
  new Function(script)();
  const before = {hidden: items.map(item => item.hidden), count: count.textContent};
  box.value = typed;
  if (listeners.input) listeners.input();
  results.push({typed, before, hidden: items.map(item => item.hidden), count: count.textContent});
}
console.log(JSON.stringify(results));
"""

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the script of a page")


def run_filter(cases: list[str], texts: list[str], drop: tuple[str, ...] = ()) -> list[dict]:
    """Run the filter script of the page in node, against items made of ``texts``, once for each typed text."""
    done = subprocess.run(["node", "-e", FILTER_HARNESS, ldp_html.FILTER_SCRIPT, json.dumps(cases), json.dumps(texts),
                           json.dumps(list(drop))], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


TEXTS = ["gold.sales\nowner ops\nthe daily numbers", "gold.stock\nowner finance", "lake.orders"]


@needs_node
def test_the_search_shows_every_item_before_anything_is_typed_and_says_how_many():
    (result,) = run_filter([""], TEXTS)

    assert result["before"] == {"hidden": [False, False, False], "count": "3 of 3 shown"}
    assert result["hidden"] == [False, False, False] and result["count"] == "3 of 3 shown"


@needs_node
@pytest.mark.parametrize("typed, hidden, count", [
    ("finance", [True, False, True], "1 of 3 shown"),
    ("FINANCE", [True, False, True], "1 of 3 shown"),  # the words are lower-cased, as the text of an item is
    ("owner ops", [False, True, True], "1 of 3 shown"),
    ("  owner   ops  ", [False, True, True], "1 of 3 shown"),
    ("owner", [False, False, True], "2 of 3 shown"),
    ("daily numbers", [False, True, True], "1 of 3 shown"),  # every word has to be there, in any order
    ("numbers daily", [False, True, True], "1 of 3 shown"),
    ("ops finance", [True, True, True], "0 of 3 shown"),
    ("nothing like it", [True, True, True], "0 of 3 shown"),
    ("sal", [False, True, True], "1 of 3 shown"),  # a part of a word is enough
    ("", [False, False, False], "3 of 3 shown"),
    ("   ", [False, False, False], "3 of 3 shown"),
])
def test_the_search_hides_the_items_that_lack_a_word_that_was_typed(typed, hidden, count):
    (result,) = run_filter([typed], TEXTS)

    assert (result["hidden"], result["count"]) == (hidden, count)


@needs_node
def test_the_search_shows_an_item_again_when_the_words_are_taken_away():
    first, second = run_filter(["finance", ""], TEXTS)

    assert first["hidden"] == [True, False, True] and second["hidden"] == [False, False, False]


@needs_node
def test_a_page_with_no_search_box_or_no_counter_does_not_break_the_script():
    (without_box,) = run_filter(["finance"], TEXTS, drop=("q",))
    (without_count,) = run_filter(["finance"], TEXTS, drop=("count",))

    assert without_box["hidden"] == [False, False, False] and without_box["count"] == ""
    assert without_count["hidden"] == [True, False, True] and without_count["count"] == ""
