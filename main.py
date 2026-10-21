"""Convenience entry point for the same CLI as ``python -m cli.main``.

Importing this module never starts an analysis or selects a historical NVDA run.
"""

from cli.main import app


if __name__ == "__main__":
    app()
