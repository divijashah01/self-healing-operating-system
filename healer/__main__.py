import argparse
import logging

from .config import load_config
from .daemon import Healer


def main() -> None:
    parser = argparse.ArgumentParser(prog="healer", description="Self-healing daemon")
    parser.add_argument("--config", help="path to healer.yaml (default: config/healer.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    Healer(load_config(args.config)).run()


if __name__ == "__main__":
    main()