import asyncio
import os
import shutil
import subprocess
import sys
import time

import pytest

from scripts import process_cleanup
from scripts.process_cleanup import (
    terminate_owned_process_tree,
    terminate_owned_subprocess_tree,
)


class FakeSyncProcess:
    pid = 4123

    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.wait_timeouts = []

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.wait_timeouts.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired('owned', timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_windows_sync_cleanup_targets_live_owned_root_tree():
    process = FakeSyncProcess()
    calls = []

    def run_taskkill(command, **options):
        calls.append((command, options))
        assert process.poll() is None
        process.returncode = 0
        return subprocess.CompletedProcess(command, 0)

    assert terminate_owned_process_tree(
        process, process_runner=run_taskkill, platform='nt') is True

    assert calls == [(
        ['taskkill', '/PID', '4123', '/T', '/F'],
        {'check': False, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
         'timeout': process_cleanup.TASKKILL_TIMEOUT_SECONDS},
    )]
    assert process.terminated is False


def test_sync_cleanup_does_not_signal_an_exited_process():
    process = FakeSyncProcess(returncode=0)

    def forbidden_taskkill(*_args, **_kwargs):
        raise AssertionError('An exited PID must not be targeted.')

    assert terminate_owned_process_tree(
        process, process_runner=forbidden_taskkill, platform='nt') is False
    assert process.wait_timeouts == []
    assert process.terminated is False
    assert process.killed is False


def test_windows_sync_cleanup_falls_back_to_the_owned_root_after_taskkill_timeout():
    process = FakeSyncProcess()
    calls = []

    def timed_out_taskkill(command, **options):
        calls.append((command, options))
        raise subprocess.TimeoutExpired(command, options['timeout'])

    assert terminate_owned_process_tree(
        process, process_runner=timed_out_taskkill, platform='nt') is True

    assert calls[0][0] == ['taskkill', '/PID', '4123', '/T', '/F']
    assert calls[0][1]['timeout'] == process_cleanup.TASKKILL_TIMEOUT_SECONDS
    assert process.terminated is True
    assert process.killed is False


def test_posix_sync_cleanup_terminates_only_the_owned_process_group(monkeypatch):
    process = FakeSyncProcess()
    signals = []
    monkeypatch.setattr(process_cleanup.signal, 'SIGKILL', 9, raising=False)

    def killpg(pid, signum):
        signals.append((pid, signum))
        process.returncode = -15

    monkeypatch.setattr(process_cleanup.os, 'killpg', killpg, raising=False)

    assert terminate_owned_process_tree(process, platform='posix') is True
    assert signals == [(process.pid, process_cleanup.signal.SIGKILL)]


def test_async_cleanup_does_not_signal_an_exited_process():
    class ExitedProcess:
        pid = 91
        returncode = 0

        def terminate(self):
            raise AssertionError('An exited process must not be signalled.')

        def kill(self):
            raise AssertionError('An exited process must not be killed.')

        async def wait(self):
            raise AssertionError('An exited process needs no wait.')

    async def forbidden_taskkill(*_args, **_kwargs):
        raise AssertionError('An exited PID must not be targeted.')

    assert asyncio.run(terminate_owned_subprocess_tree(
        ExitedProcess(), platform='nt', create_subprocess_exec=forbidden_taskkill)) is False


def test_windows_async_cleanup_targets_the_live_owned_root_tree():
    class OwnedProcess:
        pid = 9876
        returncode = None

        async def wait(self):
            return self.returncode

        def terminate(self):
            raise AssertionError('Successful tree cleanup should not need root fallback.')

        def kill(self):
            raise AssertionError('Successful tree cleanup should not need root fallback.')

    class TaskkillProcess:
        returncode = None

        async def wait(self):
            owned.returncode = -9
            self.returncode = 0
            return self.returncode

    owned = OwnedProcess()
    calls = []

    async def create_taskkill(*command, **options):
        calls.append((command, options))
        assert owned.returncode is None
        return TaskkillProcess()

    assert asyncio.run(terminate_owned_subprocess_tree(
        owned, platform='nt', create_subprocess_exec=create_taskkill)) is True
    assert calls == [((
        'taskkill', '/PID', '9876', '/T', '/F'),
        {'stdout': asyncio.subprocess.DEVNULL, 'stderr': asyncio.subprocess.DEVNULL},
    )]


def test_windows_async_cleanup_bounds_taskkill_and_falls_back_to_owned_root(monkeypatch):
    class OwnedProcess:
        pid = 9876
        returncode = None

        async def wait(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            raise AssertionError('The owned root should exit after terminate.')

    class HungTaskkill:
        returncode = None

        async def wait(self):
            await asyncio.Event().wait()

        def terminate(self):
            self.returncode = -15

        def kill(self):
            raise AssertionError('The taskkill helper should exit after terminate.')

    owned = OwnedProcess()
    taskkill = HungTaskkill()
    monkeypatch.setattr(process_cleanup, 'TASKKILL_TIMEOUT_SECONDS', 0.01)
    monkeypatch.setattr(process_cleanup, 'PROCESS_EXIT_TIMEOUT_SECONDS', 0.01)

    async def create_taskkill(*_args, **_options):
        return taskkill

    started = time.monotonic()
    assert asyncio.run(terminate_owned_subprocess_tree(
        owned, platform='nt', create_subprocess_exec=create_taskkill)) is True

    assert taskkill.returncode == -15
    assert owned.returncode == -15
    assert time.monotonic() - started < 1


def test_posix_async_cleanup_sends_sigterm_then_kills_only_owned_root_on_timeout(monkeypatch):
    class SlowProcess:
        pid = 9876
        returncode = None

        def __init__(self):
            self.finished = asyncio.Event()
            self.terminated = 0
            self.killed = 0

        async def wait(self):
            await self.finished.wait()
            return self.returncode

        def terminate(self):
            self.terminated += 1

        def kill(self):
            self.killed += 1
            self.returncode = -9
            self.finished.set()

    process = SlowProcess()
    monkeypatch.setattr(process_cleanup, 'PROCESS_EXIT_TIMEOUT_SECONDS', 0.01)
    start = time.monotonic()

    assert asyncio.run(terminate_owned_subprocess_tree(process, platform='posix')) is True

    assert process.terminated == 1
    assert process.killed == 1
    assert time.monotonic() - start < 1


@pytest.mark.skipif(os.name != 'nt' or shutil.which('node') is None,
                    reason='Requires Windows taskkill and Node.js.')
def test_windows_async_cleanup_kills_real_node_and_python_descendant(tmp_path):
    node = shutil.which('node')
    child_pid_file = tmp_path / 'python-child.pid'
    node_script = (
        "const fs=require('fs');const cp=require('child_process');"
        "const child=cp.spawn(process.argv[1],['-c',process.argv[2]],{stdio:'ignore'});"
        "fs.writeFileSync(process.argv[3],String(child.pid));setInterval(()=>{},1000);"
    )

    async def run_probe():
        process = await asyncio.create_subprocess_exec(
            node, '-e', node_script, sys.executable,
            'import time; time.sleep(60)', str(child_pid_file),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        child_pid = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not child_pid_file.exists():
                await asyncio.sleep(0.02)
            assert child_pid_file.exists(), 'Node did not start its owned Python child.'
            child_pid = int(child_pid_file.read_text(encoding='utf-8'))
            assert _windows_process_is_running(child_pid), 'The Python child exited before cleanup.'

            started = time.monotonic()
            await asyncio.wait_for(terminate_owned_subprocess_tree(process), timeout=12)
            elapsed = time.monotonic() - started
            assert process.returncode is not None
            assert elapsed < 10

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _windows_process_is_running(child_pid):
                await asyncio.sleep(0.05)
            assert not _windows_process_is_running(child_pid), (
                'taskkill returned before its owned Python descendant exited.')
        finally:
            if process.returncode is None:
                await terminate_owned_subprocess_tree(process)
            if child_pid is not None and _windows_process_is_running(child_pid):
                subprocess.run(
                    ['taskkill', '/PID', str(child_pid), '/F'], check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)

    asyncio.run(run_probe())


def _windows_process_is_running(pid):
    result = subprocess.run(
        ['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, timeout=3,
    )
    return f'"{pid}"' in result.stdout
