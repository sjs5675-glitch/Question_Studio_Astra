"""Bounded private stdio JSON-RPC transport; owns and reaps its native child."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Literal, TextIO

from .contracts import AstraError, Json, JsonMap, MAX_OUTPUT_BYTES, digest, json_object, validated_items


def error_code(error: JsonMap) -> str:
    info = error.get('codexErrorInfo')
    if isinstance(info, dict):
        info = next(iter(info), '')
    explicit = {
        'usageLimitExceeded': 'quota_exhausted', 'rateLimitExceeded': 'rate_limited',
        'serverOverloaded': 'capacity_unavailable', 'unauthorized': 'auth_rejected',
        'httpConnectionFailed': 'transport_error', 'responseStreamConnectionFailed': 'transport_error',
        'responseStreamDisconnected': 'transport_error', 'modelNotFound': 'model_unavailable',
    }
    if isinstance(info, str) and info in explicit:
        return explicit[info]
    message = str(error.get('message') or '').lower()
    if 'model' in message and any(word in message for word in ('not found', 'not available', 'unsupported', 'does not exist')):
        return 'model_unavailable'
    if any(word in message for word in ('unknown field', 'unrecognized', 'invalid config')):
        return 'cli_config_unsupported'
    return 'cli_error'


class Rpc:
    """Mutable transport state belongs to one synchronous request and child."""
    def __init__(self, command: list[str], cwd: Path, env: dict[str, str], timeout: float, cancel: threading.Event | None) -> None:
        self.deadline = time.monotonic() + timeout
        self.cancel = cancel
        self.sequence = 0
        self.request_method = 'unknown'
        self.failure_diagnostic: JsonMap = {}
        self.writers: list[threading.Thread] = []
        self.events: deque[JsonMap] = deque()
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.read_bytes = 0
        self.stderr_code: str | None = None
        self.cleanup: JsonMap = {}
        try:
            self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='strict', bufsize=1, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as exc:
            raise AstraError('cli_start_failed') from exc
        self.readers = [threading.Thread(target=self._read, args=(self.process.stdout, True), daemon=True), threading.Thread(target=self._read, args=(self.process.stderr, False), daemon=True)]
        for reader in self.readers:
            reader.start()

    def _read(self, stream: TextIO, stdout: bool) -> None:
        try:
            while line := stream.readline(MAX_OUTPUT_BYTES + 1):
                if stdout:
                    self.read_bytes += len(line.encode('utf-8'))
                    if self.read_bytes > MAX_OUTPUT_BYTES * 4 or len(line) > MAX_OUTPUT_BYTES:
                        self.lines.put('{"method":"astra/overflow"}')
                        return
                    self.lines.put(line)
                else:
                    classified = error_code({'message': line})
                    if classified != 'cli_error':
                        self.stderr_code = classified
        except (OSError, UnicodeError):
            if stdout:
                self.lines.put('{"method":"astra/malformed"}')
        finally:
            if stdout:
                self.lines.put(None)

    def send(self, value: JsonMap) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise AstraError("cancelled")
        if time.monotonic() >= self.deadline:
            raise AstraError("timeout")
        outcome: queue.Queue[str | None] = queue.Queue()
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n"
        def write() -> None:
            try:
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
            except (OSError, ValueError):
                outcome.put("transport_error")
            else:
                outcome.put(None)
        writer = threading.Thread(target=write, daemon=True)
        self.writers = [thread for thread in self.writers if thread.is_alive()]
        self.writers.append(writer)
        writer.start()
        while True:
            if self.cancel is not None and self.cancel.is_set():
                raise AstraError("cancelled")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise AstraError("timeout")
            try:
                error = outcome.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                continue
            if error is not None:
                raise AstraError(error)
            return

    def receive(self) -> JsonMap:
        while True:
            if self.cancel is not None and self.cancel.is_set():
                raise AstraError('cancelled')
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise AstraError('timeout')
            try:
                raw = self.lines.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                continue
            if raw is None:
                raise AstraError(self.stderr_code or 'terminal_missing')
            value = json_object(raw)
            method = value.get('method')
            if method == 'account/chatgptAuthTokens/refresh':
                self.send({'id': value.get('id'), 'error': {'code': -32000, 'message': 'Host credentials must be renewed separately'}})
                raise AstraError('auth_refresh_needed')
            if 'method' in value and 'id' in value:
                self.send({'id': value['id'], 'error': {'code': -32601, 'message': 'Tools and approvals are disabled'}})
                raise AstraError('unexpected_tool_request')
            if method in {'astra/overflow', 'astra/malformed'}:
                raise AstraError('output_malformed')
            params = value.get('params')
            if method == 'model/rerouted' and isinstance(params, dict) and params.get('toModel') != 'gpt-6-astra':
                raise AstraError('model_rerouted')
            if method == 'error' and isinstance(params, dict) and not params.get('willRetry', False):
                error = params.get('error')
                raise self.failure(error if isinstance(error, dict) else {}, 'async_error')
            if method in {'item/started', 'item/completed'}:
                if not isinstance(params, dict):
                    raise AstraError('rpc_malformed')
                validated_items([params.get('item')])
            return value

    def request(self, method: str, params: JsonMap) -> JsonMap:
        self.request_method = method if method in {'initialize', 'config/read', 'account/login/start', 'thread/start', 'mcpServerStatus/list', 'turn/start'} else 'unknown'
        self.sequence += 1
        identifier = self.sequence
        self.send({'id': identifier, 'method': method, 'params': params})
        while True:
            value = self.receive()
            if value.get('id') == identifier:
                error = value.get('error')
                if isinstance(error, dict):
                    raise self.failure(error, 'request_error')
                result = value.get('result')
                if not isinstance(result, dict):
                    raise AstraError('rpc_malformed')
                return result
            if len(self.events) >= 4096:
                raise AstraError('output_malformed')
            self.events.append(value)

    def failure(self, error: Json, origin: Literal['request_error', 'async_error', 'turn_completed_error']) -> AstraError:
        fields = error if isinstance(error, dict) else {}
        code = error_code(fields) if isinstance(error, dict) else 'rpc_malformed'
        diagnostic: JsonMap = {'origin': origin, 'method': self.request_method, 'category': code}
        if 0 < self.sequence < 2 ** 31:
            diagnostic['request_id'] = self.sequence
        numeric = fields.get('code')
        if type(numeric) is int and -(2 ** 31) <= numeric < 2 ** 31:
            diagnostic['jsonrpc_error_code'] = numeric
        message = fields.get('message')
        if isinstance(message, str):
            diagnostic.update(message_sha256=digest(message.encode('utf-8', errors='surrogatepass')), message_length=len(message))
        self.failure_diagnostic = diagnostic
        return AstraError(code)

    def close(self) -> None:
        requested = self.process.poll() is None
        if requested and self.thread_id and self.turn_id and not any(thread.is_alive() for thread in self.writers):
            try:
                self.send({'id': 'cleanup', 'method': 'turn/interrupt', 'params': {'threadId': self.thread_id, 'turnId': self.turn_id}})
            except AstraError:
                self.cleanup['interrupt_delivery_failed'] = True
        if requested:
            try:
                self.process.terminate()
            except OSError:
                self.cleanup['terminate_failed'] = True
        try:
            self.process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            self.cleanup['initial_wait_failed'] = True
            try:
                self.process.kill()
            except OSError:
                self.cleanup['kill_failed'] = True
            try:
                self.process.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                self.cleanup['reap_failed'] = True
        reaped = self.process.poll() is not None
        for thread in (*self.readers, *self.writers):
            thread.join(timeout=0.5)
        stopped = all(not thread.is_alive() for thread in (*self.readers, *self.writers))
        if reaped and stopped:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    stream.close()
                except OSError:
                    self.cleanup['closed_pipe_flush_failed'] = True
        self.cleanup.update({'pid': self.process.pid, 'owned_child': True, 'termination_requested': requested, 'reaped': reaped, 'returncode': self.process.returncode, 'readers_stopped': all(not reader.is_alive() for reader in self.readers), 'writers_stopped': all(not writer.is_alive() for writer in self.writers)})
