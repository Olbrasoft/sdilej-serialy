import pytest

from sdilej_serialy.pipeline import TARGET_EMAIL, target_session


def test_target_account_guard_rejects_any_other_address():
    with pytest.raises(RuntimeError, match=TARGET_EMAIL):
        target_session("wrong@example.test", "irrelevant")
