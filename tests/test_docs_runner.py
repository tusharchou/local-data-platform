"""``tests/docs_runner.py``, which the tests of the documentation pages use, on small pages of its own."""

import sys
import textwrap
from pathlib import Path

import pytest

from tests.docs_runner import DocsBlockFailed, output_matches, parse_blocks, run_page


def page_of(tmp_path: Path, text: str, name: str = "page.md") -> Path:
    page = tmp_path / name
    page.write_text(textwrap.dedent(text).lstrip("\n"))
    return page


def work_in(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    return work


# --------------------------------------------------------------------------- reading a page


def test_the_fenced_blocks_are_read_with_their_language_and_the_line_of_their_fence(tmp_path):
    page = page_of(tmp_path, """
        # Title

        ```bash
        echo one
        echo two
        ```

        Some text.

        ```python
        print("three")
        ```

        ```text
        not code
        ```
    """)

    blocks = parse_blocks(page)

    assert [(block.language, block.line, block.code) for block in blocks] == [
        ("bash", 3, "echo one\necho two\n"), ("python", 10, 'print("three")\n'), ("text", 14, "not code\n")]


def test_an_indented_block_loses_the_indent_of_its_fence_and_a_longer_fence_holds_a_shorter_one(tmp_path):
    page = page_of(tmp_path, """
        - a step

            ```bash
            echo indented
            ```

        ````markdown
        ```bash
        echo inside
        ```
        ````

        ~~~python
        print("tilde")
        ~~~
    """)

    blocks = parse_blocks(page)

    assert [(block.language, block.code) for block in blocks] == [
        ("bash", "echo indented\n"), ("markdown", "```bash\necho inside\n```\n"), ("python", 'print("tilde")\n')]


def test_a_block_that_is_never_closed_is_an_error_that_names_its_line(tmp_path):
    page = page_of(tmp_path, """
        Text

        ```bash
        echo never closed
    """)

    with pytest.raises(ValueError, match=r"page.md:3: the block is never closed"):
        parse_blocks(page)


def test_a_skip_marker_gives_the_block_a_reason_and_a_missing_reason_is_an_error(tmp_path):
    page = page_of(tmp_path, """
        <!-- docs-runner: skip it installs from PyPI -->
        ```bash
        pip install something
        ```
    """)
    bare = page_of(tmp_path, """
        <!-- docs-runner: skip -->
        ```bash
        pip install something
        ```
    """, "bare.md")

    assert [(block.skip, block.code) for block in parse_blocks(page)] == [("it installs from PyPI",
                                                                           "pip install something\n")]
    with pytest.raises(ValueError, match=r"bare.md:1: 'docs-runner: skip' needs a reason"):
        parse_blocks(bare)


def test_an_output_marker_makes_the_block_after_it_the_output_of_the_block_before_it(tmp_path):
    page = page_of(tmp_path, """
        ```bash
        echo hello
        ```

        <!-- docs-runner: output -->
        ```text
        hello
        ```

        ```text
        a block that is just shown
        ```
    """)

    blocks = parse_blocks(page)

    assert [(block.language, block.output) for block in blocks] == [("bash", "hello\n"), ("text", None)]


@pytest.mark.parametrize("text, message", [
    ("<!-- docs-runner: output -->\n```text\nx\n```\n", r"page.md:1: 'docs-runner: output' has no block before it"),
    ("```bash\nx\n```\n<!-- docs-runner: pause -->\n```bash\ny\n```\n",
     r"page.md:4: unknown marker 'docs-runner: pause'"),
    ("<!-- docs-runner: skip a -->\n<!-- docs-runner: skip b -->\n```bash\nx\n```\n", r"page.md:2: a second marker"),
    ("```bash\nx\n```\n<!-- docs-runner: output -->\n", r"page.md:4: the marker 'docs-runner: output' has no block"),
])
def test_a_marker_that_does_not_make_sense_is_an_error_that_names_its_line(tmp_path, text, message):
    page = tmp_path / "page.md"
    page.write_text(text)

    with pytest.raises(ValueError, match=message):
        parse_blocks(page)


def test_text_between_a_marker_and_a_block_cancels_the_marker(tmp_path):
    page = page_of(tmp_path, """
        <!-- docs-runner: skip it is not for the next block -->

        Some words.

        ```bash
        echo runs
        ```
    """)

    assert [block.skip for block in parse_blocks(page)] == [None]


# --------------------------------------------------------------------------- running bash


def test_bash_blocks_run_in_order_and_a_cd_carries_over_and_nothing_else_does(tmp_path):
    page = page_of(tmp_path, """
        ```bash
        mkdir sub
        cd sub
        echo hello > a.txt
        export KEPT=no
        ```

        ```bash
        cat a.txt
        pwd
        test -z "${KEPT:-}"
        ```
    """)

    run = run_page(page, work_in(tmp_path))

    assert run.outcomes[1].stdout.splitlines()[0] == "hello"
    assert run.outcomes[1].stdout.splitlines()[1].endswith("work/sub")
    assert [outcome.status for outcome in run.outcomes] == [0, 0]


def test_the_first_command_that_fails_fails_the_block_and_the_page_stops_there(tmp_path):
    page = page_of(tmp_path, """
        Words.

        ```bash
        echo before > before.txt
        ls no-such-file
        echo after > after.txt
        ```

        ```bash
        echo next > next.txt
        ```
    """)
    work = work_in(tmp_path)

    with pytest.raises(DocsBlockFailed) as failure:
        run_page(page, work)

    message = str(failure.value)
    assert message.startswith("page.md:3: the bash block exited with status")
    assert "--- code\necho before > before.txt\nls no-such-file" in message
    assert "no-such-file" in message.split("--- stderr")[1]
    assert (work / "before.txt").exists() and not (work / "after.txt").exists() and not (work / "next.txt").exists()


def test_a_command_in_a_pipe_that_fails_fails_the_block(tmp_path):
    page = page_of(tmp_path, """
        ```bash
        false | cat
        ```
    """)

    with pytest.raises(DocsBlockFailed, match="exited with status 1"):
        run_page(page, work_in(tmp_path))


def test_ldp_and_python_are_the_interpreter_that_runs_the_tests(tmp_path):
    page = page_of(tmp_path, """
        ```bash
        python -c "import sys; print(sys.executable)"
        python3 -c "import sys; print(sys.version_info[:2])"
        ldp --version
        ```
    """)

    run = run_page(page, work_in(tmp_path))

    lines = run.stdout.splitlines()
    assert Path(lines[0]).resolve() == Path(sys.executable).resolve()
    assert lines[1] == str(sys.version_info[:2])
    assert lines[2].strip()  # the version that ldp prints


def test_the_environment_can_be_given(tmp_path):
    page = page_of(tmp_path, """
        ```bash
        echo "${MY_SETTING}"
        ```
    """)

    run = run_page(page, work_in(tmp_path), env={"MY_SETTING": "given", "PATH": "/usr/bin:/bin"})

    assert run.stdout == "given\n"


def test_a_block_that_takes_too_long_fails_and_shows_what_it_had_printed(tmp_path):
    page = page_of(tmp_path, """
        ```bash
        echo started
        sleep 30
        ```
    """)

    with pytest.raises(DocsBlockFailed) as failure:
        run_page(page, work_in(tmp_path), timeout=1)

    assert "the bash block took more than 1 seconds" in str(failure.value)
    assert "started" in str(failure.value)


# --------------------------------------------------------------------------- running python


def test_python_blocks_that_follow_each_other_share_a_process_and_not_a_namespace(tmp_path):
    page = page_of(tmp_path, """
        ```python
        import os
        value = 1
        print("first", os.getpid())
        ```

        ```python
        import os
        print("second", os.getpid(), "value" in globals())
        ```

        ```bash
        echo between
        ```

        ```python
        import os
        print("third", os.getpid())
        ```
    """)

    run = run_page(page, work_in(tmp_path))

    first, second, third = (line.split() for line in run.stdout.splitlines() if line.split()[0] in
                            ("first", "second", "third"))
    assert first[1] == second[1] != third[1]  # the two blocks in a row are one process, the third another
    assert second[2] == "False"  # the second block does not see the first one's names
    assert [len(outcome.blocks) for outcome in run.outcomes] == [2, 1, 1]


def test_a_traceback_shows_the_line_of_the_page(tmp_path):
    page = page_of(tmp_path, """
        Intro.

        ```python
        print("fine")
        raise ValueError("this is line 5 of the page")
        ```
    """)

    with pytest.raises(DocsBlockFailed) as failure:
        run_page(page, work_in(tmp_path))

    message = str(failure.value)
    assert message.startswith("page.md:3: the python block exited with status 1")
    assert 'File "page.md", line 5' in message and "this is line 5 of the page" in message


def test_the_python_blocks_run_in_the_work_folder(tmp_path):
    page = page_of(tmp_path, """
        ```python
        from pathlib import Path
        Path("made.txt").write_text("by python")
        ```

        ```bash
        cat made.txt
        ```
    """)

    run = run_page(page, work_in(tmp_path))

    assert run.stdout == "by python"


# --------------------------------------------------------------------------- what is run and what is not


def test_a_skipped_block_does_not_run_and_is_reported_with_its_reason(tmp_path):
    page = page_of(tmp_path, """
        <!-- docs-runner: skip it would start a server -->
        ```bash
        touch skipped.txt
        ```

        ```bash
        touch ran.txt
        ```
    """)
    work = work_in(tmp_path)

    run = run_page(page, work)

    assert not (work / "skipped.txt").exists() and (work / "ran.txt").exists()
    assert [(block.line, block.skip) for block in run.skipped] == [(2, "it would start a server")]
    assert len(run.blocks) == 1


def test_only_the_languages_that_are_asked_for_run(tmp_path):
    page = page_of(tmp_path, """
        ```bash
        touch from_bash.txt
        ```

        ```python
        print("from python")
        ```

        ```json
        {"not": "run"}
        ```
    """)
    work = work_in(tmp_path)

    run = run_page(page, work, languages=("python",))

    assert run.stdout == "from python\n" and not (work / "from_bash.txt").exists()


# --------------------------------------------------------------------------- the output a page shows


def test_the_output_that_a_page_shows_is_checked_against_what_the_block_printed(tmp_path):
    page = page_of(tmp_path, """
        ```bash
        echo "first line"
        echo "took 1.23s and 4 rows"
        echo "last line"
        ```

        <!-- docs-runner: output -->
        ```text
        first line
        took ...s and 4 rows
        ```
    """)

    run_page(page, work_in(tmp_path))


def test_an_output_that_differs_fails_the_block_and_shows_both(tmp_path):
    page = page_of(tmp_path, """
        ```bash
        echo "5 rows"
        ```

        <!-- docs-runner: output -->
        ```text
        6 rows
        ```
    """)

    with pytest.raises(DocsBlockFailed) as failure:
        run_page(page, work_in(tmp_path))

    message = str(failure.value)
    assert message.startswith("page.md:1: the bash block did not print what the page shows")
    assert "--- the page shows\n6 rows" in message and "--- stdout\n5 rows" in message


def test_only_stdout_counts_as_the_output(tmp_path):
    page = page_of(tmp_path, """
        ```bash
        echo "on stderr" >&2
        echo "on stdout"
        ```

        <!-- docs-runner: output -->
        ```text
        on stderr
        ```
    """)

    with pytest.raises(DocsBlockFailed, match="did not print what the page shows"):
        run_page(page, work_in(tmp_path))


@pytest.mark.parametrize("expected, actual, matches", [
    ("a\nb", "a\nb", True),
    ("b", "a\nb\nc", True),                                   # the output may have more lines around it
    ("a\nc", "a\nb\nc", False),                               # but lines that are shown one after the other are
    ("a\n...\nc", "a\nb\nc", True),                           # unless a line of dots says there are lines between
    ("a\n...\nc", "a\nc", True),                              # which may be none
    ("a\n...", "a\nb\nc", True),
    ("...\nc", "a\nb\nc", True),
    ("took ...s", "took 1.5s", True),
    ("took ...s", "took 1.5 sec", False),
    ("... PASS=33 ...", "09:02:36  Done. PASS=33 WARN=0", True),
    ("  padded  ", "padded", True),                           # spaces around a line do not count
    ("a\n\nb", "a\nb", True),                                 # nor do blank lines of the shown output
    ("a.b", "axb", False),                                    # a dot is a dot
    ("a (b) [c]", "a (b) [c]", True),                         # and so are the other regular expression characters
    ("b\na", "a\nb", False),                                  # the order counts
    ("a", "", False),
])
def test_output_matching(expected, actual, matches):
    assert output_matches(expected, actual) is matches
