"""``ldp dashboard``: a local web page to browse the lakehouse, run a query, read a report and see the health.

It is a marimo notebook that is served on 127.0.0.1 only. The ``notebook`` extra brings marimo and Altair, and without
it ``ldp dashboard`` prints how to install them and exits with 1. See ``docs/dashboard.md``.

The modules, from the outside in:

* :mod:`.cli`: ``ldp dashboard`` (:func:`add_cli`). It checks the extra and the sources, writes what it was asked
  for into the environment of ``marimo run`` and starts it.
* :mod:`.app`: the notebook that marimo runs. It only lays out what :mod:`.service` gives, one tab for each page.
* :mod:`.service`: what each page shows, as plain Python that needs neither marimo nor Altair (:class:`Dashboard`).

Importing this package loads none of them, so ``ldp --help`` stays fast and works without the extra.
"""

from .cli import add_cli

__all__ = ["add_cli"]
