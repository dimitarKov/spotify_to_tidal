import sys
import os
import pytest
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Generator, List, Tuple

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from spotify_to_tidal.reporting import Progress, Reporter, use_reporter


@dataclass
class RecordedProgress:
    description: str
    total: int | None
    done: int = 0

    def update(self, n: int = 1) -> None:
        self.done += n


@dataclass
class RecordingReporter(Reporter):
    """ Records everything the sync reports, for assertions """
    infos: List[str] = field(default_factory=lambda: [])
    warnings: List[str] = field(default_factory=lambda: [])
    progresses: List[RecordedProgress] = field(default_factory=lambda: [])

    def info(self, message: str) -> None:
        self.infos.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    @contextmanager
    def progress(self, description: str, total: int | None = None) -> Generator[Progress, None, None]:
        recorded = RecordedProgress(description, total)
        self.progresses.append(recorded)
        yield recorded

    def progress_summary(self) -> List[Tuple[str, int | None, int]]:
        return [(p.description, p.total, p.done) for p in self.progresses]


@pytest.fixture
def reporter() -> Generator[RecordingReporter, None, None]:
    """ Captures the sync's messages and progress during the test """
    recording = RecordingReporter()
    with use_reporter(recording):
        yield recording
