from __future__ import annotations

import ctypes
import json
import os
import shutil
import struct
import sys
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from astra_runtime import inside

from .document_types import DocumentError, JsonMap, mapping
from .document_pdf_resources import WEB
from .document_profile import FreshProfile
from .model import atomic_write_json


@contextmanager
def owned_process_group(process: subprocess.Popen[str]) -> Iterator[None]:
    if os.name != 'nt':
        raise DocumentError('document_pdf_platform', 'The local PDF worker requires Windows Edge')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel.CreateJobObjectW.restype = ctypes.c_void_p
    kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
    kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # Windows x64 JOBOBJECT_EXTENDED_LIMIT_INFORMATION: kill only this child tree when its owning handle closes.
        limits = ctypes.create_string_buffer(144)
        struct.pack_into('I', limits, 16, 0x2000)
        if not kernel.SetInformationJobObject(handle, 9, limits, ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        child_handle = kernel.OpenProcess(0x0100 | 0x0001, False, process.pid)
        if not child_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.AssignProcessToJobObject(handle, child_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(child_handle)
        try:
            yield
        finally:
            accounting = ctypes.create_string_buffer(48)
            deadline = time.monotonic() + 5
            terminated = False
            while True:
                if not kernel.QueryInformationJobObject(handle, 1, accounting, 48, None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if struct.unpack_from('I', accounting, 40)[0] == 0:
                    break
                if not terminated:
                    if not kernel.TerminateJobObject(handle, 1):
                        raise ctypes.WinError(ctypes.get_last_error())
                    terminated = True
                if time.monotonic() >= deadline:
                    raise DocumentError('document_worker_unsettled', 'Owned browser job did not settle; profile retained')
                time.sleep(.05)
    finally:
        kernel.CloseHandle(handle)


def render_process(job: JsonMap, cancel: threading.Event) -> JsonMap:
    node = shutil.which('node')
    if not node:
        raise DocumentError('document_pdf_dependency', 'Node.js is required for local PDF output', 503)
    owner = inside(Path(str(job['root'])), 'PDF owner')
    started = time.monotonic()
    helper = 'document-formula-images.mjs' if job.get('mode') == 'formula' else 'document-export.mjs'
    with (owner / 'worker.stdout').open('w', encoding='utf-8') as output, (owner / 'worker.stderr').open('w', encoding='utf-8') as errors:
        with subprocess.Popen([node, str(WEB / 'scripts' / helper)], cwd=WEB, stdin=subprocess.PIPE,
                              stdout=output, stderr=errors, text=True, encoding='utf-8',
                              creationflags=subprocess.CREATE_NO_WINDOW) as process:
            try:
                with owned_process_group(process):
                    atomic_write_json(owner / 'worker-resource.json', {'pid': process.pid, 'profile': job['profile'], 'port': job['port'], 'tree': 'owned Windows kill-on-close job'})
                    if process.stdin is None:
                        raise DocumentError('document_pdf_worker', 'Worker input unavailable')
                    process.stdin.write(json.dumps(job) + '\n')
                    process.stdin.flush()
                    sent = False
                    stopped_at = 0.0
                    while process.poll() is None:
                        elapsed = time.monotonic() - started
                        if (cancel.is_set() or elapsed > 90) and not sent:
                            process.stdin.write('cancel\n')
                            process.stdin.flush()
                            sent = True
                            stopped_at = time.monotonic()
                        if sent and time.monotonic() - stopped_at > 25:
                            raise DocumentError('document_pdf_cleanup_timeout', 'PDF cleanup exceeded its bound; owned worker tree stopped', 504)
                        cancel.wait(.05) if not cancel.is_set() else time.sleep(.05)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
    atomic_write_json(owner / 'process-settlement.json', {'ownedJobActiveProcesses': 0, 'nodeExitCode': process.returncode})
    if cancel.is_set():
        raise DocumentError('document_pdf_canceled', 'PDF canceled; previous artifact retained', 409)
    try:
        result = mapping(json.loads((owner / 'worker.stdout').read_text(encoding='utf-8').strip()), 'PDF result')
    except json.JSONDecodeError as exc:
        raise DocumentError('document_pdf_worker_failed', 'PDF worker stopped without a valid receipt', 502) from exc
    if process.returncode != 0 or result.get('status') != 'rendered':
        raise DocumentError('document_pdf_render_failed', str(result.get('error', 'PDF worker failed')), 422)
    if (owner / 'worker.stderr').stat().st_size:
        raise DocumentError('document_pdf_worker_failed', 'PDF worker reported errors', 502)
    return result


def render_fresh_process(job: JsonMap, cancel: threading.Event) -> JsonMap:
    owner = inside(Path(str(job['root'])), 'browser worker owner')
    settlement = owner / 'process-settlement.json'
    if settlement.exists():
        raise DocumentError('document_worker_reused', 'Browser worker stage must be fresh')
    family = 'hwpx' if job.get('mode') == 'formula' else 'pdf'
    profile = FreshProfile(WEB.parents[1] / '.runtime/document-browser' / family)
    result = None
    try:
        atomic_write_json(owner / 'resources.json', {'profile': str(profile.path), 'port': job['port'], 'owner': 'exclusive current document browser job'})
        result = render_process({**job, 'profile': str(profile.path)}, cancel)
        return result
    finally:
        primary = sys.exception()
        try:
            settled = False
            if settlement.is_file():
                settled = mapping(json.loads(settlement.read_bytes()), 'settlement').get('ownedJobActiveProcesses') == 0
            cleanup = profile.cleanup(settled=settled)
            atomic_write_json(owner / 'profile-cleanup.json', cleanup)
            if result is not None:
                result['profileCleanup'] = cleanup
        except (OSError, ValueError) as cleanup_error:
            print(json.dumps({'event': 'document_profile_cleanup_failed', 'build_id': owner.name, 'error': str(cleanup_error)}), file=sys.stderr)
            if primary is None:
                raise
            primary.add_note(f'Profile cleanup diagnostic: {cleanup_error}')
