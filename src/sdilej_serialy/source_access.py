"""Check the authenticated original-download entitlement before discovery."""
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from bs4 import BeautifulSoup
from sdilej_to_prehrajto import sdilej

from .auth import login_with_retry
from .pipeline import atomic_json
from .git_state import GitCheckpointPersister, persist_source_checkpoint
from .resilience import error_evidence


def account_status(html):
    soup = BeautifulSoup(html, 'html.parser')
    for card in soup.select('.settings-card'):
        header = card.select_one('.settings-card-header')
        if not header or header.get_text(' ', strip=True).casefold() != 'neomezené stahování':
            continue
        badge = card.select_one('.settings-row-status .status-badge')
        status = badge.get_text(' ', strip=True).casefold() if badge else ''
        return {'aktivní': 'active', 'neaktivní': 'inactive'}.get(status, 'unknown')
    return 'unknown'


def check(session):
    response = session.get('https://sdilej.cz/nastaveni', allow_redirects=False, timeout=30)
    response.raise_for_status()
    return account_status(response.text) if response.status_code == 200 else 'unknown'


def main():
    root = Path(os.environ.get('GITHUB_WORKSPACE', Path.cwd()))
    report = dict(checked_at=datetime.now(UTC).isoformat(), run_id=os.environ.get('GITHUB_RUN_ID'))
    try:
        with login_with_retry(sdilej.login, os.environ['SDILEJ_EMAIL'], os.environ['SDILEJ_PASSWORD']) as session:
            report['status'] = check(session)
    except Exception as error:
        report.update(status='check_failed', error=error_evidence(error))
    report['available'] = report['status'] == 'active'
    path = root / 'reports/source-access.json'
    atomic_json(path, report)
    persist_source_checkpoint(GitCheckpointPersister(root), path)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f"available={str(report['available']).lower()}\n")
    print(json.dumps(report))
    if not report['available']:
        print('::warning::Original download access is unavailable; check the source account subscription. Episode checks are deferred.')


if __name__ == '__main__':
    main()
