from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from stram_quant_mcp.clean_views import exact, frequency


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0.460000000000"), "0.46"),
        (Decimal("-9903.000000000000"), "-9903"),
        (Decimal("540497.000000000000"), "540497"),
        (Decimal("0E-12"), "0"),
        (Decimal("0.000000000001"), "0.000000000001"),
        (Decimal("120"), "120"),
    ],
)
def test_exact_trims_zeros_without_changing_the_value(value: Decimal, expected: str) -> None:
    assert exact(value) == expected
    assert Decimal(expected) == value


def test_exact_keeps_digits_beyond_the_decimal_context() -> None:
    long = Decimal("12345678901234567890.123456789012345678900000")

    with localcontext() as context:
        context.prec = 10
        text = exact(long)

    assert text == "12345678901234567890.1234567890123456789"
    assert Decimal(text) == long


def test_a_missing_decimal_stays_missing() -> None:
    assert exact(None) is None


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [("D", "daily"), ("day", "daily"), ("daily", "daily"), ("M", "monthly"), ("W", "weekly")],
)
def test_frequency_spellings_become_one_word(spelling: str, expected: str) -> None:
    assert frequency(spelling) == expected


def test_unknown_frequency_is_kept_as_given() -> None:
    assert frequency("every full moon") == "every full moon"
    assert frequency(None) is None
