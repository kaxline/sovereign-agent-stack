"""Replace a file atomically, for the broker's state files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, data: str | bytes, mode: int = 0o600, fsync: bool = True,
                 *, prefix: str | None = None) -> None:
    """Write ``data`` to ``path`` through a temporary file in the same folder.

    The temporary file is created mode 600 by ``mkstemp``, written, fsynced
    unless ``fsync`` is false, set to ``mode`` and renamed over ``path``. On any
    error it is removed and the error is raised; ``path`` keeps its old content.
    ``str`` data is written as UTF-8. ``prefix`` names the temporary file
    (default ``.<name>.``). The parent folder must exist.
    """
    fd, tmp = tempfile.mkstemp(prefix=prefix or f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
