from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, TypeVar, cast
from urllib.parse import urlparse

if TYPE_CHECKING:  # pragma: no cover
    from broadcaster.backends.base import BroadcastBackend

ReturnTypeT = TypeVar("ReturnTypeT")


class Event:
    def __init__(self, channel: str, message: str | bytes) -> None:
        self.channel = channel
        self.message = message
        self.parsed_message: dict[Callable[[str | bytes], Any], Any] = {}

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Event) and self.channel == other.channel and self.message == other.message

    def __repr__(self) -> str:
        return f"Event(channel={self.channel!r}, message={self.message!r})"

    def parsed(self, parser: Callable[[str | bytes], ReturnTypeT]) -> ReturnTypeT:
        """Parse the message with the given parser callable.
        Caches the result so that parsing is only done, usefull if many subsrcibers exist.
        """
        if parser not in self.parsed_message:
            parsed_item = parser(self.message)
            self.parsed_message[parser] = parsed_item
            return parsed_item
        return self.parsed_message[parser]  # type: ignore[no-any-return]


class Unsubscribed(Exception):
    pass


class Broadcast:
    def __init__(
        self,
        url: str | None = None,
        *,
        backend: BroadcastBackend | None = None,
        serializer: Callable[[Any], bytes | str] | None = None,
    ) -> None:
        assert url or backend, "Either `url` or `backend` must be provided."
        self._backend = backend or self._create_backend(cast(str, url))
        self._subscribers: dict[str, set[asyncio.Queue[Event | None]]] = {}
        self.serializer = serializer

    def _create_backend(self, url: str) -> BroadcastBackend:
        parsed_url = urlparse(url)
        if parsed_url.scheme in ("redis", "rediss"):
            from broadcaster.backends.redis import RedisBackend

            return RedisBackend(url)

        elif parsed_url.scheme == "redis-stream":
            from broadcaster.backends.redis import RedisStreamBackend

            return RedisStreamBackend(url)

        elif parsed_url.scheme in ("postgres", "postgresql"):
            from broadcaster.backends.postgres import PostgresBackend

            return PostgresBackend(url)

        if parsed_url.scheme == "kafka":
            from broadcaster.backends.kafka import KafkaBackend

            return KafkaBackend(url)

        elif parsed_url.scheme == "memory":
            from broadcaster.backends.memory import MemoryBackend

            return MemoryBackend(url)
        raise ValueError(f"Unsupported backend: {parsed_url.scheme}")

    async def __aenter__(self) -> Broadcast:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any, **kwargs: Any) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        await self._backend.connect()
        self._listener_task = asyncio.create_task(self._listener())

    async def disconnect(self) -> None:
        if self._listener_task.done():
            self._listener_task.result()
        else:
            self._listener_task.cancel()
        await self._backend.disconnect()

    async def _listener(self) -> None:
        while True:
            event = await self._backend.next_published()
            for queue in list(self._subscribers.get(event.channel, [])):
                await queue.put(event)

    async def publish(self, channel: str, message: Any) -> None:
        if self.serializer:
            message = self.serializer(message)
        await self._backend.publish(channel, message)

    @asynccontextmanager
    async def subscribe(self, channel: str | Sequence[str]) -> AsyncIterator[Subscriber]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        channels = [channel] if isinstance(channel, str) else channel
        try:
            for channel in channels:
                if not self._subscribers.get(channel):
                    self._subscribers[channel] = {queue}
                    await self._backend.subscribe(channel)
                else:
                    self._subscribers[channel].add(queue)

            yield Subscriber(queue)
        finally:
            for channel in channels:
                self._subscribers[channel].remove(queue)
                if not self._subscribers.get(channel):
                    del self._subscribers[channel]
                    await self._backend.unsubscribe(channel)
                await queue.put(None)


class Subscriber:
    def __init__(self, queue: asyncio.Queue[Event | None]) -> None:
        self._queue = queue

    async def __aiter__(self) -> AsyncIterator[Event]:
        try:
            while True:
                yield await self.get()
        except Unsubscribed:
            pass

    async def get(self) -> Event:
        item = await self._queue.get()
        if item is None:
            raise Unsubscribed()
        return item
