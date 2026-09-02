from argparse import Namespace

from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier
from sdilej_serialy import cli
from sdilej_serialy.models import Episode


class ReadOnlyConnection:
    def rollback(self):
        pass

    def close(self):
        pass


def test_targeted_export_is_not_lost_to_top_series_limit(monkeypatch, tmp_path):
    rows = [
        {"series_id": 1, "series_priority_rank": 1, "season": 1, "episode": 1},
        {"series_id": 572, "series_priority_rank": 42, "season": 1, "episode": 1},
    ]
    captured = {}
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(cli, "readonly_connection", lambda _: ReadOnlyConnection())
    monkeypatch.setattr(cli, "fetch_episode_rows", lambda _: rows)
    monkeypatch.setattr(cli, "prepare_episodes", lambda value: value)
    monkeypatch.setattr(cli, "write_jsonl_gzip", lambda _, value: captured.setdefault("rows", list(value)))

    cli.export_catalog(
        Namespace(
            series_limit=1,
            series_id=572,
            season=1,
            episode=1,
            episode_limit=1,
            out=tmp_path / "episodes.jsonl.gz",
        )
    )

    assert captured["rows"] == [rows[1]]


def test_continuous_persists_its_report_for_the_next_poll(monkeypatch, tmp_path):
    state_path = tmp_path / "state" / "episodes.json"
    report_path = tmp_path / "reports" / "continuous.json"
    persisted = []

    class Persister:
        def __init__(self, root, extra_paths):
            assert root == tmp_path
            assert extra_paths == (report_path,)

        def __call__(self, path):
            persisted.append(path)

    class Manifest:
        def __init__(self, _path):
            pass

        def pending(self, _uploaded, *, limit):
            assert limit == 50
            return []

    monkeypatch.setenv("CONTINUOUS_ENABLED", "true")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "GitCheckpointPersister", Persister)
    monkeypatch.setattr(cli, "SourceManifest", Manifest)
    monkeypatch.setattr(cli, "uploaded_identities", lambda _state: set())
    monkeypatch.setattr(
        cli,
        "upload_continuously",
        lambda *args, **kwargs: {"queued": 0, "uploaded_or_reconciled": 0},
    )
    monkeypatch.setattr(cli, "require_env", lambda name: name)

    cli.continuous(
        Namespace(
            state=state_path,
            manifest=tmp_path / "manifest.jsonl",
            report=report_path,
            persist_git_state=True,
            limit=50,
            workers=6,
        )
    )

    assert report_path.exists()
    assert persisted == [state_path]


def test_prepare_queue_uses_disjoint_parallel_source_workers(monkeypatch, tmp_path):
    episodes = [
        Episode(
            episode_id=number,
            series_id=3,
            series_title="Test",
            series_original_title=None,
            season=1,
            number=number,
        )
        for number in range(1, 5)
    ]
    inspected = []

    class Provider:
        def discover(self, episode):
            inspected.append(episode.identity)
            return Candidate(
                source_id=f"source-{episode.number}",
                url=f"https://sdilej.cz/{episode.number}/test.mkv",
                title=f"Test {episode.code}",
                filename=f"Test.{episode.code}.mkv",
                size_bytes=100,
                duration_sec=100,
                width=1920,
                height=1080,
                audio_language="cs",
                language_probability=0.99,
                language_tier=LanguageTier.CZECH_AUDIO,
                match_tier=MatchTier.STRONG,
            )

    monkeypatch.setenv("SDILEJ_EMAIL", "source@example.test")
    monkeypatch.setenv("SDILEJ_PASSWORD", "test")
    monkeypatch.setattr(cli, "load_jsonl", lambda _path: [item.to_dict() for item in episodes])
    monkeypatch.setattr(
        cli.EpisodeSourceProvider,
        "authenticated",
        lambda *_args: Provider(),
    )
    manifest_path = tmp_path / "selected-episodes.jsonl"

    cli.prepare_queue(
        Namespace(
            backlog=tmp_path / "episodes.jsonl.gz",
            state=tmp_path / "source-scan.json",
            manifest=manifest_path,
            limit=4,
            workers=2,
            runtime_minutes=0,
            persist_git_state=False,
        )
    )

    assert sorted(inspected) == sorted(item.identity for item in episodes)
    assert len(inspected) == len(set(inspected))
    assert len(manifest_path.read_text(encoding="utf-8").splitlines()) == 4
