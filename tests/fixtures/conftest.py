"""Sample projects under ``tests/fixtures`` are test *data*, not product tests.

They are complete miniature repositories (their own ``app.py``, ``tests/`` and
build files) used by the audit and acceptance tests. Collecting them as part of
this project's suite ran their tests with this repository as the working
directory, so a fixture that scans ``.`` saw this repository's files instead of
its own sample project — and failed once evidence snapshots were stored under
``reports/``.

Ignore them here and keep running them explicitly, from their own directory,
when that is what you mean::

    pytest tests/fixtures/audit/fixture1/fixture1_project
"""
from __future__ import annotations

collect_ignore_glob = ["*"]
