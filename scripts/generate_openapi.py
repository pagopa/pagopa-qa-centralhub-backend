"""Export the FastAPI OpenAPI schema to a file.

Usage:
    python -m scripts.generate_openapi [--output PATH] [--format json|yaml]
        [--indent N] [--openapi-version VERSION] [--apim-compatible]

Examples:
    python -m scripts.generate_openapi
    python -m scripts.generate_openapi --openapi-version 3.0.3
    python -m scripts.generate_openapi --apim-compatible
    python -m scripts.generate_openapi --output docs/openapi.yaml --format yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from fastapi.routing import APIRoute

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
    parser.add_argument(
        "--openapi-version",
        default=None,
        help="OpenAPI specification version (default: 3.1.0; 3.0.3 with --apim-compatible).",
    )
    parser.add_argument(
        "--apim-compatible",
        action="store_true",
        help="Convert nullable anyOf schemas to OpenAPI 3.0 nullable schemas for Azure APIM.",
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


def _make_apim_compatible(value: Any) -> Any:
    if isinstance(value, list):
        return [_make_apim_compatible(item) for item in value]
    if not isinstance(value, dict):
        return value

    value = {key: _make_apim_compatible(item) for key, item in value.items()}
    for exclusive_key, limit_key in (
        ("exclusiveMinimum", "minimum"),
        ("exclusiveMaximum", "maximum"),
    ):
        exclusive_value = value.get(exclusive_key)
        if isinstance(exclusive_value, int | float) and not isinstance(exclusive_value, bool):
            value[limit_key] = exclusive_value
            value[exclusive_key] = True

    any_of = value.get("anyOf")
    if not isinstance(any_of, list) or len(any_of) != 2:
        return value

    non_null_schemas = [schema for schema in any_of if schema != {"type": "null"}]
    if len(non_null_schemas) != 1:
        return value

    schema = non_null_schemas[0]
    if not isinstance(schema, dict):
        return value

    value.pop("anyOf")
    value.update(schema)
    value["nullable"] = True
    return value


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)

    output: Path = args.output or DEFAULT_OUTPUT
    fmt = _infer_format(output, args.format)

    app.openapi_version = args.openapi_version or ("3.0.3" if args.apim_compatible else "3.1.0")
    app.openapi_schema = None

    for route in app.routes:
        if isinstance(route, APIRoute):
            if route.path in (app.openapi_url, app.docs_url, app.redoc_url):
                route.include_in_schema = True

    schema = app.openapi()
    if args.apim_compatible:
        schema = _make_apim_compatible(schema)
    payload = _dump(schema, fmt, args.indent)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")

    logger.info("OpenAPI schema written to %s (%s, %d paths)", output, fmt, len(schema.get("paths", {})))
    return 0


if __name__ == "__main__":
    sys.exit(main())

