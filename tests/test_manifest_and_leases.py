from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier
from sdilej_serialy import continuous
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


def test_manifest_merges_new_remote_rows_without_duplicates(tmp_path):
    manifest = SourceManifest(tmp_path / "sources.jsonl")
    first = {"identity": "3:1:2", "selected": {"url": "https://sdilej.cz/1/test.mkv"}}
    second = {"identity": "3:1:3", "selected": {"url": "https://sdilej.cz/2/test.mkv"}}
    manifest.add(first)

    changed = manifest.merge_jsonl(
        "\n".join((__import__("json").dumps(first), __import__("json").dumps(second)))
    )

    assert changed == 1
    assert set(manifest.rows) == {"3:1:2", "3:1:3"}


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


def test_idle_worker_refills_new_unique_episode_while_upload_is_running(monkeypatch, tmp_path):
    first_episode = episode()
    second_episode = Episode(
        episode_id=12,
        series_id=3,
        series_title="Test",
        series_original_title=None,
        season=1,
        number=3,
    )

    def row(item, source_id):
        selected = Candidate(
            source_id=source_id,
            url=f"https://sdilej.cz/{source_id}/test.mkv",
            title=f"Test {item.code}",
            size_bytes=100,
            duration_sec=100,
            width=1920,
            height=1080,
            language_tier=LanguageTier.CZECH_AUDIO,
            match_tier=MatchTier.STRONG,
        )
        return {
            "episode": item.to_dict(),
            "identity": item.identity,
            "selected": selected.to_dict(),
            "display_name": f"Test {item.code} 1080p CZ Dabing",
        }

    first = row(first_episode, "source-1")
    second = row(second_episode, "source-2")
    relayed = []

    class Provider:
        session = object()

        def refresh(self, candidate, *, session):
            return candidate

    monkeypatch.setattr(
        continuous,
        "EpisodeSourceProvider",
        SimpleNamespace(authenticated=lambda *_args: Provider()),
    )
    monkeypatch.setattr(continuous, "target_session", lambda *_args: object())
    monkeypatch.setattr(continuous.prehrajto, "uploaded_video_count", lambda _session: 0)
    monkeypatch.setattr(continuous.prehrajto, "uploaded_video_id_by_name", lambda *_args: None)
    monkeypatch.setattr(continuous.prehrajto, "uploaded_video_confirmed", lambda *_args: True)

    def relay(_target, _source, candidate, display_name, _description, *, on_prepared):
        on_prepared(candidate.source_id, 100)
        relayed.append(display_name)
        __import__("time").sleep(0.03 if candidate.source_id == "source-1" else 0.01)
        return SimpleNamespace(video_id=candidate.source_id)

    monkeypatch.setattr(continuous.prehrajto, "relay_upload", relay)
    state = EpisodeState(tmp_path / "episodes.json")

    result = continuous.upload_continuously(
        [first],
        state,
        workers=2,
        source_email="source@example.test",
        source_password="x",
        target_email="target@example.test",
        target_password="x",
        refill_rows=lambda: [first, second],
        refill_interval_seconds=0.001,
    )

    assert set(relayed) == {first["display_name"], second["display_name"]}
    assert result["queued"] == 2
    assert result["uploaded_or_reconciled"] == 2
    assert continuous.uploaded_identities(state) == {first_episode.identity, second_episode.identity}
