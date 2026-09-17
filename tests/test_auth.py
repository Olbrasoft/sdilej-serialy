import pytest
import requests
from sdilej_serialy import auth
from sdilej_serialy.episodes import EpisodeSourceProvider


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


@pytest.mark.parametrize('error',[http_error(522),http_error(503),http_error(429),requests.Timeout(),requests.ConnectionError()])
def test_transient_login_is_retried(error,monkeypatch):
    delays=[]
    monkeypatch.setattr(auth.time,'sleep',delays.append)
    calls=[]
    def login(*args):
        calls.append(args)
        if len(calls)==1:raise error
        return 'session'
    assert auth.login_with_retry(login,'email','password')=='session'
    assert len(calls)==2
    assert delays==[2]


@pytest.mark.parametrize('error',[http_error(401),http_error(403),RuntimeError('invalid credentials')])
def test_permanent_failure_is_not_retried(error,monkeypatch):
    monkeypatch.setattr(auth.time,'sleep',lambda _:pytest.fail('unexpected retry'))
    def login(*args):raise error
    with pytest.raises(type(error)):
        auth.login_with_retry(login,'email','password')


def test_retries_are_bounded(monkeypatch):
    delays=[]
    monkeypatch.setattr(auth.time,'sleep',delays.append)
    def login(*args):raise http_error(522)
    with pytest.raises(requests.HTTPError):
        auth.login_with_retry(login,'email','password')
    assert delays==[2,4]


def test_source_authentication_recovers_from_522(monkeypatch):
    from sdilej_serialy import episodes
    monkeypatch.setattr(auth.time,'sleep',lambda _:None)
    session=requests.Session()
    responses=iter([http_error(522),session])
    def login(*args):
        result=next(responses)
        if isinstance(result,Exception):raise result
        return result
    monkeypatch.setattr(episodes,'login',login)
    assert EpisodeSourceProvider.authenticated('email','password',detector=object()).session is session
