"""Structured segment IDs are provenance, not floating-point literals."""
import pytest

from water_agent.docgen.quality import numeric_preservation_problems


@pytest.mark.parametrize("suffix", ["9001e9173059", "123456789012", "6f8a400ead1b"])
def test_segment_id_is_not_a_bare_numeric_value(suffix: str) -> None:
    text = f"- steel DN200：25.0 m（来源：seg-{suffix}(25m)）"
    assert numeric_preservation_problems(text, {}, text) == []


@pytest.mark.parametrize("value", ["1e9999", "9001e9173059", "-1e9999"])
def test_actual_overflow_value_remains_rejected_even_in_base(value: str) -> None:
    text = f"流量为 {value} L/s"
    assert any("非有限" in p for p in numeric_preservation_problems(text, {}, text))


def test_segment_id_does_not_hide_adjacent_untrusted_value() -> None:
    base = "来源：seg-9001e9173059，长度 25 m"
    changed = "来源：seg-9001e9173059，长度 9001e9173059 m"
    assert any("非有限" in p for p in numeric_preservation_problems(changed, {}, base))


def test_new_segment_id_is_not_exempt_without_trusted_base() -> None:
    assert any("非有限" in p for p in numeric_preservation_problems(
        "来源：seg-9001e9173059", {}, "来源：seg-6f8a400ead1b",
    ))


def test_segment_id_suffix_is_not_added_to_numeric_whitelist() -> None:
    base = "来源：seg-123456789012"
    assert any("底座之外" in p for p in numeric_preservation_problems(
        base + "，额外流量 123456789012 L/s", {}, base,
    ))
