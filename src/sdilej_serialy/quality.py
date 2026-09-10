"""Series quality floors with a one-percent boundary tolerance."""
from sdilej_to_prehrajto.models import LanguageTier, MatchTier
from sdilej_to_prehrajto.ranking import minimum_bitrate_mbps, resolution_rank


def quality_acceptable(candidate):
    bitrate = candidate.average_bitrate_mbps
    return bitrate is not None and bitrate >= minimum_bitrate_mbps(candidate) * 0.99


def rank_candidates(candidates):
    accepted = [c for c in candidates if c.match_tier in (MatchTier.STRONG, MatchTier.SOLID)
                and c.language_tier != LanguageTier.UNKNOWN and c.width > 0 and quality_acceptable(c)]
    return sorted(accepted, key=lambda c: (int(c.language_tier),
                  -resolution_rank(c.width, c.height), c.size_bytes or 0, c.source_id))
