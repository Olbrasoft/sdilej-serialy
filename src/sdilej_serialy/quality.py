"""Rank verified originals by language, resolution, then file size."""
from sdilej_to_prehrajto.models import LanguageTier, MatchTier
from sdilej_to_prehrajto.ranking import resolution_rank

QUALITY_POLICY = 'original-media-v4'


def above_sd(row):
    selected = row.get('selected', {})
    return resolution_rank(selected.get('width', 0), selected.get('height', 0)) > 1


def upload_eligible(row):
    return (row.get('quality_policy') == QUALITY_POLICY
            and row.get('selected', {}).get('language_tier') == 'czech_audio')


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
