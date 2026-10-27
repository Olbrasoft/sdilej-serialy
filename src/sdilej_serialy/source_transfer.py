"""Resolve the original at the SDK's source GET, after target preflight reads."""
import re
import time

import requests
from sdilej_to_prehrajto.sdilej import SdilejError

from .source_detail import MIN_ORIGINAL_BYTES


class SourceUnavailable(RuntimeError):
    """A source-only operation failed before any target allocation."""


def stream_size(response):
    """Require a complete media response starting at byte zero."""
    response.raise_for_status()
    content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
    if content_type.startswith('text/') or content_type in (
            'application/json', 'application/xml', 'application/xhtml+xml'):
        raise SdilejError('Original stream returned a document instead of media')
    length = response.headers.get('Content-Length', '')
    size = int(length) if length.isdigit() else None
    if response.status_code == 206:
        match = re.fullmatch(r'bytes 0-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
        if not match or int(match[1]) + 1 != int(match[2]):
            raise SdilejError('Original stream returned an incomplete byte range')
        if size is not None and size != int(match[2]):
            raise SdilejError('Original stream length disagrees with its byte range')
        size = int(match[2])
    elif response.status_code != 200:
        raise SdilejError('Original stream returned an unexpected status')
    elif response.headers.get('Content-Range'):
        raise SdilejError('Original stream returned a range without partial-content status')
    if size is None or size < MIN_ORIGINAL_BYTES:
        raise SdilejError('Original stream size is missing or implausibly small')
    return size


class FreshSourceSession:
    """One relay's source session; never change a shared SDK function/session.

    The pinned SDK calls get after its baseline statistics and before target
    allocation. Refresh the stable detail here, then open its fast-download URL
    immediately. Keep the response open for the relay instead of probing it and
    reconnecting later. Retries are allowed only before returning that response.
    """

    def __init__(self, provider, candidate, *, require_original_size):
        self.provider = provider
        self.candidate = candidate
        self.require_original_size = require_original_size
        self.failure_before_return = None
        self.called = False

    def get(self, _old_url, **kwargs):
        if self.called:
            raise RuntimeError('A source session cannot be reused for another relay')
        self.called = True
        try:
            for attempt in range(3):
                response = None
                try:
                    fresh = self.provider.refresh(self.candidate, session=self.provider.session)
                    if (fresh.source_id, fresh.url) != (self.candidate.source_id, self.candidate.url):
                        raise RuntimeError('Verified source identity changed before stream open')
                    if not fresh.download_url:
                        raise SdilejError('Current original download link is unavailable')
                    response = self.provider.session.get(fresh.download_url, **kwargs)
                    size = stream_size(response)
                    if self.require_original_size and (
                            not self.candidate.size_bytes or size != self.candidate.size_bytes):
                        raise RuntimeError('Recovery original size changed before stream open')
                    print(f'source_stream_opened source_id={fresh.source_id} size_bytes={size} refreshed=true', flush=True)
                    return response
                except (SdilejError, requests.RequestException) as error:
                    if response is not None:
                        response.close()
                    if attempt == 2:
                        raise SourceUnavailable('Original stream unavailable before target allocation') from error
                    print(f'source_open_retry={attempt + 1} error={type(error).__name__}', flush=True)
                    time.sleep(2 ** (attempt + 1))
                except BaseException:
                    if response is not None:
                        response.close()
                    raise
        except Exception as error:
            # Exact exception identity proves this invocation never returned a
            # response to the SDK, so target allocation could not have started.
            self.failure_before_return = error
            raise
