"""Bounded retries for transient authentication transport failures."""
import time
import requests


def login_with_retry(login, email, password):
    for attempt in range(3):
        try:
            return login(email, password)
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as error:
            status = getattr(error.response, 'status_code', None)
            if isinstance(error, requests.HTTPError) and not (status == 429 or status and 500 <= status <= 599):
                raise
            if attempt == 2:
                raise
            # Never include URLs, response bodies, cookies, or credentials.
            print(f'login_retry={attempt + 1} error={type(error).__name__} status={status}', flush=True)
            time.sleep(2 ** (attempt + 1))
