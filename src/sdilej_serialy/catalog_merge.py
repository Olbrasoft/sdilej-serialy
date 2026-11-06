"""Add previously unseen series from a read-only public-metadata export."""
import argparse
import json
from pathlib import Path

from .catalog import load_jsonl, prepare_episodes, write_jsonl_gzip
from .catalog_repair import SERIES_FIELDS
from .episodes import normalize
from .pipeline import atomic_json


FIELDS = set(SERIES_FIELDS) | {'episode_id', 'season', 'episode', 'episode_title',
    'episode_name', 'runtime_min', 'description', 'series_tmdb_id'}


def merge(catalog, registry, incoming):
    known = {r['series_id'] for r in catalog}
    names = {normalize(r['series_title']) for r in catalog}
    mappings = {r['series_id']: r['tmdb_id'] for r in registry['series']}
    owners = {mid: sid for sid, mid in mappings.items()}
    episode_ids = {r['episode_id'] for r in catalog}
    clean = [{k: v for k, v in r.items() if k in FIELDS} for r in incoming]
    additions, skipped, accepted = [], set(), set()
    for r in sorted(prepare_episodes(clean), key=lambda r:(r['series_id'],r['season'],r['episode'])):
        sid, mid = r['series_id'], r.get('series_tmdb_id')
        if sid in known:
            continue
        name = normalize(r['series_title'])
        if sid not in accepted:
            if not name or name in names or mid is not None and mid in owners:
                skipped.add(sid)
                continue
            accepted.add(sid)
            names.add(name)
            if mid is not None:
                mappings[sid] = mid
                owners[mid] = sid
        if r['episode_id'] in episode_ids:
            raise ValueError('Incoming episode ID conflicts with preserved catalog')
        episode_ids.add(r['episode_id'])
        additions.append(r)
    updated = dict(registry, series=[dict(series_id=sid,tmdb_id=mid) for sid,mid in sorted(mappings.items())])
    report = dict(added_series=len(accepted), added_episodes=len(additions),
                  skipped_alias_series=sorted(skipped), existing_episodes_preserved=len(catalog))
    return catalog + additions, updated, report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--incoming',type=Path,required=True)
    parser.add_argument('--root',type=Path,default=Path.cwd())
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    catalog_path=args.root/'backlog/series-episodes.jsonl.gz'
    registry_path=args.root/'backlog/series-inventory.json'
    catalog,registry,report=merge(load_jsonl(catalog_path),json.loads(registry_path.read_text()),
                                json.loads(args.incoming.read_text()))
    if args.apply:
        write_jsonl_gzip(catalog_path,catalog)
        atomic_json(registry_path,registry)
        atomic_json(args.root/'reports/catalog-import.json',report)
    print(json.dumps(report))


if __name__=='__main__':
    main()
