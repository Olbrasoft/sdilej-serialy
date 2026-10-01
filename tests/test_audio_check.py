from pathlib import Path
import tomllib

import pytest


def test_whisper_dependency_and_workflows_require_compatible_decoder():
    config = tomllib.loads(Path('pyproject.toml').read_text())
    assert 'av>=18.1,<19' in config['project']['optional-dependencies']['whisper']
    for filename in ('prepare-reserve.yml', 'source-audit.yml'):
        workflow = Path('.github/workflows', filename).read_text()
        assert workflow.index('pip install ".[whisper]"') < workflow.index('python -m sdilej_serialy.audio_check')


def test_real_audio_decoder_startup_check():
    pytest.importorskip('faster_whisper')
    from sdilej_serialy.audio_check import check_audio_decoder
    check_audio_decoder()
