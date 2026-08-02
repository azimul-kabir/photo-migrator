"""Safe, read-only media inventory tooling."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("photo-migrator")
except PackageNotFoundError:  # source checkout without an installed distribution
    __version__ = "0+unknown"
