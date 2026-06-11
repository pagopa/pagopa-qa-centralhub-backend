from __future__ import annotations

import ast
import re

REPORT_LINE_RE = re.compile(r"report data (\{.*\})")


def parse_report_line(log_text: str) -> dict | None:
    match = REPORT_LINE_RE.search(log_text)
    if not match:
        return None
    try:
        data = ast.literal_eval(match.group(1))
    except (ValueError, SyntaxError):
        return None
    if not isinstance(data, dict):
        return None
    return data
