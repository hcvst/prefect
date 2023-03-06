import asyncio
import atexit
import concurrent.futures
import contextvars
import dataclasses
import inspect
import threading
from typing import Any, Callable, Dict, Generic, Optional, Tuple, TypeVar

from typing_extensions import ParamSpec

from prefect._internal.concurrency.event_loop import get_running_loop
from prefect._internal.concurrency.primitives import Event
from prefect.logging import get_logger

T = TypeVar("T")
P = ParamSpec("P")


logger = get_logger("prefect._internal.concurrency.workers")


@dataclasses.dataclass
class Call(Generic[T]):
    """
    A deferred function call.
    """

    future: concurrent.futures.Future
    fn: Callable[..., T]
    args: Tuple
    kwargs: Dict[str, Any]
    context: contextvars.Context

    @classmethod
    def new(cls, __fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> "Call[T]":
        return cls(
            future=concurrent.futures.Future(),
            fn=__fn,
            args=args,
            kwargs=kwargs,
            context=contextvars.copy_context(),
        )

    def run(self) -> None:
        """
        Execute the call and place the result on the future.

        All exceptions during execution of the call are captured.
        """
        # Do not execute if the future is cancelled
        if not self.future.set_running_or_notify_cancel():
            logger.debug("Skipping execution of cancelled call %r", self)
            return

        logger.debug("Running call %r", self)

        coro = self._run_sync()
        if coro is not None:
            loop = get_running_loop()
            if loop:
                # If an event loop is available, return a task to be awaited
                # Note we must create a task for context variables to propagate
                logger.debug(
                    "Executing coroutine for call %r in running loop %r", self, loop
                )
                return self.context.run(loop.create_task, self._run_async(coro))
            else:
                # Otherwise, execute the function here
                logger.debug("Executing coroutine for call %r in new loop", self)
                return self.context.run(asyncio.run, self._run_async(coro))

        return None

    def _run_sync(self):
        try:
            result = self.context.run(self.fn, *self.args, **self.kwargs)

            # Return the coroutine for async execution
            if inspect.isawaitable(result):
                return result

        except BaseException as exc:
            self.future.set_exception(exc)
            logger.debug("Encountered exception in call %r", self)
            # Prevent reference cycle in `exc`
            del self
        else:
            self.future.set_result(result)
            logger.debug("Finished call %r", self)

    async def _run_async(self, coro):
        try:
            result = await coro
        except BaseException as exc:
            logger.debug("Encountered exception %s in async call %r", exc, self)
            self.future.set_exception(exc)
            # Prevent reference cycle in `exc`
            del self
        else:
            self.future.set_result(result)
            logger.debug("Finished async call %r", self)

    def __call__(self) -> T:
        """
        Execute the call and return its result.

        All executions during excecution of the call are re-raised.
        """
        coro = self.run()

        # Return an awaitable if in an async context
        if coro is not None:

            async def run_and_return_result():
                await coro
                return self.future.result()

            return run_and_return_result()
        else:
            return self.future.result()

    def __repr__(self) -> str:
        name = getattr(self.fn, "__name__", str(self.fn))
        call_args = ", ".join(
            [repr(arg) for arg in self.args]
            + [f"{key}={repr(val)}" for key, val in self.kwargs.items()]
        )

        # Enforce a maximum length
        if len(call_args) > 100:
            call_args = call_args[:100] + "..."

        return f"{name}({call_args})"


class Worker:
    """
    A worker running on a thread.

    Runs an event loop and allows submission of calls.
    """

    def __init__(
        self, name: str = "WorkerThread", daemon: bool = False, run_once: bool = False
    ):
        self.thread = threading.Thread(
            name=name, daemon=daemon, target=self._entrypoint
        )
        self._ready_future = concurrent.futures.Future()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown_event: Optional[Event] = None
        self._run_once: bool = run_once
        self._submitted_count: int = 0

        if not daemon:
            atexit.register(self.shutdown)

    def start(self):
        """
        Start the worker; raises any exceptions encountered during startup.
        """
        self.thread.start()
        # Wait for the worker to be ready
        self._ready_future.result()

    def submit(self, call: Call) -> None:
        if self._submitted_count > 0 and self._run_once:
            raise RuntimeError(
                "Worker configured to only run once. A call has already been submitted."
            )

        if not self._shutdown_event:
            self.start()

        if self._shutdown_event.is_set():
            raise RuntimeError("Worker is shutdown.")

        self._loop.call_soon_threadsafe(call.run)
        self._submitted_count += 1
        if self._run_once:
            call.future.add_done_callback(lambda _: self.shutdown())

    def shutdown(self) -> None:
        if not self._shutdown_event:
            return

        self._shutdown_event.set()
        # TODO: Consider blocking on `thread.join` here?

    def _entrypoint(self):
        """
        Entrypoint for the worker.

        Immediately create a new event loop and pass control to `run_until_shutdown`.
        """
        try:
            asyncio.run(self._run_until_shutdown())
        except BaseException:
            # Log exceptions that crash the worker
            logger.exception("Worker encountered exception")
            raise

    async def _run_until_shutdown(self):
        try:
            self._loop = asyncio.get_running_loop()
            self._shutdown_event = Event()
            self._ready_future.set_result(True)
        except Exception as exc:
            self._ready_future.set_exception(exc)
            return

        await self._shutdown_event.wait()


GLOBAL_WORKER: Optional[Worker] = None


def get_global_worker() -> Worker:
    global GLOBAL_WORKER

    # Create a new worker on first call or if the existing worker is dead
    if GLOBAL_WORKER is None or not GLOBAL_WORKER.thread.is_alive():
        GLOBAL_WORKER = Worker(daemon=True, name="GlobalWorkerThread")
        GLOBAL_WORKER.start()

    return GLOBAL_WORKER