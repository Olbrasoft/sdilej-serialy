import pytest
from sdilej_serialy.source_access import account_status, check
from types import SimpleNamespace


@pytest.mark.parametrize('label,expected', [('Aktivní','active'),('Neaktivní','inactive'),('','unknown')])
def test_only_explicit_entitlement_status_is_accepted(label, expected):
    html = f'''<nav>Aktivní Neomezené stahování</nav><div class="settings-card">
    <div class="settings-card-header">Neomezené stahování</div>
    <div class="settings-row-status"><span class="status-badge">{label}</span></div></div>'''
    assert account_status(html) == expected


def test_other_account_cards_and_pricing_pages_do_not_prove_entitlement():
    assert account_status('<div class="settings-card"><div class="settings-card-header">Other</div><span class="status-badge">Aktivní</span></div>') == 'unknown'
    assert account_status('<h1>Neomezené stahování</h1>Aktivní') == 'unknown'


def test_login_redirect_cannot_be_accepted_as_an_active_account():
    response = SimpleNamespace(status_code=302, text='Aktivní', raise_for_status=lambda:None)
    assert check(SimpleNamespace(get=lambda *a,**k:response)) == 'unknown'
