"""Enables `python -m computer_use`, the invocation the README documents."""

import sys

from computer_use.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
