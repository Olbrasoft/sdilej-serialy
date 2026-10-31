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


@pytest.mark.parametrize('status', [302, 503, 522])
def test_sdk_profile_verification_recovers_with_a_fresh_session(status, monkeypatch):
    from sdilej_to_prehrajto import prehrajto
    from requests.adapters import BaseAdapter
    from io import BytesIO
    session_type = requests.Session
    sessions, closed, delays = [], [], []
    class Adapter(BaseAdapter):
        def send(self, request, **kwargs):
            response = requests.Response()
            response.status_code = status if request.url.endswith('/profil') and len(sessions) < 3 else 200
            response.url = request.url
            response.request = request
            response._content = b''
            response.raw = BytesIO(b'')
            return response
        def close(self):
            closed.append(True)
    def session():
        value = session_type()
        value.mount('https://', Adapter())
        sessions.append(value)
        return value
    monkeypatch.setattr(auth.requests, 'Session', session)
    monkeypatch.setattr(auth.time, 'sleep', delays.append)
    result = auth.target_login_with_retry(prehrajto.login, prehrajto.EXPECTED_EMAIL, 'test-password')
    assert result is sessions[2] and delays == [2, 4] and len(closed) == 2
    assert result.hooks['response'] == []


def test_inconclusive_target_login_is_bounded_and_keeps_secrets_out_of_logs(monkeypatch, capsys):
    from sdilej_to_prehrajto.prehrajto import PrehrajtoError
    from sdilej_serialy.resilience import transient_http
    calls, delays = [], []
    monkeypatch.setattr(auth.time, 'sleep', delays.append)
    def login(*args, session):
        calls.append(session)
        raise PrehrajtoError('Target account login verification failed')
    with pytest.raises(auth.TargetLoginUnavailable) as caught:
        auth.target_login_with_retry(login, 'private-email', 'private-password')
    assert transient_http(caught.value) and len(calls) == 3 and delays == [2, 4]
    output = capsys.readouterr().out
    assert 'private-email' not in output and 'private-password' not in output


@pytest.mark.parametrize('error', [http_error(401), http_error(403), RuntimeError('account mismatch')])
def test_explicit_authentication_rejections_are_not_retried(error, monkeypatch):
    monkeypatch.setattr(auth.time, 'sleep', lambda _: pytest.fail('Unexpected retry'))
    def login(*args, session):
        raise error
    with pytest.raises(type(error)):
        auth.target_login_with_retry(login, 'email', 'password')
