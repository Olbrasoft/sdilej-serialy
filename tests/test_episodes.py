import requests

from sdilej_serialy.episodes import EpisodeSourceProvider, episode_match, has_exact_code
from sdilej_serialy.models import Episode
from sdilej_to_prehrajto.language import LanguageDetectionError
from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier


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


def candidate(source_id: str, *, height: int, size_bytes: int, language: LanguageTier) -> Candidate:
    return Candidate(
        source_id=source_id,
        url=f"https://sdilej.cz/{source_id}/test.mkv",
        title="Teorie velkého třesku S01E01",
        size_bytes=size_bytes,
        duration_sec=100,
        width=1920 if height == 1080 else 1280,
        height=height,
        language_tier=language,
        match_tier=MatchTier.STRONG,
    )


def provider_with(monkeypatch, candidates, verify):
    provider = EpisodeSourceProvider(requests.Session(), detector=object(), request_gap_seconds=0)
    monkeypatch.setattr(provider, "search", lambda _episode: candidates)
    monkeypatch.setattr(provider, "_verify", verify)
    return provider


def test_discovery_prefers_czech_audio_over_higher_resolution(monkeypatch):
    foreign_1080p = candidate("foreign", height=1080, size_bytes=100_000_000, language=LanguageTier.FOREIGN_AUDIO)
    czech_720p = candidate("czech", height=720, size_bytes=100_000_000, language=LanguageTier.CZECH_AUDIO)
    provider = provider_with(monkeypatch, [foreign_1080p, czech_720p], lambda _episode, item: item)

    assert provider.discover(episode()) is czech_720p


def test_discovery_stops_after_smallest_verified_czech_source(monkeypatch):
    smaller = candidate("small", height=1080, size_bytes=100_000_000, language=LanguageTier.CZECH_AUDIO)
    larger = candidate("large", height=1080, size_bytes=200_000_000, language=LanguageTier.CZECH_AUDIO)
    verified = []

    def verify(_episode, item):
        verified.append(item.source_id)
        return item

    provider = provider_with(monkeypatch, [larger, smaller], verify)

    assert provider.discover(episode()) is smaller
    assert verified == ["small"]


def test_discovery_defers_after_an_unresolved_smaller_source(monkeypatch):
    unresolved = candidate("unresolved", height=1080, size_bytes=100_000_000, language=LanguageTier.UNKNOWN)
    czech = candidate("czech", height=1080, size_bytes=200_000_000, language=LanguageTier.CZECH_AUDIO)

    def verify(_episode, item):
        if item is unresolved:
            raise LanguageDetectionError("temporary failure")
        return item

    provider = provider_with(monkeypatch, [czech, unresolved], verify)

    assert provider.discover(episode()) is None
