"""Bounded cleanup for subprocess trees created by the local benchmark tools."""

import asyncio
import os
import signal
import subprocess


TASKKILL_TIMEOUT_SECONDS = 5
PROCESS_EXIT_TIMEOUT_SECONDS = 2


def _wait_for_exit(process, timeout):
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return process.poll() is not None
    return True


def _terminate_root(process):
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
        if not _wait_for_exit(process, PROCESS_EXIT_TIMEOUT_SECONDS):
            try:
                process.kill()
            except OSError:
                pass
            _wait_for_exit(process, PROCESS_EXIT_TIMEOUT_SECONDS)


def terminate_owned_process_tree(process, *, process_runner=subprocess.run, platform=None):
    """Stop an owned process and its descendants, without targeting exited PIDs.

    On Windows taskkill receives the live owned root PID and includes its process
    tree. POSIX callers must have started the root in its own process group.
    """
    if process.poll() is not None:
        return False

    platform = os.name if platform is None else platform
    if platform == 'nt':
        try:
            process_runner(
                ['taskkill', '/PID', str(process.pid), '/T', '/F'],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=TASKKILL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            _terminate_root(process)
        return True

    try:
        # The parent watchdog creates the root's process group. A Node child
        # must not call this with its own PID: it inherits the parent's group.
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


async def _wait_for_async_exit(process, timeout):
    if process.returncode is not None:
        return True
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return process.returncode is not None
    return True


async def _terminate_async_root(process):
    if process.returncode is None:
        try:
            process.terminate()
        except OSError:
            pass
        if not await _wait_for_async_exit(process, PROCESS_EXIT_TIMEOUT_SECONDS):
            try:
                process.kill()
            except OSError:
                pass
            await _wait_for_async_exit(process, PROCESS_EXIT_TIMEOUT_SECONDS)


async def _terminate_taskkill_process(taskkill):
    if taskkill.returncode is not None:
        return
    try:
        taskkill.terminate()
    except OSError:
        pass
    if not await _wait_for_async_exit(taskkill, PROCESS_EXIT_TIMEOUT_SECONDS):
        try:
            taskkill.kill()
        except OSError:
            pass
        await _wait_for_async_exit(taskkill, PROCESS_EXIT_TIMEOUT_SECONDS)


async def _run_taskkill(pid, create_subprocess_exec):
    try:
        taskkill = await create_subprocess_exec(
            'taskkill', '/PID', str(pid), '/T', '/F',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False

    try:
        try:
            returncode = await asyncio.wait_for(
                taskkill.wait(), timeout=TASKKILL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await _terminate_taskkill_process(taskkill)
            return False
        return returncode == 0
    except BaseException:
        await _terminate_taskkill_process(taskkill)
        raise


async def terminate_owned_subprocess_tree(process, *, platform=None,
                                          create_subprocess_exec=None):
    """Terminate an owned async subprocess and descendants with bounded waits.

    Windows uses taskkill on the still-live root PID so it can include Node's
    children. POSIX sends SIGTERM to Node first, allowing Playwright's handlers
    to close detached browser processes, then kills Node itself after a grace
    period. It deliberately does not signal Node's inherited process group.
    """
    if process.returncode is not None:
        return False

    platform = os.name if platform is None else platform
    if platform == 'nt':
        create_subprocess_exec = (asyncio.create_subprocess_exec
                                  if create_subprocess_exec is None else create_subprocess_exec)
        # Recheck immediately before taskkill; an already-finished Node process
        # must never cause a PID-only command to target a later owner.
        if process.returncode is None:
            taskkill_succeeded = await _run_taskkill(process.pid, create_subprocess_exec)
        else:
            taskkill_succeeded = False
        if taskkill_succeeded:
            if not await _wait_for_async_exit(process, TASKKILL_TIMEOUT_SECONDS):
                await _terminate_async_root(process)
        else:
            await _terminate_async_root(process)
        return True

    if process.returncode is None:
        try:
            process.terminate()
        except OSError:
            pass
    if not await _wait_for_async_exit(process, PROCESS_EXIT_TIMEOUT_SECONDS):
        if process.returncode is None:
            try:
                process.kill()
            except OSError:
                pass
        await _wait_for_async_exit(process, PROCESS_EXIT_TIMEOUT_SECONDS)
    return True
