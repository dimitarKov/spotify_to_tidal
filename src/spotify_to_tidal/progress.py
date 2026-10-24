from typing import Awaitable, Iterable, List, TypeVar, cast
from tqdm.asyncio import tqdm as atqdm

T = TypeVar('T')

async def gather_with_progress(aws: Iterable[Awaitable[T]], desc: str | None = None) -> List[T]:
    """ asyncio.gather with a tqdm progress bar. tqdm's type stubs lose the result type, so restore it here """
    return cast(List[T], await atqdm.gather(*aws, desc=desc))  # pyright: ignore[reportUnknownMemberType]
