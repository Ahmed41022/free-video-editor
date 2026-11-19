"""`python -m video_converter`: the desktop app, or the command line when files are given (see
`cli`)."""

import sys

from .cli import main, run_cli

__all__ = ["main", "run_cli"]

if __name__ == "__main__":
    sys.exit(main())
