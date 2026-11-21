import importlib.util
from pathlib import Path

import pytest


script = Path(__file__).resolve().parents[1] / "scripts/probe_click_route_patches.py"
spec = importlib.util.spec_from_file_location("route_patch_probes", script)
probes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probes)


def test_explicit_count_range_is_separate_from_the_public_issue():
    def rendered(value):
        return {"exit": 0, "rows": [f"  -v  Describe this option.{value}\n"]}

    reference = {
        "implicit_count": rendered(""), "count_with_default": rendered("  [default: 2]"),
        "explicit_numeric_bounds": rendered("  [1<=x<=5]"),
        "default_and_bounds": rendered("  [default: 3; 1<=x<=5]"),
        probes.EXPLICIT_COUNT: rendered("  [1<=x<=5]"),
    }
    probes.validate_reference(probes.COUNT, reference)
    lost_bounds = {**reference, probes.EXPLICIT_COUNT: rendered("")}
    result = probes.scope_differences(probes.COUNT, {"cases": lost_bounds}, reference)
    assert result["status"] == "matches_reference"
    assert result["differences"] == {}
    assert set(result["compatibility_differences"]) == {probes.EXPLICIT_COUNT}
    with pytest.raises(ValueError, match="both explicit inclusive bounds"):
        probes.validate_reference(probes.COUNT, lost_bounds)
    lost_default = {**lost_bounds, "count_with_default": rendered("")}
    result = probes.scope_differences(probes.COUNT, {"cases": lost_default}, reference)
    assert result["status"] == "mismatch"
    assert set(result["differences"]) == {"count_with_default"}
    assert set(result["compatibility_differences"]) == {probes.EXPLICIT_COUNT}
