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
