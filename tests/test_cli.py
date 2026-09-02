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
