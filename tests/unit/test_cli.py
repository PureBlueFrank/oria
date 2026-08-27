import pytest
from typer.testing import CliRunner

from oria import __version__
from oria.cli import app

pytestmark = pytest.mark.unit


def test_version_option_reports_package_version() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__
