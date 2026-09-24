"""Immutable, deduplicated replay plans with a separate target generation."""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from .manifest import SourceManifest
from .pipeline import TARGET_EMAIL, atomic_json, now_iso
from .quality import QUALITY_POLICY, upload_eligible
from .target import episode_key


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def recovery_directory(root: Path, generation: str) -> Path:
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', generation):
        raise ValueError('Invalid recovery generation')
    return root / 'recovery' / generation


def prepare_recovery(root: Path, generation: str, history: Path, manifest: Path,
                     history_commit: str) -> dict:
    directory = recovery_directory(root, generation)
    if directory.exists():
        raise ValueError('Recovery generation already exists; never reset its progress')
    history_bytes = history.read_bytes()
    state = json.loads(history_bytes)
    if state.get('schema_version') != 1:
        raise ValueError('Unsupported historical state')
    sources = SourceManifest(manifest).rows
    groups = defaultdict(list)
    for identity, record in state['episodes'].items():
        upload = record.get('upload', {})
        if upload.get('target_video_id'):
            key = episode_key(upload.get('display_name', ''))
            if not key:
                raise ValueError('Historical upload has no semantic episode identity')
            groups[key].append(identity)
    rows, excluded = [], []
    for key, identities in sorted(groups.items()):
        candidates = [sources[i] for i in identities if i in sources
                      and episode_key(sources[i]['display_name']) == key]
        eligible = [r for r in candidates if upload_eligible(r)]
        if eligible:
            from .quality import rank_candidates
            from sdilej_to_prehrajto.models import Candidate
            ranked = rank_candidates([Candidate.from_dict(r['selected']) for r in eligible])
            if not ranked:
                raise ValueError('Verified recovery source has incomplete media evidence')
            best = ranked[0].source_id
            rows.append(sorted([r for r in eligible if r['selected']['source_id'] == best],
                               key=lambda r: r['identity'])[0])
        else:
            reason = ('missing_source' if not candidates else
                      'legacy_czech_policy' if any(r['selected'].get('language_tier') == 'czech_audio'
                                                   for r in candidates) else 'non_czech_audio')
            excluded.append(dict(episode_key=key, identities=identities, reason=reason))
    payload = ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows).encode()
    plan = dict(schema_version=1, generation=generation, created_at=now_iso(),
                target_email=TARGET_EMAIL, quality_policy=QUALITY_POLICY,
                history_commit=history_commit, history_sha256=digest(history_bytes),
                original_manifest_sha256=digest(manifest.read_bytes()),
                manifest_sha256=digest(payload), historical_unique=len(groups),
                selected_count=len(rows), excluded_count=len(excluded))
    directory.mkdir(parents=True)
    (directory / 'history.json.gz').write_bytes(gzip.compress(history_bytes, mtime=0))
    (directory / 'manifest.jsonl').write_bytes(payload)
    atomic_json(directory / 'excluded.json', excluded)
    atomic_json(directory / 'plan.json', plan)
    atomic_json(directory / 'state.json', dict(schema_version=1, generation=generation,
                                             manifest_sha256=plan['manifest_sha256'], episodes={}))
    return plan


def load_recovery(root: Path, generation: str) -> tuple[Path, dict, SourceManifest]:
    directory = recovery_directory(root, generation)
    plan = json.loads((directory / 'plan.json').read_text())
    state = json.loads((directory / 'state.json').read_text())
    if (plan.get('schema_version') != 1 or plan.get('generation') != generation
            or plan.get('target_email') != TARGET_EMAIL or plan.get('quality_policy') != QUALITY_POLICY
            or state.get('generation') != generation
            or state.get('manifest_sha256') != plan.get('manifest_sha256')):
        raise ValueError('Recovery generation, account or policy mismatch')
    if digest((directory / 'manifest.jsonl').read_bytes()) != plan['manifest_sha256']:
        raise ValueError('Recovery source snapshot changed')
    if digest(gzip.decompress((directory / 'history.json.gz').read_bytes())) != plan['history_sha256']:
        raise ValueError('Recovery history snapshot changed')
    manifest = SourceManifest(directory / 'manifest.jsonl')
    keys = [episode_key(r['display_name']) for r in manifest.rows.values()]
    if (len(keys) != plan['selected_count'] or None in keys or len(set(keys)) != len(keys)
            or not all(upload_eligible(r) for r in manifest.rows.values())
            or not set(state['episodes']).issubset(manifest.rows)):
        raise ValueError('Invalid recovery queue or progress')
    return directory, plan, manifest
