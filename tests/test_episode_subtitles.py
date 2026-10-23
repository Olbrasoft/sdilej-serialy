from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import hashlib
import json

import pytest
import requests
from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier

from sdilej_serialy import episode_subtitles, quality, replenish, subtitle_policy
from sdilej_serialy.catalog import load_jsonl, write_jsonl_gzip
from sdilej_serialy.manifest import SourceManifest
from sdilej_serialy.models import Episode
from sdilej_serialy.pipeline import atomic_json
from sdilej_serialy.subtitle_media import normalize_srt, choose_stream, SubtitleUnavailable
from sdilej_serialy.subtitle_target import Target
from sdilej_serialy.git_state import CheckpointError
from sdilej_serialy import subtitle_http


def english_row():
    episode = Episode(203749, 581, 'Král Tulsy', 'Tulsa King', 4, 1, runtime_min=2)
    candidate = Candidate('9001', 'https://sdilej.cz/9001/tulsa.mkv', 'Tulsa King S04E01 CZ titulky',
                          size_bytes=1000, duration_sec=100, width=3832, height=1632,
                          audio_language='en', language_probability=.99,
                          language_tier=LanguageTier.FOREIGN_AUDIO, match_tier=MatchTier.STRONG)
    payload = ''.join(f'{n}\n00:00:{n:02d},000 --> 00:01:{n:02d},000\nTest cue {n}\n\n' for n in range(1, 21)).encode()
    evidence = subtitle_policy.subtitle_evidence(payload, candidate, dict(method='original_embedded_track', stream_index=2))
    candidate._subtitle_evidence = evidence
    row = dict(identity=episode.identity, episode=episode.to_dict(), selected=candidate.to_dict(),
               quality_policy=quality.QUALITY_POLICY, source_policy=quality.SUBTITLE_POLICY, subtitles=evidence,
               display_name='Král Tulsy S04E01 - 1. epizoda 4K CZ Titulky (titulky čekají)',
               target_account='b', queue_rank=5)
    return episode, candidate, row, normalize_srt(payload)


def test_encoder_crop_keeps_4k_and_smallest_file_preference():
    _, candidate, _, _ = english_row()
    assert quality.resolution_label(3832, 1632) == '4K'
    assert quality.resolution_label(3840, 2160) == '4K'
    assert quality.resolution_label(3696, 1632) == '1440p'
    large = replace(candidate, width=3840, height=2160, size_bytes=2000)
    assert quality.rank_candidates([large, candidate])[0].source_id == candidate.source_id


def test_season_request_survives_catalog_refresh_but_does_not_include_other_seasons():
    assert quality.subtitle_request(dict(series_id=581, season=4))
    assert not quality.subtitle_request(dict(series_id=581, season=3))
    assert not quality.subtitle_request(dict(series_id=99, season=4))
    assert not quality.subtitle_request(dict(series_id=581, season=4, source_policy=None))
    assert not quality.subtitle_request(dict(series_id=581, season=4, source_policy='czech_audio'))


@pytest.mark.parametrize('corruption', ['none', 'policy', 'season', 'series', 'audio', 'tier', 'hash',
                                       'missing_track', 'forced', 'short', 'source', 'url_binding', 'size', 'identity', 'episode', 'runtime'])
def test_only_explicit_matching_english_and_full_czech_text_are_eligible(corruption):
    _, _, row, _ = english_row()
    if corruption == 'policy': row.pop('source_policy')
    elif corruption == 'season': row['episode']['season'] = 3
    elif corruption == 'series': row['episode']['series_id'] = 99
    elif corruption == 'audio': row['selected']['audio_language'] = 'de'
    elif corruption == 'tier': row['selected']['language_tier'] = 'czech_audio'
    elif corruption == 'hash': row['subtitles']['sha256'] = 'invalid'
    elif corruption == 'missing_track': row['subtitles'] = {}
    elif corruption == 'forced': row['subtitles']['full'] = False
    elif corruption == 'short': row['subtitles']['cue_count'] = 3
    elif corruption == 'source': row['subtitles']['source_id'] = '999'
    elif corruption == 'url_binding':
        row['selected']['url'] = row['subtitles']['source_url'] = 'https://sdilej.cz/999/tulsa.mkv'
    elif corruption == 'size': row['subtitles']['source_size_bytes'] += 1
    elif corruption == 'identity': row['identity'] = '581:4:2'
    elif corruption == 'episode': row['selected']['title'] = 'Tulsa King S04E02'
    elif corruption == 'runtime': row['episode']['runtime_min'] = 60
    assert quality.upload_eligible(row) is (corruption == 'none')


def test_due_requested_season_precedes_broad_discovery_without_bypassing_episode_backoff():
    ordinary = dict(episode_id=1, series_id=99, series_title='Other', season=1, episode=1)
    requested = dict(episode_id=2, series_id=581, series_title='Král Tulsy', season=4, episode=1)
    cooling = dict(requested, episode_id=3, episode=2)
    available = lambda row: row != cooling
    result = list(replenish.reserve_order([ordinary, requested, cooling], {}, {}, {581}, available=available))
    assert result == [requested, ordinary]


def test_full_czech_text_is_required_and_forced_or_images_do_not_substitute():
    _, candidate, _, _ = english_row()
    with pytest.raises(SubtitleUnavailable):
        subtitle_policy.subtitle_evidence(b'1\n00:00:00,000 --> 00:00:01,000\nShort\n', candidate, {})
    streams = [dict(index=1, codec_name='hdmv_pgs_subtitle', tags={'language': 'cze'}),
               dict(index=2, codec_name='subrip', tags={'language': 'eng'}),
               dict(index=3, codec_name='subrip', tags={'language': 'ces'}, disposition={'forced': 1})]
    with pytest.raises(SubtitleUnavailable): choose_stream(streams)
    streams.append(dict(index=4, codec_name='subrip', tags={'language': 'ces'}, disposition={'forced': 0}))
    assert choose_stream(streams)['index'] == 4


def test_requested_english_chooses_best_resolution_then_smallest_verified_source(monkeypatch):
    episode, base, _, payload = english_row()
    rows = [replace(base, source_id='large', size_bytes=2000),
            replace(base, source_id='low', width=1920, height=1080, size_bytes=100),
            replace(base, source_id='small', size_bytes=1500),
            replace(base, source_id='czech', size_bytes=500, audio_language='cs', language_tier=LanguageTier.CZECH_AUDIO)]
    inspected = []
    provider = SimpleNamespace(discovery_timeout_seconds=900, session=None,
        search=lambda episode: rows, _inspect=lambda episode, c: inspected.append(c.source_id) or c,
        _verify_language=lambda episode, c: c)
    monkeypatch.setattr(subtitle_policy, 'extract_original', lambda *a, **kw: (payload, {}))
    selected = subtitle_policy.discover_subtitled(provider, episode)
    assert selected.source_id == 'small' and len(inspected) == len(rows)
    assert selected._subtitle_evidence['source_id'] == 'small'
    assert provider._fresh_search is False


@pytest.mark.parametrize('reason,expected', [('source_czech_text_missing', 'large'), ('source_media_timeout', None)])
def test_unknown_better_subtitle_source_defers_but_confirmed_missing_track_can_be_skipped(monkeypatch, reason, expected):
    episode, base, _, payload = english_row()
    small, large = replace(base, source_id='small'), replace(base, source_id='large', size_bytes=2000)
    provider = SimpleNamespace(discovery_timeout_seconds=900, session=None,
        search=lambda episode: [large, small], _inspect=lambda episode, c: c, _verify_language=lambda episode, c: c)
    def extract(session, source, **kwargs):
        if source['source_id'] == 'small': raise SubtitleUnavailable(reason)
        return payload, {}
    monkeypatch.setattr(subtitle_policy, 'extract_original', extract)
    selected = subtitle_policy.discover_subtitled(provider, episode)
    assert (selected.source_id if selected else None) == expected


def target_record(candidate):
    return dict(target_account='b', source=dict(source_id=candidate.source_id, source_url=candidate.url,
                size_bytes=candidate.size_bytes), upload=dict(target_video_id='555', display_name='Král Tulsy S04E01 CZ Titulky (titulky čekají)'))


def test_processing_target_does_not_extract_or_upload():
    _, candidate, row, _ = english_row()
    entry = {}
    episode_subtitles.attach(row, target_record(candidate), entry, Target('555', row['display_name'], False),
        None, None, None, lambda: pytest.fail('Unexpected write'), None, deadline=float('inf'))
    assert entry['status'] == 'target_processing'
    assert 'intent' not in entry


def test_wrong_uploaded_source_cannot_receive_subtitles():
    _, candidate, row, _ = english_row()
    record = target_record(candidate); record['source']['source_id'] = 'other'
    entry = {}
    episode_subtitles.attach(row, record, entry, Target('555', row['display_name'], True),
        None, None, None, lambda: pytest.fail('Unexpected write'), None, deadline=float('inf'))
    assert entry['status'] == 'source_binding_missing'


def test_uncertain_post_is_never_sent_twice(monkeypatch):
    _, candidate, row, payload = english_row()
    monkeypatch.setattr(episode_subtitles, 'extract_original', lambda *a, **k: (payload, {}))
    provider = SimpleNamespace(session=None, _inspect=lambda *a: candidate)
    entry, writes, posts = {}, [], []
    def save(): writes.append(deepcopy(entry))
    def upload(*args):
        assert writes[-1]['status'] == 'upload_intent'
        posts.append(args)
        raise requests.Timeout('Unknown POST result')
    api = SimpleNamespace(upload=upload)
    view = Target('555', row['display_name'], True)
    with pytest.raises(requests.Timeout):
        episode_subtitles.attach(row, target_record(candidate), entry, view, api, provider,
                                 None, save, None, deadline=float('inf'))
    assert entry.get('intent') and len(posts) == 1
    episode_subtitles.attach(row, target_record(candidate), entry, view, api, provider,
                             None, save, None, deadline=float('inf'))
    assert entry['status'] == 'upload_unconfirmed' and len(posts) == 1


def test_verified_own_track_finishes_without_another_upload():
    _, candidate, row, _ = english_row()
    entry = dict(intent=dict(filename='unique.srt', before_ids=[], subtitle_id='7'))
    view = Target('555', row['display_name'], True,
                  [dict(id='7', name='unique', czech=True, processing=False)])
    renamed = []
    api = SimpleNamespace(verify_content=lambda track, digest: digest == row['subtitles']['sha256'])
    episode_subtitles.attach(row, target_record(candidate), entry, view, api, None,
        None, lambda: None, renamed.append, deadline=float('inf'))
    assert entry['status'] == 'attached_verified' and entry['title_updated']
    assert renamed == [row['display_name'].removesuffix(' (titulky čekají)')]


def test_automatically_extracted_matching_track_is_not_uploaded_again():
    _, candidate, row, _ = english_row()
    entry, renamed = {}, []
    view = Target('555', row['display_name'] + '.mkv', True,
                  [dict(id='7', name='Czech', czech=True, processing=False)], unknown_tracks=True)
    api = SimpleNamespace(verify_content=lambda track, digest: digest == row['subtitles']['sha256'])
    episode_subtitles.attach(row, target_record(candidate), entry, view, api, None,
        None, lambda: None, renamed.append, deadline=float('inf'))
    assert entry['status'] == 'attached_verified' and entry['title_updated']
    assert not entry.get('intent')
    assert renamed == [row['display_name'].removesuffix(' (titulky čekají)') + '.mkv']


def test_checkpoint_failure_before_subtitle_post_stops_the_write(monkeypatch):
    _, candidate, row, payload = english_row()
    monkeypatch.setattr(episode_subtitles, 'extract_original', lambda *a, **kw: (payload, {}))
    provider = SimpleNamespace(session=None, _inspect=lambda *a: candidate)
    api = SimpleNamespace(upload=lambda *a: pytest.fail('POST before durable intent'))
    def save(): raise CheckpointError('not durable')
    with pytest.raises(CheckpointError):
        episode_subtitles.attach(row, target_record(candidate), {}, Target('555', row['display_name'], True),
            api, provider, None, save, None, deadline=float('inf'))


def test_changed_original_subtitle_payload_cannot_be_attached(monkeypatch):
    _, candidate, row, payload = english_row()
    monkeypatch.setattr(episode_subtitles, 'extract_original', lambda *a, **kw: (payload.replace(b'Test', b'Other'), {}))
    provider = SimpleNamespace(session=None, _inspect=lambda *a: candidate)
    with pytest.raises(SubtitleUnavailable, match='source_subtitle_changed'):
        episode_subtitles.attach(row, target_record(candidate), {}, Target('555', row['display_name'], True),
            None, provider, None, lambda: pytest.fail('Unexpected write'), None, deadline=float('inf'))


def test_uncertain_language_get_is_only_verified_not_repeated():
    _, candidate, row, _ = english_row()
    entry = dict(intent=dict(filename='unique.srt', before_ids=[], subtitle_id='7'))
    view = Target('555', row['display_name'], True,
                  [dict(id='7', name='unique', czech=False, processing=False, language_action='action')])
    calls = []
    def language(*args): calls.append(args); raise requests.Timeout('Unknown result')
    api = SimpleNamespace(set_own_czech_language=language)
    with pytest.raises(requests.Timeout):
        episode_subtitles.attach(row, target_record(candidate), entry, view, api, None,
            None, lambda: None, None, deadline=float('inf'))
    episode_subtitles.attach(row, target_record(candidate), entry, view, api, None,
        None, lambda: None, None, deadline=float('inf'))
    assert len(calls) == 1 and entry['status'] == 'language_unconfirmed'


@pytest.mark.parametrize('safe,attempts', [(False, 1), (True, 2)])
def test_only_explicit_subtitle_reads_are_retried(monkeypatch, safe, attempts):
    monkeypatch.setattr(subtitle_http.time, 'sleep', lambda delay: None)
    called = []
    def operation():
        called.append(True)
        result = requests.Response(); result.status_code = 503 if len(called) == 1 else 200
        result._content = b''; result._content_consumed = True
        return result
    result = subtitle_http.request(operation, phase='fixed_label', retry_safe=safe)
    assert len(called) == attempts and result.status_code == (200 if safe else 503)


def test_subtitle_retry_after_is_respected_without_a_mutating_retry():
    response = requests.Response(); response.status_code = 429; response.headers['Retry-After'] = '300'
    calls = []
    assert subtitle_http.request(lambda: calls.append(True) or response,
                                 phase='fixed_label', retry_safe=True).status_code == 429
    assert len(calls) == 1


@pytest.mark.parametrize('old_capability', [False, True])
def test_publisher_waits_for_policy_aware_uploader_and_reuses_verified_source(tmp_path, monkeypatch, old_capability):
    from datetime import datetime, UTC, timedelta
    from test_replenish import setup
    directory, _, _ = setup(tmp_path, monkeypatch)
    episode, candidate, row, _ = english_row()
    path = tmp_path / 'backlog/series-episodes.jsonl.gz'
    catalog = load_jsonl(path)
    catalog.append(dict(episode.to_dict(), source_policy=quality.SUBTITLE_POLICY, imdb_rating=8, imdb_votes=100))
    write_jsonl_gzip(path, catalog)
    calls = []
    monkeypatch.setattr(replenish, 'discover_subtitled', lambda *args: calls.append(True) or candidate)
    provider = SimpleNamespace(discover=lambda ep: pytest.fail('Wrong language lane'))
    before = (directory / 'state.json').read_bytes()
    if old_capability:
        atomic_json(directory / 'uploader-capabilities.json', dict(schema_version=1,
                    generation='test', source_policies=[quality.SUBTITLE_POLICY]))
    result = replenish.prepare(tmp_path, 'test', provider, identities=[episode.identity], runtime_minutes=0)
    assert result['prepared_this_run'] == 0 and calls == [True]
    assert episode.identity in SourceManifest(tmp_path / 'manifests/selected-episodes.jsonl').rows
    assert (directory / 'state.json').read_bytes() == before
    atomic_json(directory / 'uploader-capabilities.json', dict(schema_version=1, subtitle_queue_version=2,
                generation='test', source_policies=[quality.SUBTITLE_POLICY]))
    later = datetime.now(UTC) + timedelta(minutes=2)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return later
    monkeypatch.setattr(replenish, 'datetime', Clock)
    result = replenish.prepare(tmp_path, 'test', provider, identities=[episode.identity], runtime_minutes=0)
    assert result['prepared_this_run'] == 1 and calls == [True]
    assert (directory / 'state.json').read_bytes() == before
    from sdilej_serialy.dual import load
    _, _, loaded = load(tmp_path, 'test')
    added = next(r for r in loaded if r['identity'] == episode.identity)
    assert added['source_policy'] == quality.SUBTITLE_POLICY
    assert added['selected']['audio_language'] == 'en'
    assert quality.upload_eligible(added)
    rejected = (directory / 'additions.jsonl').read_text()
    (directory / 'additions.jsonl').write_text(rejected.replace(quality.SUBTITLE_POLICY, 'unsupported-policy'))
    with pytest.raises(ValueError):
        load(tmp_path, 'test')


def test_requested_subtitle_sources_can_be_reused_in_a_future_ranked_generation():
    from sdilej_serialy.dual import ranked_sources
    _, _, row, _ = english_row()
    rejected = deepcopy(row)
    rejected['episode']['series_id'] = 99
    selected = ranked_sources([row, rejected], [dict(row['episode'], imdb_rating=8, imdb_votes=100)])
    assert len(selected) == 1 and selected[0]['source_policy'] == quality.SUBTITLE_POLICY
    assert selected[0]['subtitles'] == row['subtitles']


def test_subtitle_source_repair_keeps_policy_owner_and_proof():
    from sdilej_serialy.source_repair import repair_entry, repaired_row
    _, _, base, _ = english_row()
    replacement = deepcopy(base)
    replacement['selected'].update(source_id='9002', url='https://sdilej.cz/9002/tulsa.mkv')
    replacement['subtitles'].update(source_id='9002', source_url='https://sdilej.cz/9002/tulsa.mkv')
    plan = dict(generation='test', manifest_sha256='a' * 64)
    entry = repair_entry(base, replacement, plan)
    result = repaired_row(base, entry, plan)
    assert result['target_account'] == base['target_account'] and result['queue_rank'] == base['queue_rank']
    assert result['subtitles']['source_id'] == '9002' and quality.upload_eligible(result)
    changed = deepcopy(base); changed['subtitles']['sha256'] = 'b' * 64
    with pytest.raises(ValueError): repaired_row(changed, entry, plan)
    entry['selected'].update(language_tier='czech_audio', audio_language='cs')
    with pytest.raises(ValueError): repaired_row(base, entry, plan)
