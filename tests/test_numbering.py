import pytest
from sdilej_serialy.episodes import episode_match
from sdilej_serialy.models import Episode
from sdilej_to_prehrajto.models import MatchTier


def planet(number=2):
    return Episode(1,5964,'Zázračná planeta II','Planet Earth II',1,number)


@pytest.mark.parametrize('title',[
    'Zázračná planeta II - 02 Pohoří.mkv',
    'Planet Earth S2E02 Pohoří (Zázračná planeta II 2016) 720p AC3 Cz.mkv',
    'Zázračná planeta 2-02 Pohoří (Planet Earth II 2016) 720p AC3 Cz.mkv',
])
def test_verified_planet_numbering_aliases(title):
    tier,evidence=episode_match(planet(),title)
    assert tier==MatchTier.STRONG
    assert evidence['numbering_alias']==title


@pytest.mark.parametrize('title',[
    'Zázračná planeta II - 05 Pláně.mkv',
    'Zázračná planeta II - 02 Džungle.mkv',
    'Zázračná planeta II - 02 03 Pohoří.mkv',
    'Planet Earth S2E02 Pohoří.mkv',
    'Planet Earth S2E02 Pohoří (Planet Earth II) S2E03.mkv',
    'Zázračná planeta - 2-02 Sněhová koule (Miracle Planet II 2005).mkv',
])
def test_aliases_do_not_accept_wrong_series_or_episodes(title):
    assert episode_match(planet(),title)[0]==MatchTier.REJECT


def test_bare_numbers_are_not_guessed_for_other_series():
    item=Episode(1,3,'Test','Test',1,2)
    assert episode_match(item,'Test - 02 Hory.mkv')[0]==MatchTier.REJECT


def test_sequel_alias_does_not_match_original_series():
    item=Episode(1,3,'Zázračná planeta','Planet Earth',1,2)
    assert episode_match(item,'Zázračná planeta II - 02 Pohoří.mkv')[0]==MatchTier.REJECT
