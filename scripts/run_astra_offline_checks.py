"""Serial unittest runner with native-model and external-network tripwires."""
from __future__ import annotations
import json
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'crop'), str(ROOT / 'studio')]
from astra_runtime import bootstrap
bootstrap()

original_popen = subprocess.Popen
original_connect = socket.socket.connect
rejections = {'native': 0, 'remote': 0}

def guarded_popen(args, *positional, **kwargs):
    executable = str(args[0] if isinstance(args, (list, tuple)) else args).lower()
    if 'codex' in Path(executable).name and Path(executable).suffix.lower() in {'.exe', '.cmd', '.ps1', ''}:
        rejections['native'] += 1
        raise AssertionError('offline fence: native Codex launch rejected')
    return original_popen(args, *positional, **kwargs)

def guarded_connect(connection, address):
    if isinstance(address, tuple) and address[0] not in {'127.0.0.1', 'localhost', '::1'}:
        rejections['remote'] += 1
        raise AssertionError('offline fence: remote socket rejected')
    return original_connect(connection, address)

with patch('subprocess.Popen', side_effect=guarded_popen), patch('socket.socket.connect', guarded_connect):
    for probe in ('native', 'remote'):
        try:
            if probe == 'native':
                subprocess.Popen(['codex.exe', 'app-server'])
            else:
                with socket.socket() as connection:
                    connection.connect(('203.0.113.1', 443))
        except AssertionError:
            continue
        raise SystemExit('offline tripwire did not reject deliberate probe')
    print('OFFLINE_FENCE ' + json.dumps(rejections), flush=True)
    suite = unittest.defaultTestLoader.discover(sys.argv[1], pattern=sys.argv[2] if len(sys.argv) > 2 else 'test_*.py')
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print('OFFLINE_FENCE_FINAL ' + json.dumps(rejections), flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
