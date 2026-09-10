from dataclasses import replace
from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier
from sdilej_serialy.quality import quality_acceptable, rank_candidates


def test_complete_full_hd_beats_sd_without_arbitrary_bitrate_floor():
    hd = Candidate(source_id='33882527', url='https://sdilej.cz/33882527/video',title='S04E10',
        size_bytes=865831506,duration_sec=2535,width=1920,height=1080,video_codec='h264',
        language_tier=LanguageTier.CZECH_AUDIO,match_tier=MatchTier.STRONG)
    sd = replace(hd,source_id='6748802',width=854,height=480,size_bytes=419000000)
    assert quality_acceptable(hd)
    assert rank_candidates([sd,hd])[0] == hd
    compact = replace(hd,size_bytes=700000000)
    assert quality_acceptable(compact)
    assert rank_candidates([sd,hd,compact])[0] == compact


def test_compact_czech_animation_720p_beats_larger_sd():
    hd = Candidate(source_id='33102532', url='https://sdilej.cz/33102532/video', title='Avatar S01E09',
                   width=900, height=720, size_bytes=260460752, duration_sec=1366,
                   video_codec='mpeg4', language_tier=LanguageTier.CZECH_AUDIO, match_tier=MatchTier.STRONG)
    larger_hd = replace(hd, source_id='30690903', size_bytes=374145804, video_codec='h264')
    sd = replace(hd, source_id='30230962', width=720, height=404, size_bytes=390675346)
    assert rank_candidates([sd,larger_hd,hd])[0] == hd


def test_incomplete_original_metadata_is_rejected():
    item = Candidate(source_id='1', url='x', title='x', width=1920, height=1080,
                     duration_sec=100, size_bytes=0)
    assert not quality_acceptable(item)
