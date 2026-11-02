"""Commands agentbox tells the user to run, copy-pasteable from where it was launched."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def agentbox_command(args: str) -> str:
    """``agentbox <args>``, prefixed with ``uv run`` when launched through uv.

    ``agentbox`` is a project console script in ``.venv/bin``, which is on ``PATH`` only
    inside ``uv run`` or an activated venv. ``uv`` sets ``$UV`` for its subprocesses.
    """
    return f"{_uv_prefix()}agentbox {args}"


def msb_command(args: str) -> str:
    """``msb <args>``, prefixed with ``uv run`` only when ``msb`` is not installed system-wide.

    microsandbox's installer puts ``msb`` on ``PATH`` for good; the wheel additionally
    ships a copy inside the venv. Only that second case needs ``uv run``.
    """
    path = os.environ.get("PATH", "")
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        path = os.pathsep.join(
            p for p in path.split(os.pathsep) if Path(venv) not in Path(p).parents
        )
    prefix = "" if shutil.which("msb", path=path) else _uv_prefix()
    return f"{prefix}msb {args}"


def _uv_prefix() -> str:
    return "uv run " if os.environ.get("UV") else ""
