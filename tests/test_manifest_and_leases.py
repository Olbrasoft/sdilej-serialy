from datetime import UTC, datetime, timedelta

from sdilej_serialy.manifest import SourceManifest
from sdilej_serialy.models import Episode
from sdilej_serialy.pipeline import EpisodeState


def episode() -> Episode:
    return Episode(episode_id=11, series_id=3, series_title="Test", series_original_title=None, season=1, number=2)


def test_second_worker_cannot_take_live_lease(tmp_path):
    state = EpisodeState(tmp_path / "episodes.json")
    assert state.claim(episode(), "worker-a")
    assert not state.claim(episode(), "worker-b")


def test_manifest_rejects_ephemeral_authenticated_urls(tmp_path):
    manifest = SourceManifest(tmp_path / "sources.jsonl")
    row = {"identity": "3:1:2", "selected": {"url": "https://sdilej.cz/1/file.mkv", "download_url": "https://secret"}}
    try:
        manifest.add(row)
    except ValueError as error:
        assert "Authenticated" in str(error)
    else:
        raise AssertionError("manifest accepted an authenticated URL")
