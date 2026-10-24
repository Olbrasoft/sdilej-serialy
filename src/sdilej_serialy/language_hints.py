"""Filename hints guide discovery only; speech verification remains mandatory."""
import re
import unicodedata

from sdilej_to_prehrajto.sdilej import audio_language_hint as sdk_audio_hint

_CODES = r'cz|cs|cze|czech|cesky|sk|svk|slovak|slovensky'
_SUBTITLE = r'tit(?:ulky)?|subs?|subtitles?'


def _words(title):
    text = ''.join(c for c in unicodedata.normalize('NFKD', (title or '').casefold())
                   if not unicodedata.combining(c))
    text = re.sub(r'[^a-z0-9]+', ' ', text).strip()
    return re.sub(rf'\b({_CODES})({_SUBTITLE}|dabing|dubbing|dab)\b', r'\1 \2', text)


def audio_language_hint(title):
    words = _words(title)
    # CZ-SK tit labels two subtitle languages, not two dubbed audio tracks.
    words = re.sub(rf'\b(?:{_CODES})(?:\s+(?:{_CODES}))*\s+(?:{_SUBTITLE})\b', ' ', words)
    words = re.sub(r'\bcs\b', 'cz', words)
    words = re.sub(r'\bslovensky\b', 'sk', words)
    return sdk_audio_hint(words)


def search_hint_rank(title):
    hint = audio_language_hint(title)
    if hint == 'cs':
        return 0
    if hint == 'sk':
        return 1
    words = _words(title)
    if (re.search(rf'\b(?:{_CODES})(?:\s+(?:{_CODES}))*\s+(?:{_SUBTITLE})\b', words)
            or re.search(r'\b(?:eng|english|en|jpn|japanese|ger|german)(?:\d+p)?\b', words)):
        return 2
    return 1
