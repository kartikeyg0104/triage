"""Keep every repo the service reads inside one root directory."""

from pathlib import Path


def inside_root(root: Path, relative: str | Path) -> Path | None:
    """The resolved directory, or None if it is missing, not a directory, or outside root
    once '..' and symlinks are resolved. `root` must already be resolved."""
    try:
        path = (root / relative).resolve()
    except (OSError, ValueError):
        return None
    return path if path.is_relative_to(root) and path.is_dir() else None
