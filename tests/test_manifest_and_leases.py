from datetime import UTC, datetime, timedelta

from sdilej_serialy.manifest import SourceManifest
from sdilej_serialy.models import Episode
from sdilej_serialy.pipeline import EpisodeState, build_plan


def episode() -> Episode:
    return Episode(episode_id=11, series_id=3, series_title="Test", series_original_title=None, season=1, number=2)


def test_second_worker_cannot_take_live_lease(tmp_path):
    state = EpisodeState(tmp_path / "episodes.json")
    assert state.claim(episode(), "worker-a")
    assert not state.claim(episode(), "worker-b")


def test_expired_lease_can_be_safely_reclaimed(tmp_path):
    state = EpisodeState(tmp_path / "episodes.json")
    state.row(episode())["claim"] = {"lease_expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
    assert state.claim(episode(), "replacement-worker")


def test_manifest_rejects_ephemeral_authenticated_urls(tmp_path):
    manifest = SourceManifest(tmp_path / "sources.jsonl")
    row = {"identity": "3:1:2", "selected": {"url": "https://sdilej.cz/1/file.mkv", "download_url": "https://secret"}}
    try:
        manifest.add(row)
    except ValueError as error:
        assert "Authenticated" in str(error)
    else:
        raise AssertionError("manifest accepted an authenticated URL")


def test_build_plan_publishes_each_verified_episode_immediately(tmp_path):
    candidate = type(
        "CandidateStub",
        (),
        {
            "source_id": "source-1",
            "url": "https://sdilej.cz/1/test.mkv",
            "title": "Test S01E02",
            "filename": "Test.S01E02.mkv",
            "size_bytes": 100,
            "duration_sec": 120,
            "width": 1920,
            "height": 1080,
            "audio_language": "cs",
            "language_probability": 0.99,
            "language_tier": type("Tier", (), {"name": "CZECH_AUDIO"})(),
            "match_evidence": {"episode_code_match": True},
            "to_dict": lambda self: {
                "source_id": self.source_id,
                "url": self.url,
                "title": self.title,
            },
        },
    )()
    provider = type("ProviderStub", (), {"discover": lambda self, item: candidate})()
    published = []

    rows = build_plan(
        [episode()],
        provider,
        EpisodeState(tmp_path / "source-state.json"),
        1,
        on_prepared=lambda row: published.append(row["identity"]),
    )

    assert len(rows) == 1
    assert published == [episode().identity]
