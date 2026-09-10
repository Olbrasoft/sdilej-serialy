from types import SimpleNamespace
import pytest
import requests
from sdilej_serialy.episodes import EpisodeSourceProvider
from sdilej_serialy.models import Episode
from sdilej_to_prehrajto.sdilej import SdilejError


def result(source_id,title):
    return f'<div class="videobox"><a class="webm-hover" href="/{source_id}/video.mkv" title="{title}">{title}</a></div>'


def test_search_includes_bare_series_and_all_result_pages(monkeypatch):
    p=EpisodeSourceProvider(requests.Session(),detector=object(),request_gap_seconds=0)
    e=Episode(1,5964,'Zázračná planeta II','Planet Earth II',1,2)
    visited=[]
    def get(url):
        visited.append(url)
        if 's01e02' in url:
            return SimpleNamespace(text='')
        if 'page=2' in url:
            return SimpleNamespace(text=result('10974880','Zázračná planeta II - 02 Pohoří.mkv'))
        return SimpleNamespace(text='<a rel="next" href="?page=2">Next</a>')
    monkeypatch.setattr(p,'_get',get)
    assert [c.source_id for c in p.search(e)]==['10974880']
    assert any('page=2' in url for url in visited)


def test_search_defers_when_pagination_loops(monkeypatch):
    p=EpisodeSourceProvider(requests.Session(),detector=object(),request_gap_seconds=0)
    e=Episode(1,3,'Test',None,1,1)
    monkeypatch.setattr(p,'_get',lambda url:SimpleNamespace(text=f'<a rel="next" href="{url}">Next</a>'))
    with pytest.raises(SdilejError):
        p.search(e)
