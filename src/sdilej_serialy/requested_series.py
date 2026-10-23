"""Durable, season-scoped sampling for explicitly requested dubbed series."""
import json
from collections import defaultdict
from pathlib import Path

from .models import Episode
from .quality import SLOVAK_POLICY, upload_eligible

PROBE_REVISION = 1


class RequestedSeries:
    def __init__(self, path: Path, generation, records, sources):
        self.records, self.sources = records, sources
        self.series, self.seasons, self.samples = {}, defaultdict(dict), {}
        if not path.exists():
            return
        payload = json.loads(path.read_text())
        if payload.get('schema_version') != 1 or payload.get('generation') != generation:
            raise ValueError('Invalid requested-series plan')
        excluded = {int(row['series_id']) for row in payload.get('excluded_series', [])}
        for row in payload['series']:
            sid = int(row['series_id'])
            if sid in self.series or sid in excluded:
                raise ValueError('Duplicate or already-present requested series')
            self.series[sid] = row
            for item in row['episodes']:
                season, number = int(item['season']), int(item['episode'])
                identity = f'{sid}:{season}:{number}'
                if identity in self.seasons[sid].setdefault(season, []):
                    raise ValueError('Duplicate requested episode')
                self.seasons[sid][season].append(identity)
            for season, identities in self.seasons[sid].items():
                identities.sort(key=lambda value: int(value.split(':')[2]))
                self.samples[sid, season] = identities[:3]

    def decorate(self, metadata):
        sid, season = metadata['series_id'], metadata['season']
        identity = Episode.from_dict(metadata).identity
        if sid not in self.series:
            return metadata
        return dict(metadata, requested_audio_policy=SLOVAK_POLICY,
                    requested_probe=(identity in self.samples.get((sid, season), [])
                                     and not self.positive(sid, season)))

    def checked(self, identity):
        return self.records.get(identity, {}).get('request_probe_revision') == PROBE_REVISION

    def positive(self, sid, season):
        for identity in self.samples[sid, season]:
            row = self.sources.get(identity, {})
            proof = row.get('requested_audio_check', {})
            if (proof.get('revision') == PROBE_REVISION and proof.get('identity') == identity
                    and proof.get('source_id') == row.get('selected', {}).get('source_id')
                    and proof.get('audio_language') in ('cs', 'sk') and upload_eligible(row)):
                return True
        return False

    def available(self, metadata):
        sid, season = metadata['series_id'], metadata['season']
        if sid not in self.series:
            return True
        identity = Episode.from_dict(metadata).identity
        if identity not in self.seasons[sid].get(season, []):
            return False
        first = min((s for s in self.seasons[sid] if s > 0), default=min(self.seasons[sid]))
        if season != first and not (self.positive(sid, first)
                or all(self.checked(i) for i in self.samples[sid, first])):
            return False
        if self.positive(sid, season):
            return True
        probes = self.samples[sid, season]
        pending = next((i for i in probes if not self.checked(i)), None)
        return identity == pending if pending else identity in probes

    def report(self):
        result = []
        for sid, series in self.series.items():
            seasons = []
            for season, identities in sorted(self.seasons[sid].items()):
                probes = self.samples[sid, season]
                checked = [i for i in probes if self.checked(i)]
                foreign = [i for i in checked if self.records[i].get('observed_audio_language')
                           not in (None, 'cs', 'sk')]
                status = ('dubbed_available' if self.positive(sid, season)
                          else 'needs_subtitles' if len(foreign) == len(probes)
                          else 'samples_checked_no_verified_dub' if len(checked) == len(probes)
                          else 'sampling')
                seasons.append(dict(season=season, episodes=len(identities), status=status,
                                    checked_samples=checked, foreign_audio_samples=foreign))
            result.append(dict(series_id=sid, title=series['title'], seasons=seasons))
        return result
