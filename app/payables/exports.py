"""Where Freight Payables writes its workbooks: a folder picked on the Freight Payables page.

Docker shares one folder of the PC or server with the service at /exports and names it in
PAYABLES_EXPORT_HOST (both in docker-compose.override.yml, for example the OneDrive folder).
People pick a folder inside it, typed after its name (OCR AP\\Exports) or pasted whole from
File Explorer; it is kept as the part after the shared folder.
"""

import os
import re
import uuid
from pathlib import Path

DEFAULT_FOLDER = ("OCR AP", "Exports")


def mount():
    """Where the shared folder is inside the service."""
    return Path(os.environ.get("PAYABLES_EXPORT_MOUNT", "/exports"))


def host_root():
    """The shared folder as the PC or server shows it; empty when none is shared."""
    return os.environ.get("PAYABLES_EXPORT_HOST", "").strip()


def _windows():
    return bool(re.match(r"^[A-Za-z]:", host_root()))


def _parts(path):
    return [part for part in re.split(r"[\\/]+", (path or "").strip()) if part not in ("", ".")]


def _join(parts):
    return ("\\" if _windows() else "/").join(parts)


def root_name():
    """The shared folder's own name, as the page shows it: OneDrive - ATV INC."""
    parts = _parts(host_root())
    return parts[-1] if parts else ""


def default_folder():
    return _join(DEFAULT_FOLDER)


def full_path(folder):
    """The folder as the PC shows it, from the part after the shared folder."""
    if not folder:
        return host_root()
    return ("/" if host_root().startswith("/") else "") + _join([*_parts(host_root()), *_parts(folder)])


NOT_SHARED = "Exports aren't set up on this computer yet. Ask the administrator."


def relative(typed):
    """(the part after the shared folder, None), or (None, why not), for a folder typed after the
    shared folder's name or pasted whole. Windows folder names match whatever their case."""
    root = host_root()
    if not root:
        return None, NOT_SHARED
    typed_parts, root_parts = _parts(typed), _parts(root)
    pasted = bool(re.match(r"^([A-Za-z]:|[\\/])", (typed or "").strip()))
    if pasted:
        same = (lambda a, b: a.lower() == b.lower()) if _windows() else (lambda a, b: a == b)
        if len(typed_parts) < len(root_parts) or not all(same(a, b) for a, b in zip(typed_parts, root_parts)):
            return None, f"Choose a folder inside {root_name()}."
        typed_parts = typed_parts[len(root_parts):]
    if ".." in typed_parts:
        return None, "Type the folder without “..”."
    return _join(typed_parts), None


def folder(rel):
    """The folder inside the service."""
    return mount().joinpath(*_parts(rel))


def check(typed):
    """(the folder to keep, None) once it exists and can be written, or (None, why not)."""
    rel, error = relative(typed)
    if error:
        return None, error
    target = folder(rel)
    probe = target / f".ocr-write-test-{uuid.uuid4().hex}"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        return None, f"That folder can't be used: {exc.strerror or exc}."
    return rel, None


def next_run(rel):
    """The folder the next export goes in: Run N, one after the highest Run already in the export
    folder (Run 1 in an empty one), as the scanner numbered its runs. Nothing is ever overwritten."""
    numbers = [int(m.group(1)) for e in os.scandir(folder(rel)) if e.is_dir()
               for m in [re.fullmatch(r"Run (\d+)", e.name, re.IGNORECASE)] if m] if folder(rel).is_dir() else []
    return folder(rel) / f"Run {max(numbers, default=0) + 1}"


def suggestions(depth=2, limit=300):
    """Folders already in the shared folder, two levels down, offered as the folder is typed."""
    found, level = [], [()]
    for _ in range(depth):
        below = []
        for parts in level:
            try:
                names = sorted(e.name for e in os.scandir(mount().joinpath(*parts))
                               if e.is_dir() and not e.name.startswith("."))
            except OSError:
                continue
            for name in names:
                below.append((*parts, name))
                found.append(_join((*parts, name)))
                if len(found) >= limit:
                    return found
        level = below
    return found
