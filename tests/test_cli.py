from argparse import Namespace

from sdilej_serialy import cli


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
