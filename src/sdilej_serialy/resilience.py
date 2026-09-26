"""Read-only HTTP retries and durable evidence for uncertain target transfers."""
import requests
from threading import Event
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from sdilej_to_prehrajto import prehrajto

TRANSIENT_STATUSES = (408, 429, 500, 502, 503, 504)


class TargetPending(RuntimeError):
    """Keep the allocated target; never create a replacement automatically."""


def transient_http(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (requests.Timeout, requests.ConnectionError, requests.exceptions.RetryError)):
            return True
        if isinstance(error, requests.HTTPError):
            return getattr(error.response, 'status_code', None) in TRANSIENT_STATUSES
        error = error.__cause__ or error.__context__
    return False


def error_evidence(error):
    """Never persist exception messages, request URLs, cookies or credentials."""
    evidence = {'type': type(error).__name__}
    status = getattr(getattr(error, 'response', None), 'status_code', None)
    if isinstance(status, int):
        evidence['http_status'] = status
    return evidence


def retry_target_reads(session):
    if not isinstance(session, requests.Session):
        return session
    # connect=0 prevents retries of POST requests even before transmission.
    # Status/read retries apply only to safe reads, never allocation or upload.
    session.mount(prehrajto.BASE_URL + '/', HTTPAdapter(max_retries=Retry(
        total=3, connect=0, read=3, status=3, other=0,
        allowed_methods=frozenset({'GET', 'HEAD'}),
        status_forcelist=TRANSIENT_STATUSES, backoff_factor=2)))
    return session


def receipt_requester(state, episode):
    finished = Event()
    finished.set()
    def request(url, **kwargs):
        finished.clear()
        try:
            response = requests.post(url, **kwargs)
            encoder = kwargs['data']
            reader = encoder.fields[0][1][1]
            if response.status_code in (200, 201) and reader.position == reader.total:
                with state._lock:
                    record = state.row(episode)
                    prepared = record.get('prepared_target') or {}
                    if prepared.get('target_video_id') and prepared.get('size_bytes') == reader.total:
                        record['transfer_receipt'] = {
                            'target_video_id': prepared['target_video_id'],
                            'size_bytes': reader.total,
                            'source_bytes_read': reader.position,
                            'http_status': response.status_code,
                        }
                        state.save()
            return response
        finally:
            finished.set()
    request.finished = finished
    return request


def receipt_matches(record):
    prepared = record.get('prepared_target') or {}
    receipt = record.get('transfer_receipt') or {}
    return bool(prepared.get('target_video_id')
        and receipt.get('target_video_id') == prepared['target_video_id']
        and receipt.get('size_bytes', 0) > 0
        and receipt.get('size_bytes') == prepared.get('size_bytes') == receipt.get('source_bytes_read')
        and receipt.get('http_status') in (200, 201))
