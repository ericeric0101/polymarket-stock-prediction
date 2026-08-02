"""Backward-compatible CLI facade.

Command implementations live in :mod:`polymarket_stock.commands.entrypoint`.
"""

from .commands.entrypoint import (
    _await_with_graceful_shutdown,  # noqa: F401
    _report_public_api_failure,  # noqa: F401
    build_parser,
    main,
)

__all__ = ("build_parser", "main")


if __name__ == "__main__":
    main()
