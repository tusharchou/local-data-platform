"""Runs the code blocks of a documentation page the way a reader would, so that the page cannot drift from the code.

The blocks are the fenced blocks of the Markdown page that are tagged ``bash`` or ``python``. They run in the order
they are written, in one folder, and the run stops at the first one that fails. A page can rely on these rules.

* A ``bash`` block runs with ``bash -e -o pipefail``, so the first command that fails fails the block. ``ldp`` and
  ``python`` are the ones that run the tests, and the folder of the Python that runs them is on ``PATH``, as it is
  when a reader has activated a virtual environment. A ``cd`` carries over to the next block, and nothing else does,
  so a block that needs a file names it relative to the folder that the ``cd`` left it in.
* The ``python`` blocks that follow one another run in one process, each in a namespace of its own, so a block does
  not see the names of the block before it, as when a reader pastes it into a new session. A ``bash`` block between
  them starts a new process.
* ``<!-- docs-runner: skip REASON -->`` on the line before a block keeps it from running. A reason is required,
  because a skipped block is a block that nothing tests.
* ``<!-- docs-runner: output -->`` on the line before a fenced block of any language says what the block before it
  prints. Every line must appear in the block's stdout, one after the other (for ``python`` blocks that run together,
  in what they printed all together). ``...`` inside a line stands for any text, a line that is only ``...`` stands
  for any number of lines, and the spaces around a line are ignored.

``run_page`` is what a test calls. A block that fails, or prints something else than the page says, raises
``DocsBlockFailed``, whose message names the page and the line of the block and shows its code and what it printed.
"""

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

RUNNABLE = ("bash", "python")

_FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<ticks>`{3,}|~{3,})[ \t]*(?P<info>[^`\n]*)$")
_MARKER = re.compile(r"^[ \t]*<!--[ \t]*docs-runner:[ \t]*(?P<what>.*?)[ \t]*-->[ \t]*$")
_SHOWN = 3000  # the characters of stdout and stderr that a failure message shows


class DocsBlockFailed(AssertionError):
    """A block of a page failed, or printed something else than the page says it prints."""


@dataclass(frozen=True)
class Block:
    """One fenced block of a page."""

    page: Path
    language: str
    code: str
    line: int  # the line of the opening fence, counted from 1
    skip: str | None = None  # why the block does not run, when the page marks it
    output: str | None = None  # what the page says the block prints, when a marked block follows it

    @property
    def where(self) -> str:
        return f"{self.page.name}:{self.line}"


@dataclass
class Outcome:
    """What one process printed. A run of ``python`` blocks is one process, and a ``bash`` block is another."""

    blocks: list[Block]
    status: int
    stdout: str
    stderr: str


@dataclass
class Run:
    """Every process that ran, and every block that was marked to be skipped."""

    outcomes: list[Outcome] = field(default_factory=list)
    skipped: list[Block] = field(default_factory=list)

    @property
    def stdout(self) -> str:
        return "".join(outcome.stdout for outcome in self.outcomes)

    @property
    def blocks(self) -> list[Block]:
        return [block for outcome in self.outcomes for block in outcome.blocks]


def parse_blocks(page: Path) -> list[Block]:
    """Every fenced block of ``page`` with a language, marked blocks included, in the order they are written.

    A block that a ``docs-runner: output`` marker introduces is not a block of its own: its text becomes the
    ``output`` of the block before it.

    Raises:
        ValueError: If a block is never closed, a marker is not one of the two above, a ``skip`` has no reason,
            or an ``output`` marker has no block before it.
    """
    lines = page.read_text().splitlines()
    blocks: list[Block] = []
    marker: tuple[str, int] | None = None  # the marker that waits for its block, and its line
    index = 0
    while index < len(lines):
        text = lines[index]
        found = _MARKER.match(text)
        fence = _FENCE.match(text)
        if found:
            if marker is not None:
                raise ValueError(f"{page.name}:{index + 1}: a second marker comes before the block that "
                                 f"'docs-runner: {marker[0]}' on line {marker[1]} is for")
            marker = (found.group("what"), index + 1)
        elif fence:
            index, block, marker = _read_block(page, lines, index, fence, marker, blocks)
            if block is not None:
                blocks.append(block)
        elif text.strip():
            marker = None  # the marker is for the block that comes right after it, so text in between cancels it
        index += 1
    if marker is not None:
        raise ValueError(f"{page.name}:{marker[1]}: the marker 'docs-runner: {marker[0]}' has no block after it")
    return blocks


def _read_block(page, lines, start, fence, marker, blocks):
    """Read the block that opens at ``lines[start]``. Returns where it ends, the block, and the marker left over."""
    indent, ticks = fence.group("indent"), fence.group("ticks")
    language = (fence.group("info").split() or [""])[0].lower()
    body: list[str] = []
    end = start + 1
    while end < len(lines):
        closing = _FENCE.match(lines[end])
        if closing and closing.group("ticks")[0] == ticks[0] and len(closing.group("ticks")) >= len(ticks) \
                and not closing.group("info").strip():
            break
        body.append(lines[end][len(indent):] if lines[end].startswith(indent) else lines[end].lstrip())
        end += 1
    else:
        raise ValueError(f"{page.name}:{start + 1}: the block is never closed")
    code = "\n".join(body) + "\n"
    if marker is None:
        return end, Block(page, language, code, start + 1), None
    what, where = marker
    if what == "output":
        if not blocks:
            raise ValueError(f"{page.name}:{where}: 'docs-runner: output' has no block before it")
        previous = blocks[-1]
        blocks[-1] = Block(previous.page, previous.language, previous.code, previous.line, previous.skip, code)
        return end, None, None
    if what.startswith("skip"):
        reason = what[len("skip"):].strip(" :")
        if not reason:
            raise ValueError(f"{page.name}:{where}: 'docs-runner: skip' needs a reason, as in "
                             f"'docs-runner: skip it installs from PyPI'")
        return end, Block(page, language, code, start + 1, skip=reason), None
    raise ValueError(f"{page.name}:{where}: unknown marker 'docs-runner: {what}'; the markers are 'skip REASON' "
                     f"and 'output'")


def run_page(page: Path, workdir: Path, *, languages: tuple[str, ...] = RUNNABLE, env: dict[str, str] | None = None,
             timeout: float = 600) -> Run:
    """Run the blocks of ``page`` in ``languages``, in order, starting in ``workdir``.

    Args:
        page: The Markdown page.
        workdir: The folder the first block starts in.
        languages: The languages to run. Blocks in other languages are left alone, and so are the ``bash`` blocks
            of a page whose caller asks for ``python`` only.
        env: The environment of the processes. It defaults to this process's, and ``PATH`` is changed either way.
        timeout: The seconds that each process may take.

    Returns:
        The processes that ran and the blocks that were skipped.

    Raises:
        DocsBlockFailed: If a block exits with a failure, or its output is not what the page says.
    """
    blocks = [block for block in parse_blocks(page) if block.language in languages]
    run = Run(skipped=[block for block in blocks if block.skip])
    cwd = Path(workdir)
    with tempfile.TemporaryDirectory(prefix="docs-runner-") as scratch:
        environment = _environment(Path(scratch), env)
        for group in _groups([block for block in blocks if not block.skip]):
            try:
                if group[0].language == "bash":
                    outcome, cwd = _run_bash(group[0], cwd, environment, Path(scratch), timeout)
                else:
                    outcome = _run_python(group, cwd, environment, timeout)
            except subprocess.TimeoutExpired as expired:
                printed = Outcome(group, -1, _text(expired.stdout), _text(expired.stderr))
                raise DocsBlockFailed(_report(group, f"took more than {timeout:g} seconds", printed)) from None
            run.outcomes.append(outcome)
            if outcome.status != 0:
                raise DocsBlockFailed(_report(group, f"exited with status {outcome.status}", outcome))
            for block in group:
                if block.output is not None and not output_matches(block.output, outcome.stdout):
                    raise DocsBlockFailed(_report([block], "did not print what the page shows", outcome,
                                                  expected=block.output))
    return run


def _text(printed: str | bytes | None) -> str:
    return printed.decode(errors="replace") if isinstance(printed, bytes) else printed or ""


def _groups(blocks: list[Block]) -> list[list[Block]]:
    """The blocks that share a process: a ``bash`` block alone, and the ``python`` blocks that follow each other."""
    groups: list[list[Block]] = []
    for block in blocks:
        if block.language == "python" and groups and groups[-1][0].language == "python":
            groups[-1].append(block)
        else:
            groups.append([block])
    return groups


def _environment(scratch: Path, env: dict[str, str] | None) -> dict[str, str]:
    """``env`` with ``ldp`` and ``python`` first on ``PATH``, as the interpreter that runs the tests."""
    shims = scratch / "bin"
    shims.mkdir()
    python = sys.executable
    for name, command in (("ldp", f'exec "{python}" -m local_data_platform.cli "$@"'),
                          ("python", f'exec "{python}" "$@"'), ("python3", f'exec "{python}" "$@"')):
        shim = shims / name
        shim.write_text(f"#!/bin/sh\n{command}\n")
        shim.chmod(0o755)
    environment = dict(os.environ if env is None else env)
    environment["PATH"] = os.pathsep.join([str(shims), str(Path(python).parent), environment.get("PATH", "")])
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _run_bash(block: Block, cwd: Path, env: dict[str, str], scratch: Path, timeout: float) -> tuple[Outcome, Path]:
    """Run one ``bash`` block in ``cwd`` and return what it printed and the folder it left the shell in."""
    cwd_file = scratch / "cwd"
    cwd_file.unlink(missing_ok=True)
    script = f'{block.code}\npwd > "$DOCS_RUNNER_CWD"\n'  # reached only when every command before it succeeded
    done = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=cwd,
                          env={**env, "DOCS_RUNNER_CWD": str(cwd_file)}, capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL, check=False)
    left = Path(cwd_file.read_text().strip()) if cwd_file.exists() else cwd
    return Outcome([block], done.returncode, done.stdout, done.stderr), left


def _run_python(group: list[Block], cwd: Path, env: dict[str, str], timeout: float) -> Outcome:
    """Run ``python`` blocks in one process, each in a new namespace.

    The code is padded with blank lines, so the line numbers of a traceback are the lines of the page.
    """
    script = "\n".join(
        f"exec(compile({chr(10) * block.line + block.code!r}, {block.page.name!r}, 'exec'), "
        f"{{'__name__': '__main__'}})" for block in group)
    with tempfile.TemporaryDirectory(prefix="docs-runner-") as scratch:
        path = Path(scratch) / "blocks.py"
        path.write_text(script)
        done = subprocess.run([sys.executable, "-W", "error::ResourceWarning", str(path)], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, check=False)
    return Outcome(group, done.returncode, done.stdout, done.stderr)


def output_matches(expected: str, actual: str) -> bool:
    """Whether every line of ``expected`` appears in ``actual``, in order and one right after the other.

    ``...`` inside a line stands for any text. A line that is only ``...`` stands for any number of lines,
    including none. The spaces around a line, and the blank lines of ``expected``, do not count.
    """
    wanted = [line.strip() for line in expected.splitlines() if line.strip()]
    have = [line.strip() for line in actual.splitlines()]
    return _match(wanted, 0, have, 0, True)


def _match(wanted: list[str], at: int, have: list[str], start: int, gap: bool) -> bool:
    if at == len(wanted):
        return True
    if wanted[at] == "...":
        return _match(wanted, at + 1, have, start, True)
    pattern = re.compile("^" + ".*".join(re.escape(part) for part in wanted[at].split("...")) + "$")
    candidates = range(start, len(have)) if gap else range(start, min(start + 1, len(have)))
    return any(pattern.match(have[found]) and _match(wanted, at + 1, have, found + 1, False) for found in candidates)


def _report(group: list[Block], problem: str, outcome: Outcome, expected: str | None = None) -> str:
    where = ", ".join(block.where for block in group)
    code = "".join(block.code for block in group).rstrip()
    text = [f"{where}: the {group[0].language} block {problem}", "--- code", code]
    if expected is not None:
        text += ["--- the page shows", expected.rstrip()]
    text += ["--- stdout", outcome.stdout[-_SHOWN:].rstrip(), "--- stderr", outcome.stderr[-_SHOWN:].rstrip()]
    return "\n".join(text)
