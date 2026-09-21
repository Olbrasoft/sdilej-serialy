from sdilej_serialy.cli import fair_source_order
from sdilej_serialy.pipeline import EpisodeState
from sdilej_serialy.models import Episode


def episode(number):
    return Episode(number, 1, 'Test', None, 1, number)


def test_failed_inspection_survives_restart(tmp_path):
    path = tmp_path/'scan.json'
    state = EpisodeState(path)
    state.inspected(episode(1))
    restored = EpisodeState(path)
    ordered = fair_source_order([episode(1), episode(2)], set(), restored.data['episodes'])
    assert [e.number for e in ordered] == [2, 1]
    assert not restored.uploaded(episode(1))


def test_uploaded_read_does_not_create_fake_scan_progress(tmp_path):
    state = EpisodeState(tmp_path/'scan.json')
    assert not state.uploaded(episode(1))
    assert not state.data['episodes']


def test_old_reviews_do_not_starve_new_backlog():
    episodes = [episode(n) for n in range(1,7)]
    stale = {e.identity for e in episodes[:3]}
    rows = {episodes[0].identity: {'last_inspected_at': '2026-09-13T00:00:00+00:00'}}
    ordered = fair_source_order(episodes, stale, rows)
    assert [e.number for e in ordered] == [4,2,5,3,6,1]


def test_empty_legacy_rows_are_not_attempts():
    ordered = fair_source_order([episode(1),episode(2)], set(), {episode(1).identity:{}})
    assert [e.number for e in ordered] == [1,2]


def test_long_series_cannot_monopolize_source_workers():
    from dataclasses import replace
    first = [episode(n) for n in range(1,51)]
    second = replace(episode(1), series_id=2)
    third = replace(episode(1), series_id=3)
    ordered = fair_source_order(first + [second, third], set(), {})
    assert [e.series_id for e in ordered[:4]] == [1,2,3,1]
    assert len(ordered) == 52
    assert len({e.identity for e in ordered}) == 52
    assert [e.number for e in ordered if e.series_id == 1] == list(range(1,51))


def test_series_rotation_preserves_retry_age_within_series():
    from dataclasses import replace
    other = replace(episode(1), series_id=2)
    rows = {episode(1).identity: {'last_inspected_at':'2026-09-13T00:00:00+00:00'}}
    ordered = fair_source_order([episode(1),episode(2),other], set(), rows)
    assert [e.identity for e in ordered] == [episode(2).identity,other.identity,episode(1).identity]


def test_queue_checkpoints_an_unsuccessful_search(tmp_path, monkeypatch):
    from argparse import Namespace
    from types import SimpleNamespace
    from sdilej_serialy import cli
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    monkeypatch.setattr(cli, 'load_jsonl', lambda _: [episode(1).to_dict()])
    monkeypatch.setattr(cli.EpisodeSourceProvider, 'authenticated',
                        lambda *a: SimpleNamespace(discover=lambda e: None))
    monkeypatch.setenv('SDILEJ_EMAIL', 'test')
    monkeypatch.setenv('SDILEJ_PASSWORD', 'test')
    state_path = tmp_path/'scan.json'
    cli.prepare_queue(Namespace(backlog=tmp_path/'backlog',state=state_path,
                                manifest=tmp_path/'manifest',limit=1,workers=1,
                                runtime_minutes=0,persist_git_state=False))
    restored = EpisodeState(state_path)
    assert restored.row(episode(1))['last_inspected_at']
    assert 'source' not in restored.row(episode(1))


def test_continuous_scan_reschedules_after_bounded_unsuccessful_batch(tmp_path, monkeypatch):
    from argparse import Namespace
    from types import SimpleNamespace
    from sdilej_serialy import cli

    episodes = [episode(n) for n in range(1, 5)]
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    monkeypatch.setattr(cli, 'load_jsonl', lambda _: [e.to_dict() for e in episodes])
    monkeypatch.setattr(cli.EpisodeSourceProvider, 'authenticated', lambda *a: object())
    monkeypatch.setattr(cli, 'require_env', lambda _: 'test')
    ticks = iter([0, 0, 1, 61])
    monkeypatch.setattr(cli, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    batches = []

    def inspect(candidates, provider, state, limit, **kwargs):
        batches.append([e.number for e in candidates])
        for e in candidates:
            kwargs['on_inspected'](e)
        return []

    monkeypatch.setattr(cli, 'build_plan', inspect)
    cli.prepare_queue(Namespace(backlog=tmp_path/'backlog', state=tmp_path/'scan.json',
                                manifest=tmp_path/'manifest', limit=2, workers=1,
                                runtime_minutes=1, persist_git_state=False))
    assert batches == [[1, 2], [3, 4]]
