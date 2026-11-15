"""Grade synthetic interviews with opted-in real models, or validate inputs offline."""

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from eki.config import ConfigError, get_settings, model_name
from eki.evals.driver import ROOT, load_pairs, run_all
from eki.evals.guard import has_provider_key
from eki.evals.models import Report
from eki.evals.report import compare, render, save


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=ROOT / "evals/scenarios")
    parser.add_argument("--golden", type=Path, default=ROOT / "evals/golden")
    parser.add_argument("--only", help="one scenario ID")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-calls", type=_positive, default=40)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--compare", type=Path, metavar="PREVIOUS.json")
    args = parser.parse_args(argv)
    try:
        pairs = load_pairs(args.scenarios, args.golden)
        if args.only:
            pairs = tuple(pair for pair in pairs if pair[0].id == args.only)
            if not pairs:
                raise ValueError(f"unknown scenario: {args.only}")
        if args.dry_run:
            for scenario, _ in pairs:
                print(f"{scenario.id}: {scenario.title} (question cap {scenario.question_cap})")
            print(f"{len(pairs)} scenarios validated; no database or model work")
            return 0
        # Check the actual process before any SDK import can load a .env file.
        if not has_provider_key():
            raise ConfigError(
                "no provider key exported to the process; a .env entry alone is not consent"
            )
        try:
            settings = get_settings()
        except ValidationError:
            raise ConfigError(
                "invalid settings; EKI_DATABASE_URL and valid EKI settings are required"
            ) from None
        names = {role: model_name(settings, role) for role in ("drafter", "interviewer")}
        path = args.report or ROOT / "reports/evals" / (
            datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S.%fZ") + ".json"
        )
        if not path.resolve().is_relative_to((ROOT / "reports").resolve()):
            raise ValueError("--report must be inside reports/")
        if path.exists():
            raise ValueError("--report already exists; choose a new path")
        previous = None
        if args.compare:
            try:
                previous = Report.model_validate_json(args.compare.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raise ValueError("--compare must name a valid readable report") from None
        os.environ.setdefault("LITELLM_MODE", "PRODUCTION")
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        from eki.llm.litellm_model import LiteLLMModel

        domains = frozenset(d for scenario, _ in pairs for d in scenario.expert.work_domains)
        models = {
            role: LiteLLMModel(
                name, timeout=settings.model_timeout_seconds, work_email_domains=domains
            )
            for role, name in names.items()
        }
        report = asyncio.run(
            run_all(
                pairs,
                dsn=settings.database_url.get_secret_value(),
                drafter=models["drafter"],
                interviewer=models["interviewer"],
                max_calls=args.max_calls,
                timeout_seconds=settings.model_timeout_seconds,
            )
        )
        save(report, path)
        print(render(report))
        print(f"Report: {path}")
        regressed = False
        if previous is not None:
            changes, regressed = compare(previous, report)
            print(changes)
        return int(report.passed != len(report.scenarios) or regressed)
    except (ConfigError, ValueError, OSError) as error:
        # Never print a connection/SDK exception or a settings validation dump.
        print(
            f"grade: {error}"
            if not isinstance(error, OSError)
            else "grade: could not read or write eval files",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
