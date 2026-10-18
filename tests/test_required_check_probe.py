"""Temporary probe: proves a failing required check blocks merge. Deleted after the check."""


def test_required_check_probe() -> None:
    assert False, "intentional failure for branch-protection enforcement proof"
