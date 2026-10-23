"""Rank verified originals by language, resolution, then file size."""
from sdilej_to_prehrajto.models import LanguageTier, MatchTier
import re
from urllib.parse import urlsplit

from sdilej_to_prehrajto.ranking import resolution_rank as sdk_resolution_rank

from .models import Episode
from .source_cache import stable_url

QUALITY_POLICY = 'original-media-v4'
SUBTITLE_POLICY = 'requested-english-original-czech-subtitles-v1'
SUBTITLE_SEASONS = frozenset({(581, 4)})


def resolution_rank(width, height=0):
    # Preserve the 4K tier for small encoder crops, including 3832-wide frames.
    if width >= 3800 or height >= 2120:
        return 5
    return sdk_resolution_rank(width, height)


def resolution_label(width, height=0):
    return {5: '4K', 4: '1440p', 3: '1080p', 2: '720p'}.get(resolution_rank(width, height), 'SD')


def subtitle_request(metadata):
    return (metadata.get('source_policy', SUBTITLE_POLICY) == SUBTITLE_POLICY
            and (metadata.get('series_id'), metadata.get('season')) in SUBTITLE_SEASONS)


def subtitled_eligible(row):
    metadata, selected, subtitles = row.get('episode', {}), row.get('selected', {}), row.get('subtitles', {})
    try:
        from .episodes import episode_match, runtime_acceptable
        from sdilej_to_prehrajto.models import Candidate
        episode = Episode.from_dict(metadata)
        candidate = Candidate.from_dict(selected)
        identity_matches = row.get('identity') == episode.identity
        tier, _ = episode_match(episode, candidate.title)
        matching_episode = tier in (MatchTier.STRONG, MatchTier.SOLID) and runtime_acceptable(episode, candidate.duration_sec)
    except (KeyError, TypeError, ValueError):
        return False
    return (row.get('source_policy') == SUBTITLE_POLICY
            and (metadata.get('series_id'), metadata.get('season')) in SUBTITLE_SEASONS
            and identity_matches and matching_episode and selected.get('language_tier') == 'foreign_audio'
            and selected.get('audio_language') == 'en' and (selected.get('language_probability') or 0) >= .65
            and selected.get('match_tier') in ('strong', 'solid')
            and all(isinstance(selected.get(k), (int, float)) and selected[k] > 0
                    for k in ('width', 'height', 'size_bytes', 'duration_sec'))
            and stable_url(selected.get('url', ''))
            and urlsplit(selected['url']).path.split('/')[1:2] == [selected.get('source_id')]
            and subtitles.get('language') == 'cs'
            and subtitles.get('source_id') == selected.get('source_id')
            and subtitles.get('source_url') == selected.get('url')
            and subtitles.get('source_size_bytes') == selected.get('size_bytes')
            and subtitles.get('full') is True and subtitles.get('cue_count', 0) >= 20
            and subtitles.get('end_ms', 0) >= selected['duration_sec'] * 500
            and re.fullmatch('[a-f0-9]{64}', subtitles.get('sha256', '')) is not None)


def above_sd(row):
    selected = row.get('selected', {})
    return resolution_rank(selected.get('width', 0), selected.get('height', 0)) > 1


def upload_eligible(row):
    if row.get('quality_policy') != QUALITY_POLICY:
        return False
    if row.get('source_policy'):
        return subtitled_eligible(row)
    return row.get('selected', {}).get('language_tier') == 'czech_audio'


def quality_acceptable(candidate):
    # A fixed bitrate floor penalizes animation and efficient encodes, and
    # contradicts the requested smallest-file preference within a resolution.
    return (candidate.width > 0 and candidate.height > 0
            and bool(candidate.duration_sec and candidate.duration_sec > 0)
            and bool(candidate.size_bytes and candidate.size_bytes > 0))


def rank_candidates(candidates):
    accepted = [c for c in candidates if c.match_tier in (MatchTier.STRONG, MatchTier.SOLID)
                and c.language_tier != LanguageTier.UNKNOWN and c.width > 0 and quality_acceptable(c)]
    return sorted(accepted, key=lambda c: (int(c.language_tier),
                  -resolution_rank(c.width, c.height), c.size_bytes or 0, c.source_id))
