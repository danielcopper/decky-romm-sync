"""``python -m atlas`` — the CLI without the console script installed."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
