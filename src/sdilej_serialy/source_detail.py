"""Read the original media URL from an authenticated detail page."""
import mimetypes
import re
from dataclasses import replace
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from sdilej_to_prehrajto import sdilej


# Episode originals cannot be tiny error/login documents returned with HTTP 200.
MIN_ORIGINAL_BYTES = 1024 * 1024


def parse_detail_html(html_text, candidate):
    soup = BeautifulSoup(html_text, 'html.parser')
    heading = soup.select_one('h1')
    filename = heading.get_text(' ', strip=True) if heading else candidate.title
    text = soup.get_text(' ', strip=True)
    width, height = sdilej.parse_resolution(text)
    fast_link = next((urljoin(candidate.url, a['href']) for a in soup.select('a[href]')
                      if ' '.join(a.stripped_strings).casefold() == 'stáhnout rychle'), None)
    if not fast_link:
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
        return replace(candidate, download_url=response.url, sample_url=response.url,
                       size_bytes=size)
    finally:
        response.close()
