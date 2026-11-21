from packaging.requirements import Requirement


def test_arbitrary_equality_can_precede_spaced_specifier():
    req = Requirement("example===custom,>= 1")
    assert req.name == "example"
    assert str(req.specifier) in {"===custom,>=1", ">=1,===custom"}
