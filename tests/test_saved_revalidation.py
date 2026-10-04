from dataclasses import replace

import pytest
from sdilej_to_prehrajto.models import Candidate, LanguageTier
from sdilej_to_prehrajto.sdilej import SdilejError

from sdilej_serialy.episodes import EpisodeSourceProvider
from sdilej_serialy.models import Episode
from test_source_audit import row


def provider(monkeypatch):
    instance = EpisodeSourceProvider(None, detector=object())
    monkeypatch.setattr(instance, 'search', lambda _: pytest.fail('Saved review must not search'))
    return instance


def test_saved_review_refreshes_every_original_then_selects_smallest_best_czech(monkeypatch):
    p = provider(monkeypatch)
    base = row(height=1080)
    episode = Episode.from_dict(base['episode'])
    candidate = Candidate.from_dict(base['selected'])
    candidates = [replace(candidate, source_id=str(i), size_bytes=999) for i in range(4)]
    inspected, languages = [], []
    def inspect(ep, c):
        inspected.append(c.source_id)
        return replace(c, width=3840 if c.source_id != '3' else 1920,
                       height=2160 if c.source_id != '3' else 1080,
                       size_bytes=[80, 120, 100, 50][int(c.source_id)])
    def verify(ep, c):
        assert len(inspected) == 4
        languages.append(c.source_id)
        return replace(c, audio_language='en', language_tier=LanguageTier.FOREIGN_AUDIO) if c.source_id == '0' else c
    monkeypatch.setattr(p, '_inspect', inspect)
    monkeypatch.setattr(p, '_verify_language', verify)
    result = p.revalidate_saved(episode, candidates)
    assert result.source_id == '2' and result.size_bytes == 100 and result.height == 2160
    assert languages == ['0', '2']


def test_saved_hd_label_cannot_admit_actual_low_resolution(monkeypatch):
    p = provider(monkeypatch)
    base = row(height=1080)
    monkeypatch.setattr(p, '_inspect', lambda ep, c: replace(c, width=1280, height=720))
    monkeypatch.setattr(p, '_verify_language', lambda *a: pytest.fail('Low resolution needs full discovery'))
    assert p.revalidate_saved(Episode.from_dict(base['episode']), [Candidate.from_dict(base['selected'])]) is None


def test_unavailable_saved_original_defers_without_search(monkeypatch):
    p = provider(monkeypatch)
    base = row(height=1080)
    calls = []
    def inspect(*args):
        calls.append(1)
        raise SdilejError('Unavailable')
    monkeypatch.setattr(p, '_inspect', inspect)
    assert p.revalidate_saved(Episode.from_dict(base['episode']), [Candidate.from_dict(base['selected'])]) is None
    assert len(calls) == 2


def test_saved_review_rechecks_audio_instead_of_trusting_old_language(monkeypatch):
    p = provider(monkeypatch)
    base = row(height=1080)
    monkeypatch.setattr(p, '_inspect', lambda ep, c: c)
    monkeypatch.setattr(p, '_verify_language', lambda ep, c: replace(
        c, audio_language='en', language_tier=LanguageTier.FOREIGN_AUDIO))
    candidate = p.revalidate_saved(Episode.from_dict(base['episode']), [Candidate.from_dict(base['selected'])])
    assert candidate.audio_language == 'en'
