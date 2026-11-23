from orbitkit_tools.passes import GroundSite, Pass, PassPhase, PassPlanner


def test_planner_keeps_the_higher_of_two_overlapping_passes():
    a = Pass("KESTREL-1", 0, 600_000, 23.0, 437.5e6)
    b = Pass("AURORA-3", 60_000, 500_000, 71.0, 435.9e6)
    c = Pass("PIKA-SAT", 700_000, 900_000, 8.0, 145.8e6)       # below the mask
    planner = PassPlanner(GroundSite("home", 47.6, 8.5))
    assert [p.satellite for p in planner.plan([a, b, c])] == ["AURORA-3"]
    assert planner.conflicts([a, b, c]) == [(a, b)]


def test_phases():
    p = Pass("PIKA-SAT", 100_000, 400_000, 40.0, 145.8e6)
    assert [p.phase(t) for t in (0, 110_000, 200_000, 390_000, 500_000)] == [
        PassPhase.UPCOMING, PassPhase.ACQUIRING, PassPhase.TRACKING, PassPhase.FADING, PassPhase.DONE]
