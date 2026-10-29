"""Append missing aired episode metadata without changing upload history or CR."""
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

import requests

from .catalog import load_jsonl, write_jsonl_gzip
from .episodes import normalize

SERIES_FIELDS = (
    'series_id', 'series_title', 'series_original_title', 'series_description',
    'imdb_rating', 'imdb_votes', 'csfd_rating', 'csfd_rating_count',
    'tmdb_rating', 'tmdb_vote_count', 'rating_source', 'rating_value',
    'rating_votes', 'priority_score', 'series_priority_rank',
)


def supplement(catalog, series_id, details, seasons, *, today):
    """Metadata is only a discovery input; original/audio checks still apply."""
    existing = [r for r in catalog if r['series_id'] == series_id]
    if not existing:
        raise ValueError('Series must already exist in the local catalog')
    names = {normalize(r.get(k) or '') for r in existing
             for k in ('series_title', 'series_original_title')} - {''}
    if not names.intersection(normalize(details.get(k) or '') for k in ('name', 'original_name')):
        raise ValueError('TMDB series identity does not match the catalog')
    tmdb_id = int(details['id'])
    known = {(r['series_id'], r['season'], r['episode']) for r in catalog}
    ids = {r['episode_id'] for r in catalog}
    template = {k: existing[0][k] for k in SERIES_FIELDS if k in existing[0]}
    additions = []
    seen = set()
    for season in seasons:
        number = int(season['season_number'])
        if not 1 <= number < 1000 or number in seen:
            raise ValueError('Invalid or duplicate season inventory')
        seen.add(number)
        episode_numbers = set()
        for item in season['episodes']:
            n = int(item['episode_number'])
            if item.get('season_number') != number or not 1 <= n < 1000 or n in episode_numbers:
                raise ValueError('Invalid or duplicate episode inventory')
            episode_numbers.add(n)
            identity = (series_id, number, n)
            if identity in known:
                continue
            aired = item.get('air_date')
            if not aired or date.fromisoformat(aired) > today:
                continue
            # Existing supplementary inventory uses this deterministic local
            # namespace. These identifiers are never written to the CR DB.
            episode_id = -(series_id * 1_000_000 + number * 1000 + n)
            if episode_id in ids:
                raise ValueError('Supplementary episode ID collision')
            runtime = item.get('runtime')
            if runtime is not None and (not isinstance(runtime, int) or runtime <= 0):
                raise ValueError('Invalid episode runtime')
            additions.append(dict(template, episode_id=episode_id, season=number, episode=n,
                episode_title=item.get('name') or None, episode_name=item.get('name') or f'{n}. epizoda',
                runtime_min=runtime, description=item.get('overview') or None,
                tmdb_episode_id=int(item['id']), air_date=aired,
                episode_metadata_source=f'https://www.themoviedb.org/tv/{tmdb_id}/season/{number}/episode/{n}',
                episode_metadata_checked_at=today.isoformat(), episode_code=f'S{number:02d}E{n:02d}'))
            ids.add(episode_id)
            known.add(identity)
    return catalog + sorted(additions, key=lambda r: (r['season'], r['episode']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=Path('backlog/series-episodes.jsonl.gz'))
    parser.add_argument('--series-id', type=int, required=True)
    parser.add_argument('--tmdb-id', type=int, required=True)
    parser.add_argument('--apply', action='store_true', help='Append metadata after validation; default is preview')
    args = parser.parse_args()
    key = os.environ.get('TMDB_API_KEY')
    if not key:
        parser.error('TMDB_API_KEY is required')
    catalog = load_jsonl(args.catalog)
    with requests.Session() as session:
        def get(path):
            try:
                response = session.get('https://api.themoviedb.org/3/' + path,
                                       params={'api_key': key, 'language': 'cs-CZ'}, timeout=30)
            except requests.RequestException:
                raise RuntimeError('TMDB metadata request failed during transport') from None
            if response.status_code != 200:
                # HTTPError includes the secret API key in the request URL.
                raise RuntimeError(f'TMDB metadata request failed: HTTP {response.status_code}')
            return response.json()
        details = get(f'tv/{args.tmdb_id}')
        if details.get('id') != args.tmdb_id:
            raise ValueError('Unexpected TMDB series ID')
        seasons = [get(f'tv/{args.tmdb_id}/season/{s["season_number"]}')
                   for s in details['seasons'] if s['season_number'] > 0]
    result = supplement(catalog, args.series_id, details, seasons, today=date.today())
    if args.apply and len(result) > len(catalog):
        write_jsonl_gzip(args.catalog, result)
    print(json.dumps(dict(series_id=args.series_id, tmdb_id=args.tmdb_id,
        previous_episodes=len(catalog), added_episodes=len(result)-len(catalog), applied=args.apply)))


if __name__ == '__main__':
    main()
