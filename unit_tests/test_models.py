from claimcheck.models import Claim, Evidence, Status, Verdict


def ev(status):
    return Evidence(Claim("symbol", "x"), status, "d")


def test_empty_verdict_is_unverified():
    assert Verdict().outcome == "unverified"


def test_all_confirmed_is_supported():
    assert Verdict([ev(Status.CONFIRMED), ev(Status.CONFIRMED)]).outcome == "supported"


def test_any_refuted_wins():
    assert Verdict([ev(Status.CONFIRMED), ev(Status.REFUTED)]).outcome == "refuted"


def test_confirmed_plus_unverified_is_not_supported():
    assert Verdict([ev(Status.CONFIRMED), ev(Status.UNVERIFIED)]).outcome == "unverified"
