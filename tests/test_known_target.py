from types import SimpleNamespace
import pytest
from sdilej_serialy.target import existing_episode


def response(text,status=200):
    return SimpleNamespace(text=text,status_code=status,raise_for_status=lambda:None)


def test_known_target_in_another_folder_is_reconciled_without_listing():
    calls=[]
    def get(url,**kwargs):
        calls.append(kwargs['params'])
        return response('<h1>Změna složky videa</h1><h2>Series S01E02 - Title 1080p</h2>')
    assert existing_episode(SimpleNamespace(get=get),'Series S01E02 - Title','123')=='123'
    assert calls==[{'videoId':'123'}]


@pytest.mark.parametrize('page',[
    '<h1>Změna složky videa</h1><h2>Other S01E02 - Title</h2>',
    '<h1>Změna složky videa</h1><h2>Series S01E03 - Title</h2>',
    '<h1>Přihlášení</h1><h2>Series S01E02 - Title</h2>',
])
def test_known_id_requires_matching_episode_and_detail_page(page):
    responses=iter([response(page),response('<div id="uploadedVideoListing"></div>')])
    session=SimpleNamespace(get=lambda *a,**kw:next(responses))
    assert existing_episode(session,'Series S01E02 - Title','123') is None


def test_missing_known_target_still_checks_listing():
    responses=iter([response('',404),response('<div id="uploadedVideoListing"></div>')])
    assert existing_episode(SimpleNamespace(get=lambda *a,**kw:next(responses)),
                            'Series S01E02','123') is None
