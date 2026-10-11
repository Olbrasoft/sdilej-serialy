import pytest
from sdilej_to_prehrajto.models import Candidate
from sdilej_to_prehrajto.sdilej import SdilejError
from sdilej_serialy.source_detail import parse_detail_html


@pytest.mark.parametrize('host', ['data8.sdilej.cz', 'stream12.sdilej.cz'])
def test_player_cdn_hosts_and_escaped_query(host):
    page = f'''<h1>Series S04E10.mp4</h1><p>1920x1080 42:16</p>
    <a href="https://data8.sdilej.cz/sdilej_profi.php?id=1">Stáhnout <span>rychle</span></a>
    <video><source src="https://{host}/download_free_stream.php?id=1&amp;stream=1"></video>'''
    candidate = Candidate(source_id='1', url='https://sdilej.cz/1/video', title='Series')
    result = parse_detail_html(page, candidate)
    assert result.sample_url == result.download_url
    assert (result.width,result.height) == (1920,1080)
    assert result.download_url.endswith('sdilej_profi.php?id=1')
    assert 'sample_url' not in result.to_dict()


def test_missing_player_is_not_silently_treated_as_verified():
    candidate = Candidate(source_id='1', url='https://sdilej.cz/1/video', title='Series')
    with pytest.raises(SdilejError):
        parse_detail_html('<h1>Video</h1>', candidate)


def test_original_without_player_or_known_cdn_hostname():
    candidate = Candidate(source_id='1', url='https://sdilej.cz/1/video', title='Series')
    result = parse_detail_html('<a href="https://new-cdn.example/media?token=test&amp;id=1">Stáhnout rychle</a>', candidate)
    assert result.download_url == 'https://new-cdn.example/media?token=test&id=1'
    assert result.sample_url == result.download_url


def test_redirect_uses_original_size_not_preview_or_range_length():
    from types import SimpleNamespace
    from sdilej_serialy.source_detail import resolve_original
    closed = []
    response = SimpleNamespace(status_code=206, headers={'Content-Range':'bytes 0-0/865831506','Content-Length':'1'},
        url='https://new-cdn.example/original', raise_for_status=lambda:None, close=lambda:closed.append(True))
    session = SimpleNamespace(get=lambda *a, **kw:response)
    candidate = Candidate(source_id='1',url='https://sdilej.cz/1/video',title='Series',download_url='https://sdilej.cz/download')
    result = resolve_original(session,candidate)
    assert result.size_bytes == 865831506
    assert result.sample_url == result.download_url == response.url
    assert closed


@pytest.mark.parametrize('status,headers', [
    (200, {'Content-Length': '18'}),
    (200, {'Content-Length': '0'}),
    (200, {}),
    (200, {'Content-Length': 'invalid'}),
    (200, {'Content-Length': '865831506', 'Content-Type': 'text/html; charset=UTF-8'}),
    (200, {'Content-Length': '865831506', 'Content-Type': 'application/json'}),
    (206, {'Content-Range': 'bytes 0-0/18', 'Content-Length': '1'}),
    (206, {'Content-Length': '865831506'}),
    (206, {'Content-Range': 'bytes 0-0/*'}),
    (206, {'Content-Range': 'bytes 2-2/865831506'}),
    (204, {'Content-Length': '865831506'}),
])
def test_invalid_original_response_never_overwrites_verified_size(status, headers):
    from types import SimpleNamespace
    from sdilej_serialy.source_detail import resolve_original
    closed = []
    response = SimpleNamespace(status_code=status, headers=headers,
        url='https://cdn.example/original', raise_for_status=lambda: None,
        close=lambda: closed.append(True))
    candidate = Candidate(source_id='1', url='https://sdilej.cz/1/video', title='Series',
                          size_bytes=865831506, download_url='https://sdilej.cz/download')
    with pytest.raises(SdilejError):
        resolve_original(SimpleNamespace(get=lambda *a, **kw: response), candidate)
    assert candidate.size_bytes == 865831506
    assert closed == [True]


def test_original_server_can_ignore_range_and_return_full_media():
    from types import SimpleNamespace
    from sdilej_serialy.source_detail import resolve_original
    response = SimpleNamespace(status_code=200,
        headers={'Content-Length': '865831506', 'Content-Type': 'application/octet-stream'},
        url='https://cdn.example/original', raise_for_status=lambda: None, close=lambda: None)
    candidate = Candidate(source_id='1', url='https://sdilej.cz/1/video', title='Series',
                          download_url='https://sdilej.cz/download')
    result = resolve_original(SimpleNamespace(get=lambda *a, **kw: response), candidate)
    assert result.size_bytes == 865831506


@pytest.mark.parametrize('etag,needs_samples', [(None, True), ('W/"weak"', True), ('"strong"', False)])
def test_request_timestamp_is_not_a_stable_original_validator(etag, needs_samples):
    from types import SimpleNamespace
    from sdilej_serialy.source_detail import resolve_original
    headers = {'Content-Range': 'bytes 0-0/2000000',
               'Date': 'Sun, 11 Oct 2026 11:00:14 GMT',
               'Last-Modified': 'Sun, 11 Oct 2026 11:00:15 GMT'}
    if etag:
        headers['ETag'] = etag
    response = SimpleNamespace(status_code=206, headers=headers, url='https://cdn.example/original',
                               raise_for_status=lambda: None, close=lambda: None)
    evidence = {}
    resolve_original(SimpleNamespace(get=lambda *a, **kw: response), Candidate(
        '1', 'https://sdilej.cz/1/video', 'title', download_url='fast'), evidence=evidence)
    assert 'last_modified' not in evidence
    assert bool(evidence.get('needs_content_fingerprint')) == needs_samples
    assert evidence['etag'] == etag


def test_sampled_fingerprint_is_bounded_and_detects_same_size_middle_changes():
    from types import SimpleNamespace
    from sdilej_serialy.source_detail import sampled_content_fingerprint
    requested, closed = [], []
    middle = [b'a']
    size = 2000000
    def get(url, **kwargs):
        assert url == 'https://new-cdn.example/original'
        assert kwargs['stream'] and kwargs['headers']['Accept-Encoding'] == 'identity'
        requested.append(kwargs['headers']['Range'])
        start, end = map(int, requested[-1][6:].split('-'))
        assert end - start + 1 == 65536
        content = middle[0] * 65536 if start == size // 2 else b'x' * 65536
        return SimpleNamespace(status_code=206, headers={'Content-Range': f'bytes {start}-{end}/{size}'},
            iter_content=lambda **kw: iter([content]), close=lambda: closed.append(True))
    candidate = Candidate('1', 'https://sdilej.cz/1/video', 'title', size_bytes=size,
                          download_url='https://new-cdn.example/original')
    session = SimpleNamespace(get=get)
    first = sampled_content_fingerprint(session, candidate)
    assert first == sampled_content_fingerprint(session, candidate)
    middle[0] = b'b'
    assert first != sampled_content_fingerprint(session, candidate)
    assert len(requested) == len(closed) == 9


@pytest.mark.parametrize('mode', ['ignored_range', 'wrong_range', 'short_body', 'error'])
def test_unsupported_fingerprint_does_not_authorize_stale_cache_or_read_full_file(mode):
    from types import SimpleNamespace
    import requests
    from sdilej_serialy.source_detail import sampled_content_fingerprint
    closed, reads = [], []
    def get(*args, **kwargs):
        if mode == 'error':
            raise requests.Timeout('unavailable')
        return SimpleNamespace(status_code=200 if mode == 'ignored_range' else 206,
            headers={'Content-Range': 'bytes 1-65536/2000000' if mode == 'wrong_range' else 'bytes 0-65535/2000000'},
            iter_content=lambda **kw: reads.append(True) or iter([b'short']), close=lambda: closed.append(True))
    candidate = Candidate('1', 'https://sdilej.cz/1/video', 'title', size_bytes=2000000, download_url='fast')
    assert sampled_content_fingerprint(SimpleNamespace(get=get), candidate) is None
    assert bool(reads) == (mode == 'short_body')
    assert bool(closed) == (mode != 'error')
