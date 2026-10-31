"""Bounded retries for transient authentication transport failures."""
import time
import requests


class TargetLoginUnavailable(RuntimeError):
    """The target did not confirm a session; no upload is authorized."""


def target_login_with_retry(login, email, password):
    """Preserve HTTP failures hidden by the SDK's profile verification."""
    from sdilej_to_prehrajto.prehrajto import PrehrajtoError

    def attempt(email, password):
        session = requests.Session()
        def check_status(response, **kwargs):
            # The SDK checks login POST status but turns every non-200 profile
            # response, including 503, into a permanent authentication error.
            response.raise_for_status()
        session.hooks['response'].append(check_status)
        try:
            result = login(email, password, session=session)
        except Exception as error:
            session.close()
            if (isinstance(error, PrehrajtoError)
                    and str(error) == 'Target account login verification failed'):
                raise TargetLoginUnavailable('Target session verification is inconclusive') from error
            raise
        session.hooks['response'].remove(check_status)
        return result

    return login_with_retry(attempt, email, password)


def login_with_retry(login, email, password):
    for attempt in range(3):
        try:
            return login(email, password)
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError, TargetLoginUnavailable) as error:
            status = getattr(getattr(error, 'response', None), 'status_code', None)
            if isinstance(error, requests.HTTPError) and not (status == 429 or status and 500 <= status <= 599):
                raise
            if attempt == 2:
                raise
            # Never include URLs, response bodies, cookies, or credentials.
            print(f'login_retry={attempt + 1} error={type(error).__name__} status={status}', flush=True)
            time.sleep(2 ** (attempt + 1))
