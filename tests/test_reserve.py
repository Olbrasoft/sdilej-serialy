from datetime import UTC, datetime, timedelta

from sdilej_serialy.reserve import should_prepare, stock


def test_stock_counts_only_unused_sources_and_hours_not_historical_additions():
    now = datetime.now(UTC)
    rows = [dict(identity=str(n), target_account='a' if n % 2 else 'b') for n in range(1, 7)]
    state = dict(episodes={
        '1': dict(upload=dict(uploaded_at=now.isoformat())),
        '2': dict(upload=dict(uploaded_at=(now - timedelta(days=2)).isoformat())),
        '3': dict(attempts=[dict(error='SourceUnavailable')]),
        '4': dict(prepared_target=dict(target_video_id='123')),
        '5': dict(claim=dict(worker_id='active'))})
    result = stock(rows, state, now=now)
    assert result['ready'] == 1 and result['ready_by_account'] == dict(a=0, b=1)
    assert result['uploads_last_24h'] == 1 and result['stock_hours_24h'] == 24
    assert result['categories'] == dict(completed=2, failed=1, allocated=1, in_flight=1, ready=1)
    assert state['episodes']['4']['prepared_target']['target_video_id'] == '123'


def test_watermark_hysteresis_and_audit_priority():
    assert should_prepare(dict(ready=999), {})
    assert not should_prepare(dict(ready=1000), {})
    assert should_prepare(dict(ready=2999), dict(refilling=True))
    assert not should_prepare(dict(ready=3000), dict(refilling=True))
