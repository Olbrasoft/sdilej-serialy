"""Read the original media URL from an authenticated detail page."""
import mimetypes
import hashlib
import re
from dataclasses import replace
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from sdilej_to_prehrajto import sdilej


# Episode originals cannot be tiny error/login documents returned with HTTP 200.
MIN_ORIGINAL_BYTES = 1024 * 1024


def request_time_modified(headers):
    """Some download handlers emit request time as Last-Modified on every GET."""
    try:
        modified = parsedate_to_datetime(headers['Last-Modified'])
        served = parsedate_to_datetime(headers['Date'])
        return abs((served - modified).total_seconds()) <= 5
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def sampled_content_fingerprint(session, candidate):
    """Bounded content evidence when the endpoint has no stable HTTP validator.

    Hash three 64-KiB ranges, including the middle rather than only common title
    sequences. Never store samples or signed URLs. Unsupported/failed ranges
    disable cache reuse; they do not prevent a fresh probe/audio verification.
    """
    size, length = candidate.size_bytes, 64 * 1024
    digest = hashlib.sha256()
    for start in (0, size // 2, size - length):
        end = start + length - 1
        try:
            response = session.get(candidate.download_url, headers={
                'Range': f'bytes={start}-{end}', 'Accept-Encoding': 'identity', 'Referer': candidate.url,
            }, stream=True, timeout=(10, 15))
            try:
                if (response.status_code != 206
                        or response.headers.get('Content-Range') != f'bytes {start}-{end}/{size}'):
                    return None
                content = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    content.extend(chunk)
                    if len(content) >= length:
                        break
                if len(content) != length:
                    return None
                digest.update(str(start).encode() + b':' + content)
            finally:
                response.close()
        except requests.RequestException:
            return None
    return 'ranges-v1:' + digest.hexdigest()


def parse_detail_html(html_text, candidate):
    soup = BeautifulSoup(html_text, 'html.parser')
    heading = soup.select_one('h1')
    filename = heading.get_text(' ', strip=True) if heading else candidate.title
    text = soup.get_text(' ', strip=True)
    width, height = sdilej.parse_resolution(text)
    fast_link = next((urljoin(candidate.url, a['href']) for a in soup.select('a[href]')
                      if ' '.join(a.stripped_strings).casefold() == 'stáhnout rychle'), None)
    if not fast_link:
        if 'detail souboru se nepodařilo načíst' in text.casefold():
            raise sdilej.SdilejError('Source detail is temporarily unavailable; no original download link')
        raise sdilej.SdilejError('Authenticated fast download link is unavailable; premium login is required')
    if urlparse(fast_link).scheme not in ('http', 'https'):
        raise sdilej.SdilejError('Fast download link is not an HTTP media address')
    return replace(candidate, title=filename, filename=filename,
                   size_bytes=sdilej.parse_size(text) or candidate.size_bytes,
                   duration_sec=sdilej.parse_duration(text) or candidate.duration_sec,
                   width=width or candidate.width, height=height or candidate.height,
                   video_codec=sdilej.infer_video_codec(filename) or candidate.video_codec,
                   mime_type=mimetypes.guess_type(filename)[0] or 'application/octet-stream',
                   download_url=fast_link, sample_url=fast_link)


def resolve_original(session, candidate, *, evidence=None):
    """Follow the actual download link with login cookies, reading headers only."""
    response = session.get(candidate.download_url, headers={
        'Range': 'bytes=0-0', 'Accept-Encoding': 'identity', 'Referer': candidate.url,
    }, stream=True, timeout=(30, 45))
    try:
        response.raise_for_status()
        content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        if (content_type.startswith('text/') or content_type in ('application/json',
                'application/xml', 'application/xhtml+xml')):
            raise sdilej.SdilejError('Original media endpoint returned a document instead of media')
        size = None
        content_range = re.fullmatch(r'bytes 0-0/(\d+)', response.headers.get('Content-Range', ''))
        if response.status_code == 206 and content_range:
            size = int(content_range.group(1))
        elif response.status_code == 200:
            length = response.headers.get('Content-Length', '')
            if length.isdigit():
                size = int(length)
        if size is None or size < MIN_ORIGINAL_BYTES:
            # Do not replace verified metadata with a short error body, or fall
            # back to rounded detail-page sizes when original length is unknown.
            # SdilejError is retried/deferred before any target is allocated.
            raise sdilej.SdilejError('Original media size is missing or implausibly small')
        if evidence is not None:
            # Used only as input to a hashed cache key, never persisted verbatim.
            evidence.update(etag=response.headers.get('ETag'),
                            last_modified=response.headers.get('Last-Modified'))
            if request_time_modified(response.headers):
                evidence.pop('last_modified')
                etag = evidence.get('etag')
                # A strong ETag is already a representation validator. Without
                # one, require content samples instead of trusting a timestamp
                # that changes for every request to the same immutable file.
                if not etag or etag.startswith('W/'):
                    evidence['needs_content_fingerprint'] = True
        return replace(candidate, download_url=response.url, sample_url=response.url,
                       size_bytes=size)
    finally:
        response.close()
