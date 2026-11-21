import pytest

from packaging.markers import Marker, UndefinedComparison


def test_set_valued_environment_rejects_accidental_string():
    with pytest.raises(UndefinedComparison):
        Marker('"doc" in extras').evaluate({"extras": "docs"}, context="lock_file")
