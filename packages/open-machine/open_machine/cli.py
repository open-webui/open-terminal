"""Thin wrapper that forwards everything to open-terminal's CLI."""

import sys


def main():
    """Entry point that delegates to open-terminal's CLI."""
    from open_terminal.cli import main as open_terminal_main

    sys.argv[0] = "open-terminal"
    open_terminal_main()
