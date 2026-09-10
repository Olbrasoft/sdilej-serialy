"""Read the original media URL from an authenticated detail page."""
import mimetypes
from dataclasses import replace
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from sdilej_to_prehrajto import sdilej


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


def resolve_original(session, candidate):
    """Follow the actual download link with login cookies, reading headers only."""
    response = session.get(candidate.download_url, headers={
        'Range': 'bytes=0-0', 'Accept-Encoding': 'identity', 'Referer': candidate.url,
    }, stream=True, timeout=(30, 45))
    try:
        response.raise_for_status()
        size = None
        total = response.headers.get('Content-Range', '').rsplit('/', 1)[-1]
        if response.status_code == 206 and total.isdigit():
            size = int(total)
        elif response.status_code == 200:
            length = response.headers.get('Content-Length', '')
            if length.isdigit():
                size = int(length)
        return replace(candidate, download_url=response.url, sample_url=response.url,
                       size_bytes=size or candidate.size_bytes)
    finally:
        response.close()
