from sdilej_serialy.episodes import episode_match, has_exact_code
from sdilej_serialy.models import Episode


def episode() -> Episode:
    return Episode(episode_id=1, series_id=7, series_title="Teorie velkého třesku", series_original_title="The Big Bang Theory", season=1, number=1)


def test_accepts_both_episode_notations():
    assert has_exact_code("The Big Bang Theory S01E01 CZ", episode())
    assert has_exact_code("The Big Bang Theory 1x1 CZ", episode())


def test_rejects_other_episode_of_same_series():
    tier, evidence = episode_match(episode(), "Teorie velkého třesku S01E02 CZ")
    assert tier.value == "reject"
    assert not evidence["episode_code_match"]


def test_requires_series_identity_for_strong_match():
    tier, evidence = episode_match(episode(), "Teorie velkého třesku S01E01 CZ")
    assert tier.value == "strong"
    assert evidence["series_alias_match"]


def test_rejects_a_candidate_with_multiple_episode_codes():
    tier, evidence = episode_match(episode(), "Teorie velkého třesku S01E01 S01E02 CZ")
    assert tier.value == "reject"
    assert evidence["reason"] == "multiple_episode_codes"


def test_rejects_an_episode_code_without_a_series_identity():
    tier, _evidence = episode_match(episode(), "Completely different show S01E01 CZ")
    assert tier.value == "reject"
