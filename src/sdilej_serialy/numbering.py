"""Explicit release-numbering aliases; never guess seasons from bare numbers."""
import re


def mapped_episode_title(episode, title, normalize):
    # Planet Earth II is season 1 in the catalog, but releases also use the
    # parent franchise's season 2 or just II-02. Require the exact sequel
    # identity, episode number, and a known subtitle to avoid other series.
    if normalize(episode.series_original_title or '') != 'planet earth ii' or episode.season != 1:
        return None
    subtitles = {
        1: ('ostrovy', 'islands'), 2: ('pohori', 'hory', 'mountains'),
        3: ('dzungle', 'jungles'), 4: ('pouste', 'deserts'),
        5: ('plane', 'grasslands'), 6: ('mesta', 'cities'),
    }
    value = normalize(title)
    if not any(re.search(rf'\b{word}\b', value) for word in subtitles.get(episode.number, ())):
        return None
    n = rf'0*{episode.number}(?!\d)'
    subtitle = '(?:' + '|'.join(subtitles.get(episode.number, ())) + r')\b'
    # Full sequel title followed by a bare episode number (not SxxExx).
    bare = re.match(rf'^(?:zazracna planeta ii|planet earth ii)(?: 2016)? (?:e)?{n} {subtitle}', value)
    franchise = re.match(rf'^(?:planet earth|zazracna planeta) (?:s0?2e{n}|2 {n}) {subtitle}', value)
    sequel = re.search(r'\b(?:planet earth ii|zazracna planeta ii)\b', value)
    if bare or (franchise and sequel):
        return f'{episode.series_title} {episode.code}'
    return None
