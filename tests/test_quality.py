from dataclasses import replace
from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier
from sdilej_serialy.quality import quality_acceptable, rank_candidates


def test_complete_full_hd_near_floor_beats_sd_but_low_bitrate_is_rejected():
    hd = Candidate(source_id='33882527', url='https://sdilej.cz/33882527/video',title='S04E10',
        size_bytes=865831506,duration_sec=2535,width=1920,height=1080,video_codec='h264',
        language_tier=LanguageTier.CZECH_AUDIO,match_tier=MatchTier.STRONG)
    sd = replace(hd,source_id='6748802',width=854,height=480,size_bytes=419000000)
    assert quality_acceptable(hd)
    assert rank_candidates([sd,hd])[0] == hd
    assert not quality_acceptable(replace(hd,size_bytes=700000000))
