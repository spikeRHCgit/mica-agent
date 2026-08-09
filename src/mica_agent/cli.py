from __future__ import annotations

import argparse
from importlib.metadata import version


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mica",
        description="A lightweight agent foundation for personal developers.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('mica-agent')}",
    )
    parser.parse_args()
    parser.print_help()
