from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import DesignError
from .demo import demo
from .pipeline import build


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Design mounting structures and enclosures around STEP devices."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    demo_parser = commands.add_parser("demo", help="Run with synthetic STEP devices")
    demo_parser.add_argument("--output", type=Path, required=True)
    build_parser = commands.add_parser("build", help="Build from a YAML or JSON design")
    build_parser.add_argument("config", type=Path)
    build_parser.add_argument("--output", type=Path, required=True)
    plan_parser = commands.add_parser(
        "plan", help="Translate a natural-language brief using local Ollama"
    )
    plan_parser.add_argument("brief", type=Path)
    plan_parser.add_argument("--components", type=Path, required=True)
    plan_parser.add_argument("--model", required=True)
    plan_parser.add_argument("--output", type=Path, required=True)
    enclosure_parser = commands.add_parser(
        "enclose", help="Build an enclosure from an explicitly reviewed intent"
    )
    enclosure_parser.add_argument("brief", type=Path)
    enclosure_parser.add_argument("--components", type=Path, required=True)
    enclosure_parser.add_argument("--plan", type=Path, required=True)
    enclosure_parser.add_argument(
        "--accept-plan",
        action="store_true",
        required=True,
        help="Confirm that the plan correctly interprets the brief",
    )
    enclosure_parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "plan":
            from .planning import plan

            plan(
                arguments.brief, arguments.components, arguments.model, arguments.output
            )
            print(f"Plan written: {arguments.output.resolve()}; review before enclose")
            return 0
        if arguments.command == "enclose":
            from .enclosure import enclose

            report = enclose(
                arguments.brief, arguments.components, arguments.plan, arguments.output
            )
        elif arguments.command == "demo":
            report = demo(arguments.output)
        else:
            report = build(arguments.config, arguments.output)
    except (DesignError, OSError, ValueError, RuntimeError) as exc:
        print(f"fastbuild: error: {exc}", file=sys.stderr)
        return 1
    print(
        f"{report['status']}: {len(report['parts'])} parts; "
        f"outputs: {arguments.output.resolve()}"
    )
    print("Not checked: " + ", ".join(report["not_checked"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
