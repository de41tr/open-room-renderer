"""Read explicit startup input without applying state or probing resources."""
from __future__ import annotations

import argparse
import sys

from .config import StartupConfigError, read_startup_config


def add_config_parser(subparsers) -> None:
    parser = subparsers.add_parser("config", help="check explicit startup configuration input")
    commands = parser.add_subparsers(dest="config_cmd", required=True)
    check = commands.add_parser(
        "check", help="check startup input without starting the renderer",
        description=(
            "Check YAML/document construction and startup logging/gain conversions only. "
            "Does not create identity, read saved state, probe ports or start/contact mpv. "
            "Other runtime validation, including final channel balance after saved-state "
            "overrides, still happens during service startup."
        ),
    )
    check.add_argument("--config", required=True, help="explicit readable YAML file; no default fallback")


def run_config_check(args: argparse.Namespace) -> int:
    """Report only the existing startup input contract, with owned diagnostics."""
    try:
        read_startup_config(args.config)
    except StartupConfigError as exc:
        print(f"Configuration check failed: {exc}", file=sys.stderr)
        return 2
    print("Startup input checks passed. Runtime settings are not fully validated; "
          "saved state, ports, backend and audio devices were not checked.")
    return 0
