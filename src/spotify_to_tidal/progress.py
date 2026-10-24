import asyncio
from typing import Awaitable, Iterable, List, TypeVar

from .reporting import get_reporter

T = TypeVar('T')

async def gather_with_progress(aws: Iterable[Awaitable[T]], desc: str = "") -> List[T]:
    """ asyncio.gather, reporting progress as each awaitable completes. Results keep the input order """
    awaitables = list(aws)
    with get_reporter().progress(desc, total=len(awaitables)) as progress:
        async def _tracked(aw: Awaitable[T]) -> T:
            result = await aw
            progress.update(1)
            return result
        return await asyncio.gather(*[_tracked(aw) for aw in awaitables])
