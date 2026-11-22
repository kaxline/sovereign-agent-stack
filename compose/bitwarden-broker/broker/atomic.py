"""Replace a file atomically, for the broker's state files and setup's secrets."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, data: str | bytes, mode: int = 0o600, fsync: bool = True,
                 *, prefix: str | None = None, suffix: str = "",
                 owner: tuple[int, int] | None = None) -> bool:
    """Write ``data`` to ``path`` through a temporary file in the same folder.

    The temporary file is created mode 600 by ``mkstemp``, written, given
    ``owner`` (uid, gid) when set, fsynced unless ``fsync`` is false, set to
    ``mode`` and renamed over ``path``. On any error it is removed and the error
    is raised; ``path`` keeps its old content. ``str`` data is written as UTF-8.
    ``prefix`` and ``suffix`` name the temporary file (default prefix
    ``.<name>.``). The parent folder must exist.

    Returns False when ``owner`` could not be set for lack of permission (the
    file is still written, owned by this process), True otherwise.
    """
    fd, tmp = tempfile.mkstemp(prefix=prefix or f".{path.name}.", suffix=suffix, dir=path.parent)
    owner_kept = True
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
            if owner is not None:
                try:
                    os.fchown(f.fileno(), *owner)
                except PermissionError:
                    owner_kept = False
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
    return owner_kept
