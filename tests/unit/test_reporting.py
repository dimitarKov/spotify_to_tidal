# tests/unit/test_reporting.py

import asyncio
import re
import sys
import pytest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List
from pytest_mock import MockerFixture

from conftest import RecordingReporter
from spotify_to_tidal import __main__ as cli
from spotify_to_tidal.errors import SpotifyAuthError
from spotify_to_tidal.progress import gather_with_progress
from spotify_to_tidal.reporting import ConsoleReporter, Reporter, get_reporter, use_reporter


def test_gather_with_progress_keeps_order_and_reports_progress(reporter: RecordingReporter):
    async def value_after(value: int, delay: float) -> int:
        await asyncio.sleep(delay)
        return value

    async def main() -> List[int]:
        # finish in reverse order
        return await gather_with_progress([value_after(i, (3 - i) / 100) for i in range(3)], desc="Counting")

    assert asyncio.run(main()) == [0, 1, 2]
    assert reporter.progress_summary() == [("Counting", 3, 3)]


def test_concurrent_jobs_use_their_own_reporter():
    """ e.g. two users' syncs running at the same time in a web server """
    def job(name: str) -> RecordingReporter:
        recording = RecordingReporter()

        async def sync():
            get_reporter().info(f"{name} in task")
            await asyncio.sleep(0.01)
            await asyncio.to_thread(lambda: get_reporter().info(f"{name} in thread"))

        with use_reporter(recording):
            asyncio.run(sync())
        return recording

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(job, ["first", "second"])
    assert first.infos == ["first in task", "first in thread"]
    assert second.infos == ["second in task", "second in thread"]


def test_use_reporter_restores_previous_reporter():
    before = get_reporter()
    with use_reporter(Reporter()):
        assert get_reporter() is not before
    assert get_reporter() is before


def test_console_reporter_is_the_default(capsys: pytest.CaptureFixture[str]):
    assert isinstance(get_reporter(), ConsoleReporter)
    get_reporter().info("hello")
    get_reporter().warning("careful")
    out = capsys.readouterr().out
    assert "hello\n" in out
    assert "\033[91mcareful\033[0m\n" in out  # warnings in red


def test_base_reporter_ignores_everything(capsys: pytest.CaptureFixture[str]):
    reporter = Reporter()
    reporter.info("hello")
    reporter.warning("careful")
    with reporter.progress("work", total=2) as progress:
        progress.update(2)
    assert capsys.readouterr().out == ""


LIBRARY = Path(__file__).parents[2] / "src" / "spotify_to_tidal"
FORBIDDEN = re.compile(r"^\s*(print\(|sys\.exit\(|from tqdm|import tqdm)", re.MULTILINE)

@pytest.mark.parametrize("module", sorted(p.name for p in LIBRARY.glob("*.py") if p.name not in ("reporting.py", "__main__.py")))
def test_library_code_does_not_print_exit_or_use_tqdm(module: str):
    """ Library code reports through get_reporter() and raises SyncError, so it can run under a UI """
    assert FORBIDDEN.findall((LIBRARY / module).read_text(encoding="utf-8")) == []


def test_cli_exits_with_sync_error_message(mocker: MockerFixture):
    def failing_run(args: object):
        raise SpotifyAuthError("could not log in") from ValueError("bad token")

    mocker.patch.object(cli, "run", failing_run)
    mocker.patch.object(sys, "argv", ["spotify_to_tidal"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == "Error: could not log in\n  caused by: ValueError('bad token')"
