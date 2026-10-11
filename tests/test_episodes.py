import requests
import pytest

from sdilej_serialy.episodes import EpisodeSourceProvider, episode_match, has_exact_code, runtime_acceptable
from sdilej_serialy.models import Episode
from sdilej_to_prehrajto.language import LanguageDetectionError
from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier
from sdilej_to_prehrajto.sdilej import SdilejError


def episode() -> Episode:
    return Episode(episode_id=1, series_id=7, series_title="Teorie velkého třesku", series_original_title="The Big Bang Theory", season=1, number=1)


def test_accepts_both_episode_notations():
    assert has_exact_code("The Big Bang Theory S01E01 CZ", episode())
    assert has_exact_code("The Big Bang Theory 1x1 CZ", episode())


@pytest.mark.parametrize('code', ['S01E01', '1x1'])
def test_underscore_separated_episode_and_series_words(code):
    title = f'The_Big_Bang_Theory_1080p_{code}_CZ.mkv'
    assert has_exact_code(title, episode())
    assert episode_match(episode(), title)[0] == MatchTier.STRONG


def test_mash_underscore_release_matches_catalog_asterisks():
    ep = Episode(episode_id=22493, series_id=108, series_title='M*A*S*H',
                 series_original_title='M*A*S*H', season=2, number=23)
    assert episode_match(ep, 'M A S H_S02E23_Pošta volá.mkv')[0] == MatchTier.STRONG
    assert episode_match(ep, 'MASH_S02E23_Pošta volá_1080p_CZdab.mkv')[0] == MatchTier.STRONG
    assert episode_match(ep, 'Nash Bridges S02E23 CZ')[0] == MatchTier.REJECT
    assert episode_match(ep, 'MASH S02E23-24 CZ')[0] == MatchTier.REJECT


def test_mash_search_includes_compact_acronym(monkeypatch):
    from types import SimpleNamespace
    p = EpisodeSourceProvider(None, detector=object())
    urls = []
    def get(url):
        urls.append(url)
        return SimpleNamespace(text='')
    monkeypatch.setattr(p, '_get', get)
    ep = Episode(episode_id=22493, series_id=108, series_title='M*A*S*H',
                 series_original_title='M*A*S*H', season=2, number=23)
    assert p.search(ep) == []
    assert any('/mash-s02e23/' in url for url in urls)


@pytest.mark.parametrize('title', ['The Big Bang Theory S01E01-02', 'The Big Bang Theory S01E01+E02'])
def test_episode_ranges_are_not_single_episodes(title):
    assert episode_match(episode(), title)[0] == MatchTier.REJECT


@pytest.mark.parametrize('code', ['XS01E01', 'S01E01X', 'S01E0100', '11x1', 'éS01E01'])
def test_episode_boundaries_do_not_match_embedded_or_other_codes(code):
    assert not has_exact_code(f'The Big Bang Theory_{code}_CZ', episode())


def test_underscore_multiepisode_and_sequel_are_still_rejected():
    assert episode_match(episode(), 'The_Big_Bang_Theory_S01E01_S01E02_CZ')[0] == MatchTier.REJECT
    assert episode_match(episode(), 'The_Big_Bang_Theory_II_S01E01_CZ')[0] == MatchTier.REJECT


def test_runtime_rejects_same_title_remake_episode():
    animated_episode = Episode(
        episode_id=1,
        series_id=748,
        series_title="Avatar: Legenda o Aangovi",
        series_original_title="Avatar: The Last Airbender",
        season=1,
        number=5,
        runtime_min=25,
    )

    assert runtime_acceptable(animated_episode, 1_520)
    assert not runtime_acceptable(animated_episode, 3_101)


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


def test_rejects_a_sequel_with_the_same_base_series_title():
    planet_earth = Episode(
        episode_id=1,
        series_id=998,
        series_title="Zázračná planeta",
        series_original_title="Planet Earth",
        season=1,
        number=6,
    )

    tier, evidence = episode_match(planet_earth, "Planet Earth II 2016 S01E06 1080p CZ")

    assert tier == MatchTier.REJECT
    assert evidence["unmatched_series_prefix"] == "ii"


def test_accepts_both_series_aliases_and_release_year_before_episode_code():
    tier, evidence = episode_match(
        Episode(
            episode_id=1,
            series_id=26,
            series_title="Perníkový táta",
            series_original_title="Breaking Bad",
            season=5,
            number=11,
        ),
        "Pernikovy tata Breaking Bad 2012 S05E11 CZ",
    )

    assert tier == MatchTier.STRONG
    assert evidence["unmatched_series_prefix"] == ""


def candidate(source_id: str, *, height: int, size_bytes: int | None, language: LanguageTier) -> Candidate:
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
    monkeypatch.setattr(provider, "_inspect", lambda _episode, item: item)
    monkeypatch.setattr(provider, "_verify_language", verify)
    return provider


def test_discovery_prefers_czech_audio_over_higher_resolution(monkeypatch):
    foreign_1080p = candidate("foreign", height=1080, size_bytes=100_000_000, language=LanguageTier.FOREIGN_AUDIO)
    czech_720p = candidate("czech", height=720, size_bytes=100_000_000, language=LanguageTier.CZECH_AUDIO)
    provider = provider_with(monkeypatch, [foreign_1080p, czech_720p], lambda _episode, item: item)

    assert provider.discover(episode()) is czech_720p


def test_czech_sd_is_selected_when_hd_has_only_foreign_audio(monkeypatch):
    from dataclasses import replace
    foreign = candidate('hd', height=1080, size_bytes=100_000_000, language=LanguageTier.FOREIGN_AUDIO)
    sd = replace(candidate('sd', height=480, size_bytes=50_000_000,
                           language=LanguageTier.CZECH_AUDIO), width=854)
    provider = provider_with(monkeypatch, [foreign, sd], lambda _episode, item: item)
    assert provider.discover(episode()) is sd


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


def test_original_4k_is_not_hidden_by_720p_preview(monkeypatch):
    from dataclasses import replace
    preview = candidate('4k', height=720, size_bytes=100_000_000, language=LanguageTier.CZECH_AUDIO)
    full_hd = candidate('hd', height=1080, size_bytes=200_000_000, language=LanguageTier.CZECH_AUDIO)
    original = replace(preview, width=3840, height=2160, size_bytes=400_000_000)
    provider = provider_with(monkeypatch, [full_hd, preview], lambda _episode, item: item)
    monkeypatch.setattr(provider, '_inspect', lambda _episode, item: original if item is preview else item)
    assert provider.discover(episode()) is original


def test_original_sizes_determine_order_not_preview_sizes(monkeypatch):
    from dataclasses import replace
    first = candidate('first', height=1080, size_bytes=100_000_000, language=LanguageTier.CZECH_AUDIO)
    second = candidate('second', height=1080, size_bytes=200_000_000, language=LanguageTier.CZECH_AUDIO)
    larger_original = replace(first, size_bytes=500_000_000)
    provider = provider_with(monkeypatch, [first, second], lambda _episode, item: item)
    monkeypatch.setattr(provider, '_inspect', lambda _episode, item: larger_original if item is first else item)
    assert provider.discover(episode()) is second


def test_unresolved_original_metadata_defers_selection(monkeypatch):
    item = candidate('unknown', height=720, size_bytes=100_000_000, language=LanguageTier.CZECH_AUDIO)
    provider = provider_with(monkeypatch, [item], lambda _episode, item: item)
    def inspect(*args):
        raise SdilejError('temporary failure')
    monkeypatch.setattr(provider, '_inspect', inspect)
    assert provider.discover(episode()) is None


@pytest.mark.parametrize('metadata', [
    {'video_codec': None, 'width': 0, 'height': 0, 'duration_sec': 7759},
    {},
    {'video_codec': 'h264', 'width': 0, 'height': 0, 'duration_sec': 1500},
])
def test_inspection_rejects_confirmed_nonvideo_but_defers_probe_failures(monkeypatch, metadata):
    from types import SimpleNamespace
    from sdilej_serialy import episodes
    item = candidate('audio', height=1080, size_bytes=186221873, language=LanguageTier.CZECH_AUDIO)
    p = EpisodeSourceProvider(None, detector=object())
    monkeypatch.setattr(p, '_get', lambda _: SimpleNamespace(text='detail'))
    monkeypatch.setattr(episodes, 'parse_detail_html', lambda *args: item)
    monkeypatch.setattr(episodes, 'resolve_original', lambda *args: item)
    monkeypatch.setattr(episodes, 'probe_media', lambda *args: metadata)
    if metadata.get('video_codec', 'missing') is None:
        assert p._inspect(episode(), item) is None
    else:
        with pytest.raises(SdilejError, match='incomplete'):
            p._inspect(episode(), item)


def test_discovery_checks_known_smallest_size_before_unknown_size(monkeypatch):
    known = candidate("known", height=1080, size_bytes=100_000_000, language=LanguageTier.CZECH_AUDIO)
    unknown = candidate("unknown", height=1080, size_bytes=None, language=LanguageTier.CZECH_AUDIO)
    verified = []

    def verify(_episode, item):
        verified.append(item.source_id)
        return item

    provider = provider_with(monkeypatch, [unknown, known], verify)

    assert provider.discover(episode()) is known
    assert verified == ["known"]


def test_discovery_defers_after_an_unresolved_smaller_source(monkeypatch):
    unresolved = candidate("unresolved", height=1080, size_bytes=100_000_000, language=LanguageTier.UNKNOWN)
    czech = candidate("czech", height=1080, size_bytes=200_000_000, language=LanguageTier.CZECH_AUDIO)

    def verify(_episode, item):
        if item is unresolved:
            raise LanguageDetectionError("temporary failure")
        return item

    provider = provider_with(monkeypatch, [czech, unresolved], verify)

    assert provider.discover(episode()) is None


def test_unresolved_higher_resolution_defers_lower_resolution(monkeypatch):
    unresolved = candidate("unresolved", height=1080, size_bytes=100_000_000, language=LanguageTier.UNKNOWN)
    czech = candidate("czech", height=720, size_bytes=100_000_000, language=LanguageTier.CZECH_AUDIO)

    def verify(_episode, item):
        if item is unresolved:
            raise LanguageDetectionError("temporary failure")
        return item

    provider = provider_with(monkeypatch, [unresolved, czech], verify)

    assert provider.discover(episode()) is None


def test_discovery_timeout_skips_a_problematic_episode(monkeypatch):
    provider = EpisodeSourceProvider(
        requests.Session(),
        detector=object(),
        request_gap_seconds=0,
        discovery_timeout_seconds=0,
    )
    monkeypatch.setattr(
        provider,
        "search",
        lambda _episode: (_ for _ in ()).throw(AssertionError("search must not start after deadline")),
    )

    assert provider.discover(episode()) is None


def test_discovery_retries_a_transient_search_timeout(monkeypatch):
    expected = candidate("working", height=1080, size_bytes=100_000_000, language=LanguageTier.CZECH_AUDIO)
    provider = EpisodeSourceProvider(requests.Session(), detector=object(), request_gap_seconds=0)
    attempts = 0

    def search(_episode):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SdilejError("temporary timeout")
        return [expected]

    monkeypatch.setattr(provider, "search", search)
    monkeypatch.setattr(provider, "_inspect", lambda _episode, item: item)
    monkeypatch.setattr(provider, "_verify_language", lambda _episode, item: item)

    assert provider.discover(episode()) is expected
    assert attempts == 2


def test_discovery_defers_episode_after_repeated_search_timeouts(monkeypatch):
    provider = EpisodeSourceProvider(requests.Session(), detector=object(), request_gap_seconds=0)
    attempts = 0

    def search(_episode):
        nonlocal attempts
        attempts += 1
        raise SdilejError("temporary timeout")

    monkeypatch.setattr(provider, "search", search)

    assert provider.discover(episode()) is None
    assert attempts == 2


def test_empty_search_is_confirmed_by_fresh_search_before_missing_result(monkeypatch):
    expected = candidate('working', height=1080, size_bytes=100_000_000, language=LanguageTier.CZECH_AUDIO)
    provider = EpisodeSourceProvider(requests.Session(), detector=object(), request_gap_seconds=0)
    freshness = []
    def search(_):
        freshness.append(getattr(provider, '_fresh_search', False))
        return [] if len(freshness) == 1 else [expected]
    monkeypatch.setattr(provider, 'search', search)
    monkeypatch.setattr(provider, '_inspect', lambda ep, item: item)
    monkeypatch.setattr(provider, '_verify_language', lambda ep, item: item)
    assert provider.discover(episode()) is expected
    assert freshness == [False, True]
    assert not provider._fresh_search


def test_empty_search_followed_by_timeout_is_transient_not_proof_of_missing_source(monkeypatch):
    provider = EpisodeSourceProvider(requests.Session(), detector=object(), request_gap_seconds=0)
    calls = []
    def search(_):
        calls.append(1)
        if len(calls) == 1:
            return []
        raise SdilejError('temporary search outage')
    monkeypatch.setattr(provider, 'search', search)
    assert provider.discover(episode()) is None
    assert provider.last_outcome == 'transient_search'
    assert len(calls) == 2
    assert not provider._fresh_search


def test_two_completed_empty_searches_are_still_reported_as_missing(monkeypatch):
    provider = EpisodeSourceProvider(requests.Session(), detector=object(), request_gap_seconds=0)
    calls = []
    monkeypatch.setattr(provider, 'search', lambda _: calls.append(1) or [])
    assert provider.discover(episode()) is None
    assert provider.last_outcome == 'no_matches'
    assert len(calls) == 2
