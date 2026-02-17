from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gmail-r2-backup")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

__all__ = ["__version__"]
