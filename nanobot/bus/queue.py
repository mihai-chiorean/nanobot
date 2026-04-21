"""Async message queue for decoupled channel-agent communication."""

import asyncio
import logging
from typing import Callable, Awaitable

from nanobot.bus.events import InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    def __init__(self, max_inbound: int = 1000, max_outbound: int = 1000):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=max_inbound)
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=max_outbound)
        self._workers: list[asyncio.Task] = []
        self._running = False

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()

    async def start_workers(
        self,
        n: int,
        handler: Callable[[InboundMessage], Awaitable[None]]
    ) -> None:
        """
        Start N worker tasks that consume from the inbound queue.

        Args:
            n: Number of concurrent workers
            handler: Async function to process each message
        """
        self._running = True
        for i in range(n):
            task = asyncio.create_task(self._worker(i, handler))
            self._workers.append(task)
            logger.info(f"Started worker {i+1}/{n}")

    async def _worker(
        self,
        worker_id: int,
        handler: Callable[[InboundMessage], Awaitable[None]]
    ) -> None:
        """Worker coroutine that continuously processes messages."""
        logger.debug(f"Worker {worker_id} started")
        consecutive_errors = 0
        _MAX_BACKOFF = 30.0  # seconds
        while self._running:
            try:
                msg = await self.inbound.get()
                logger.debug(f"Worker {worker_id} processing message for {msg.session_key}")
                await handler(msg)
                consecutive_errors = 0  # Reset on success
            except asyncio.CancelledError:
                logger.debug(f"Worker {worker_id} cancelled")
                break
            except Exception as e:
                consecutive_errors += 1
                backoff = min(0.1 * (2 ** consecutive_errors), _MAX_BACKOFF)
                logger.error(f"Worker {worker_id} error ({consecutive_errors} consecutive): {e}")
                if consecutive_errors >= 10:
                    logger.critical(
                        f"Worker {worker_id} circuit breaker: {consecutive_errors} "
                        f"consecutive errors, backing off {backoff:.1f}s"
                    )
                await asyncio.sleep(backoff)

    async def stop_workers(self, timeout: float = 5.0) -> None:
        """Gracefully stop all workers."""
        self._running = False

        if not self._workers:
            return

        logger.info(f"Stopping {len(self._workers)} workers...")

        # Cancel remaining workers
        for task in self._workers:
            task.cancel()

        # Wait for graceful shutdown
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._workers, return_exceptions=True),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning(f"Workers did not stop within {timeout}s")

        self._workers.clear()
        logger.info("All workers stopped")
