"""Safe handling of user-supplied file names and server paths.

* Uploaded files keep only a plain base name with an allowed extension and are saved inside the
  project's own upload folder - a name like ``../../app/x.py`` or ``/etc/passwd`` cannot escape.
* Paths sent to the API for Discover must sit inside ``allowed_import_roots``.
  (The CLI is run by an operator on the server and is not restricted.)
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from accelerator.config.settings import get_settings
from accelerator.core.exceptions import UnsafePathError

ALLOWED_UPLOAD_EXTENSIONS = {
    ".dtsx",
    ".ispac",
    ".xml",
    ".dsx",
    ".isx",
    ".sql",
    ".ddl",
    ".bteq",
    ".btq",
    ".pls",
    ".pks",
    ".pkb",
    ".prc",
    ".fnc",
}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$")


def safe_upload_name(filename: str | None) -> str:
    name = PurePosixPath((filename or "").replace("\\", "/")).name.strip()
    if not _SAFE_NAME.match(name):
        raise UnsafePathError(f"Unsupported file name {filename!r}")
    if Path(name).suffix.lower() not in ALLOWED_UPLOAD_EXTENSIONS:
        raise UnsafePathError(
            f"File type not allowed: {name!r}. Allowed: {sorted(ALLOWED_UPLOAD_EXTENSIONS)}"
        )
    return name


def save_upload(project_id: str, filename: str | None, data: bytes) -> Path:
    if len(data) > MAX_UPLOAD_BYTES:
        raise UnsafePathError(f"File too large ({len(data)} bytes; limit {MAX_UPLOAD_BYTES})")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", project_id):
        raise UnsafePathError("Invalid project id")
    folder = (get_settings().upload_dir / project_id).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    dest = (folder / safe_upload_name(filename)).resolve()
    if dest.parent != folder:
        raise UnsafePathError("Upload path escapes the upload folder")
    dest.write_bytes(data)
    return dest


def check_import_path(path: str | Path) -> Path:
    """Allow a server path only if it lies inside one of the configured import roots."""
    resolved = Path(path).resolve()
    settings = get_settings()
    roots = [Path(r).resolve() for r in settings.allowed_import_roots] + [
        settings.upload_dir.resolve()
    ]
    if not any(resolved == r or r in resolved.parents for r in roots):
        raise UnsafePathError(f"Path {str(path)!r} is outside the allowed import folders")
    return resolved
