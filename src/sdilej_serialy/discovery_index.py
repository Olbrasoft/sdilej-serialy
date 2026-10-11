"""Positive search evidence is a dispatch hint, never upload authorization."""
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

from sdilej_to_prehrajto.models import MatchTier
from sdilej_to_prehrajto.sdilej import audio_language_hint

from .episodes import EPISODE_CODE_RE, episode_match, normalize, series_aliases
from .models import Episode


class CandidateIndex:
    def __init__(self, catalog):
        self.aliases = defaultdict(list)
        self.hints = {}
        self.seen = set()
        for metadata in catalog:
            episode = Episode.from_dict(metadata)
            for alias in {normalize(a) for a in series_aliases(episode)}:
                self.aliases[alias, episode.season, episode.number].append(episode)

    def observe(self, candidates):
        for row in candidates:
            marker = (row['source_id'], row['title'])
            if marker in self.seen:
                continue
            self.seen.add(marker)
            matches = list(EPISODE_CODE_RE.finditer(row['title']))
            if len(matches) != 1:
                continue
            match = matches[0]
            code = int(match['season'] or match['sx']), int(match['episode'] or match['ex'])
            prefix = normalize(row['title'][:match.start()])
            words = re.sub(r'\b(?:19|20)\d{2}\b', ' ', prefix).split()
            # Match only known alias prefixes, then apply the exact existing
            # sequel/range/episode guards. Avoid comparing every result with
            # thousands of unrelated S01E01 catalog rows.
            for end in range(1, len(words) + 1):
                for episode in self.aliases.get((' '.join(words[:end]), *code), ()):
                    if episode_match(episode, row['title'])[0] not in (MatchTier.STRONG, MatchTier.SOLID):
                        continue
                    hint = audio_language_hint(row['title'])
                    rank = 0 if hint == 'cs' else 1 if not hint else 2
                    self.hints[episode.identity] = min(rank, self.hints.get(episode.identity, 3))

    def rank(self, identity):
        return self.hints.get(identity, 3)


def indexed_order(catalog, fresh_ids, paused, index, refresh, progress, *, clock=time.monotonic):
    """Six positive hints, two new-series probes, two due retries per round.

    All lanes share one pending set. The exploration cursor survives restarts;
    publishing/selection still belongs to the existing single producer.
    """
    pending = {Episode.from_dict(row).identity: row for row in catalog}
    positions = {identity: n for n, identity in enumerate(pending)}
    groups = defaultdict(deque)
    series_orders = {}
    retries = deque()
    for identity, row in pending.items():
        if identity in fresh_ids:
            groups[row['series_id']].append(identity)
            series_orders[row['series_id']] = (row.get('imdb_rating') is None, -(row.get('imdb_rating') or 0),
                                              -(row.get('imdb_votes') or 0), row['series_id'])
        else:
            retries.append(identity)
    series = list(groups)
    cursor = progress.get('last_series')
    if cursor in series:
        position = series.index(cursor) + 1
        series = series[position:] + series[:position]
    elif progress.get('last_series_order'):
        # The last probed series may now have no eligible rows. Do not reset
        # exploration to the start just because its final episode was consumed.
        anchor = tuple(progress['last_series_order'])
        series = ([s for s in series if series_orders[s] > anchor]
                  + [s for s in series if series_orders[s] <= anchor])
    series = deque(series)
    hinted = deque()
    refreshed = float('-inf')

    def take(lane):
        while lane:
            identity = lane.popleft()
            row = pending.get(identity)
            if row is not None and row['series_id'] not in paused:
                del pending[identity]
                return row

    def explore():
        while series:
            series_id = series.popleft()
            row = take(groups[series_id])
            if groups[series_id]:
                series.append(series_id)
            if row is not None:
                progress['last_series'] = series_id
                progress['last_series_order'] = list(series_orders[series_id])
                return row

    while pending:
        if clock() - refreshed >= 60:
            refresh()
            hinted = deque(sorted((i for i in pending if index.rank(i) < 2),
                                  key=lambda i: (index.rank(i), positions[i])))
            refreshed = clock()
        emitted = hinted_count = 0
        for _ in range(6):
            row = take(hinted)
            if row is None:
                break
            emitted += 1
            hinted_count += 1
            yield row
        # Unused hint slots go to exploration, not more repeated old failures.
        for _ in range(8 - hinted_count):
            row = explore()
            if row is None:
                break
            emitted += 1
            yield row
        for _ in range(2):
            row = take(retries)
            if row is None:
                break
            emitted += 1
            yield row
        if not emitted:
            return


def paused_series(records, now):
    return {int(key) for key, value in records.items()
            if value.get('retry_after') and datetime.fromisoformat(value['retry_after']) > now}


def record_series_result(records, episode, result, now):
    """A bounded scheduling cooldown, never a verdict on unsearched episodes."""
    key = str(episode.series_id)
    previous = records.get(key, {})
    if result['status'] == 'prepared':
        records[key] = dict(misses=[], updated_at=now.isoformat())
        return
    # A completed pause gives the series a new small probe budget.
    expired = (previous.get('retry_after')
               and datetime.fromisoformat(previous['retry_after']) <= now)
    misses = [] if expired else list(previous.get('misses', []))
    if episode.identity not in misses:
        misses.append(episode.identity)
    record = dict(misses=misses[-3:], updated_at=now.isoformat())
    if len(misses) >= 3:
        minutes = 240 if result.get('reason') in ('no_matches', 'verified_non_czech', 'no_acceptable_original') else 30
        record['retry_after'] = (now + timedelta(minutes=minutes)).isoformat()
    records[key] = record
