"""Export the FastAPI OpenAPI schema to a file.

Usage:
    python -m scripts.generate_openapi [--output PATH] [--format json|yaml] [--indent N]

Examples:
    python -m scripts.generate_openapi
    python -m scripts.generate_openapi --output docs/openapi.yaml --format yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from app.main import app

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "openapi/qa-hub-be.json"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the OpenAPI schema for the QA Hub API.")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help=f"Output file path (default: {DEFAULT_OUTPUT}; extension drives format if --format omitted).",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=("json", "yaml"),
        default=None,
        help="Output format. Inferred from --output extension when omitted (defaults to json).",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="Indentation for JSON output (default: 2).",
    )
    return parser.parse_args(argv)


def _infer_format(output: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    suffix = output.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    return "json"


def _dump(schema: dict[str, Any], fmt: str, indent: int) -> str:
    if fmt == "yaml":
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise SystemExit(
                "YAML output requested but PyYAML is not installed. "
                "Install it with `pip install pyyaml` or use --format json."
            ) from exc
        return yaml.safe_dump(schema, sort_keys=False, allow_unicode=True)
    return json.dumps(schema, indent=indent, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)

    output: Path = args.output or DEFAULT_OUTPUT
    fmt = _infer_format(output, args.format)

    schema = app.openapi()
    payload = _dump(schema, fmt, args.indent)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")

    logger.info("OpenAPI schema written to %s (%s, %d paths)", output, fmt, len(schema.get("paths", {})))
    return 0


if __name__ == "__main__":
    sys.exit(main())

