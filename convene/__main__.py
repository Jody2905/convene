"""Command line.

  python -m convene demo      walk through the booking rules with a throwaway database
"""

import argparse
import sys

from . import demo


def main(argv=None):
    parser = argparse.ArgumentParser(prog="convene")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="walk through the booking rules step by step")
    args = parser.parse_args(argv)

    if args.command == "demo":
        demo.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
