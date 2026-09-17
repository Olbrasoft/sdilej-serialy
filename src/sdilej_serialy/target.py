"""Target lookup independent of episode subtitles and filename extensions."""
import re
import unicodedata
from urllib.parse import parse_qs, urlparse
from bs4 import BeautifulSoup
from sdilej_to_prehrajto import prehrajto


def episode_key(name):
    match = re.match(r'^(.*?)\s+S(\d+)E(\d+)(?=\D|$)', name, re.I)
    if not match:
        return None
    title = ' '.join(unicodedata.normalize('NFKC', match[1]).casefold().split())
    return f'{title}:s{int(match[2])}:e{int(match[3])}'


def listing_rows(html):
    soup = BeautifulSoup(html, 'html.parser')
    rows = {}
    for link in soup.select('a[href*="do=uploadedVideoListing-deleteVideo"]'):
        href = link['href']
        video_id = parse_qs(urlparse(href).query)['uploadedVideoListing-videoId'][0]
        node = link.parent
        while node is not None:
            headings = node.select('h2,h3')
            if headings:
                ids = {parse_qs(urlparse(a['href']).query)['uploadedVideoListing-videoId'][0]
                       for a in node.select('a[href*="do=uploadedVideoListing-deleteVideo"]')}
                if ids != {video_id}:
                    raise RuntimeError('Ambiguous target listing row')
                name = headings[0].get_text(' ', strip=True)
                rows[video_id] = dict(id=video_id, name=name, key=episode_key(name),
                                      delete_url=href, processing='(zpracovává se)' in name.casefold())
                break
            node = node.parent
        if video_id not in rows:
            raise RuntimeError('Target listing row not recognized')
    return list(rows.values())


def existing_episode(session, name, known_id=None):
    match = re.match(r'^(.*?\s+S\d+E\d+)', name, re.I)
    if not match:
        raise RuntimeError('Missing episode code in target name')
    if known_id is not None:
        if not str(known_id).isdigit():
            raise RuntimeError('Invalid persisted target ID')
        # Videos moved out of the current listing can still be resolved by
        # their durable target ID. This GET only reads the folder-edit page;
        # it never submits changes. Require the matching episode heading.
        detail = session.get(prehrajto.BASE_URL + '/profil/uprava-slozky-videa',
                             params={'videoId': str(known_id)}, timeout=30)
        if detail.status_code != 404:
            detail.raise_for_status()
            soup = BeautifulSoup(detail.text, 'html.parser')
            heading = soup.select_one('h1')
            if heading and 'Změna složky videa' in heading.get_text(' ', strip=True):
                if any(episode_key(h.get_text(' ', strip=True)) == episode_key(name)
                       for h in soup.select('h2')):
                    return str(known_id)
    response = session.get(prehrajto.BASE_URL + '/profil/nahrana-videa',
                           params={'searchPhrase': match[1]}, timeout=30)
    response.raise_for_status()
    if 'uploadedVideoListing' not in response.text:
        raise RuntimeError('Target listing unavailable; refusing new upload')
    matches = [r for r in listing_rows(response.text) if r['key'] == episode_key(name)]
    matches.sort(key=lambda r: (r['processing'], int(r['id'])))
    return matches[0]['id'] if matches else None
