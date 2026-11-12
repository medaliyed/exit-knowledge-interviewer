"""Command line entry point: ``uv run python -m tests.fixtures.generator``."""

import argparse
from pathlib import Path

from tests.fixtures.generator import DEFAULT_OUT_DIR, DEFAULT_SEED, generate


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic eki test corpus.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    arguments = parser.parse_args()
    generate(seed=arguments.seed, out_dir=arguments.out)
    print(f"Wrote corpus for seed {arguments.seed} under {arguments.out}")


if __name__ == "__main__":
    main()
