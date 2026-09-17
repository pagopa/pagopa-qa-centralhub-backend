from scripts.generate_openapi import _make_apim_compatible


def test_make_apim_compatible_converts_numeric_exclusive_bounds() -> None:
    schema = {
        "type": "number",
        "exclusiveMinimum": 0.0,
        "exclusiveMaximum": 10.0,
    }

    assert _make_apim_compatible(schema) == {
        "type": "number",
        "exclusiveMinimum": True,
        "exclusiveMaximum": True,
        "minimum": 0.0,
        "maximum": 10.0,
    }


def test_make_apim_compatible_converts_nullable_numeric_exclusive_bound() -> None:
    schema = {
        "anyOf": [
            {"type": "number", "exclusiveMinimum": 0.0},
            {"type": "null"},
        ]
    }

    assert _make_apim_compatible(schema) == {
        "type": "number",
        "exclusiveMinimum": True,
        "minimum": 0.0,
        "nullable": True,
    }