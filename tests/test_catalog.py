from sdilej_serialy.catalog import prepare_episodes, rating_details


def test_rating_prefers_imdb_and_uses_bayesian_prior():
    source, rating, votes, score = rating_details(
        {"imdb_rating": 9.0, "imdb_votes": 2_500, "csfd_rating": 99}
    )
    assert (source, rating, votes) == ("imdb", 9.0, 2_500)
    assert score == 7.9


def test_episode_rows_are_ranked_by_series_score_then_episode_order():
    rows = prepare_episodes(
        [
            {"series_id": 2, "series_title": "Second", "episode_id": 3, "season": 1, "episode": 2, "tmdb_rating": 7.0, "tmdb_vote_count": 500},
            {"series_id": 1, "series_title": "First", "episode_id": 2, "season": 1, "episode": 2, "imdb_rating": 9.0, "imdb_votes": 5_000},
            {"series_id": 1, "series_title": "First", "episode_id": 1, "season": 1, "episode": 1, "imdb_rating": 9.0, "imdb_votes": 5_000},
        ]
    )
    assert [row["episode_id"] for row in rows] == [1, 2, 3]
    assert [row["series_priority_rank"] for row in rows] == [1, 1, 2]
    assert rows[0]["episode_code"] == "S01E01"
