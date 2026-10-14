"""Recipes that call ``ldp schedule tick`` every 5 minutes: a crontab line for Linux and a launchd plist for macOS.

``ldp schedule install --print cron DIR`` and ``--print launchd`` print them, and nothing here writes to a system
location, so the person who runs the command decides where the text goes::

    (crontab -l 2>/dev/null; ldp schedule install --print cron ldp_demo/restaurant) | crontab -
    ldp schedule install --print launchd ldp_demo/restaurant > ~/Library/LaunchAgents/restaurant.plist

Both call the ``ldp`` executable by its absolute path and the folder by its absolute path, because cron and launchd
start a command with almost no environment. A tick only does what is due, so calling it more often than a schedule
fires costs a few milliseconds, and a tick that is missed, because the machine was off or asleep, is made up by the next
one. cron does not run while a Mac sleeps and Apple has deprecated it, whereas launchd runs an interval that went by
during sleep once when the Mac wakes, so the macOS recipe is launchd.

This module needs only the standard library.
"""

import hashlib
import os
import plistlib
import re
import shlex
import shutil
import sys
from pathlib import Path

from local_data_platform.exceptions import ConfigError

RECIPES = ("cron", "launchd")
"""The recipes that ``ldp schedule install --print`` knows."""

INTERVAL_MINUTES = 5
"""How often the recipes call ``ldp schedule tick``."""


def find_executable(explicit: "str | os.PathLike | None" = None) -> Path:
    """The absolute path of the ``ldp`` executable that the recipes call.

    Args:
        explicit: A path that the caller gives. Otherwise the ``ldp`` that is running, then the first one on the
            ``PATH``, then the one in the folder of this Python, which is where a virtual environment puts it.

    Raises:
        ConfigError: If ``explicit`` is not a file, or no ``ldp`` can be found.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"{explicit} is not a file, so it cannot be the ldp executable")
        return Path(os.path.abspath(path))
    running = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if running is not None and running.name in ("ldp", "ldp.exe") and running.is_file():
        return Path(os.path.abspath(running))
    if (found := shutil.which("ldp")) is not None:
        return Path(os.path.abspath(found))
    beside = Path(sys.executable).parent / ("ldp.exe" if os.name == "nt" else "ldp")
    if beside.is_file():
        return beside
    raise ConfigError("cannot find the ldp executable on the PATH or beside this Python. Pass its path with --ldp PATH")


def label_of(directory: "str | os.PathLike") -> str:
    """The launchd label of a folder: its name and a short hash of its path, so two folders never clash."""
    folder = Path(directory).resolve()
    slug = re.sub(r"[^A-Za-z0-9_]+|-+", "-", folder.name).strip("-").lower() or "project"  # no `--`, which XML forbids
    return f"local-data-platform.schedule.{slug}.{hashlib.sha256(str(folder).encode('utf-8')).hexdigest()[:8]}"


def cron_recipe(directory: "str | os.PathLike", executable: "str | os.PathLike") -> str:
    """A comment and the crontab line that ticks ``directory`` every 5 minutes, and logs to ``.ldp/schedule.log``.

    The line makes the ``.ldp`` folder first, because the shell opens the log before the tick can make it.
    """
    folder = Path(directory).resolve()
    quote = shlex.quote
    command = (f"mkdir -p {quote(str(folder / '.ldp'))} && {quote(str(executable))} schedule tick {quote(str(folder))} "
               f">> {quote(str(folder / '.ldp' / 'schedule.log'))} 2>&1")
    command = command.replace("%", "\\%")  # cron reads an unescaped % as a newline
    return (f"# ldp schedule tick for {folder}, every {INTERVAL_MINUTES} minutes\n"
            f"*/{INTERVAL_MINUTES} * * * * {command}\n")


def launchd_recipe(directory: "str | os.PathLike", executable: "str | os.PathLike",
                   home: "str | os.PathLike | None" = None) -> str:
    """A launchd plist that ticks ``directory`` every 5 minutes and when it loads, with a comment on how to install it.

    The log goes to ``~/Library/Logs``, which exists, because launchd does not make the folder of a log.
    """
    folder = Path(directory).resolve()
    label = label_of(folder)
    log = (Path(home) if home is not None else Path.home()) / "Library" / "Logs" / f"{label}.log"
    plist = plistlib.dumps({
        "Label": label,
        "ProgramArguments": [str(executable), "schedule", "tick", str(folder)],
        "StartInterval": INTERVAL_MINUTES * 60,
        "RunAtLoad": True,
        "WorkingDirectory": str(folder),
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")
    note = "\n".join([
        "<!--",
        f"  ldp schedule tick for {folder}, every {INTERVAL_MINUTES} minutes and when it loads.".replace("--", "- -"),
        "  launchd runs an interval that went by while the Mac slept once, when it wakes.",
        f"  Save this as ~/Library/LaunchAgents/{label}.plist and run",
        f"    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{label}.plist",
        "  To stop it, run",
        f"    launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/{label}.plist",
        "-->",
    ])
    head, _, rest = plist.partition("<plist")
    return f"{head}{note}\n<plist{rest}"


def recipe(kind: str, directory: "str | os.PathLike", executable: "str | os.PathLike | None" = None,
           home: "str | os.PathLike | None" = None) -> str:
    """The text of a recipe: ``cron`` or ``launchd``.

    Args:
        kind: One of :data:`RECIPES`.
        directory: The folder to tick. The recipe holds its absolute path.
        executable: The ``ldp`` executable, see :func:`find_executable`.
        home: The home folder for the launchd log, ``~`` by default.

    Raises:
        ConfigError: If ``kind`` is not a recipe, or no ``ldp`` executable can be found.
    """
    if kind not in RECIPES:
        raise ConfigError(f"unknown recipe {kind!r}; the recipes are {', '.join(RECIPES)}")
    exe = find_executable(executable)
    return cron_recipe(directory, exe) if kind == "cron" else launchd_recipe(directory, exe, home)


__all__ = ["INTERVAL_MINUTES", "RECIPES", "cron_recipe", "find_executable", "label_of", "launchd_recipe", "recipe"]
