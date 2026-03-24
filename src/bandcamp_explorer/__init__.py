from importlib.metadata import version

from . import app, core

__version__ = version("bandcamp-explorer")

__all__ = ["__version__", "app", "core"]
