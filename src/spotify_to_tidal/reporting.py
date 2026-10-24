""" Status messages and progress from the sync, decoupled from where they are shown.

    Library code calls get_reporter().info(...) / .warning(...) / .progress(...) instead of print/tqdm.
    The CLI uses ConsoleReporter (the default); a UI installs its own Reporter for a sync job with use_reporter().
    The current reporter is held in a ContextVar, so concurrent sync jobs each see their own; asyncio tasks
    and asyncio.to_thread carry it along automatically """

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Generator, Protocol
from tqdm import tqdm


class Progress(Protocol):
    def update(self, n: int = 1) -> object: ...


class _NoProgress:
    def update(self, n: int = 1) -> None:
        pass


class Reporter:
    """ Receives status messages and progress from the sync. This base class ignores everything """

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    @contextmanager
    def progress(self, description: str, total: int | None = None) -> Generator[Progress, None, None]:
        """ A unit of work with `total` steps; call update() on the result as steps complete """
        yield _NoProgress()


class ConsoleReporter(Reporter):
    """ Prints to the terminal, with warnings in red and tqdm progress bars """

    _RED, _RESET = '\033[91m', '\033[0m'

    def info(self, message: str) -> None:
        print(message)

    def warning(self, message: str) -> None:
        print(self._RED + message + self._RESET)

    @contextmanager
    def progress(self, description: str, total: int | None = None) -> Generator[Progress, None, None]:
        with tqdm(desc=description, total=total) as bar:
            yield bar


_current_reporter: ContextVar[Reporter] = ContextVar('reporter', default=ConsoleReporter())

def get_reporter() -> Reporter:
    return _current_reporter.get()

@contextmanager
def use_reporter(reporter: Reporter) -> Generator[Reporter, None, None]:
    """ Sends the sync's messages and progress to `reporter` within this block (and tasks/threads started from it) """
    token = _current_reporter.set(reporter)
    try:
        yield reporter
    finally:
        _current_reporter.reset(token)
