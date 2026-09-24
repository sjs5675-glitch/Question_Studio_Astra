"""Run the non-authenticated native isolation probe; never starts a model turn."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astra_runtime import ROOT
from astra_cli.contracts import AstraError
from astra_cli.isolation import BASE_CONFIG, check_config, command, environment, native_executable
from astra_cli.rpc import Rpc

parent = ROOT / '.runtime/astra-cli'
parent.mkdir(parents=True, exist_ok=True)
receipt = {'kind': 'native-no-auth-no-inference-isolation-preflight'}
with tempfile.TemporaryDirectory(prefix='preflight-', dir=parent) as temporary:
    root = Path(temporary)
    env = environment(root)
    work = root / 'work'
    work.mkdir()
    (work / '.git').mkdir()
    arguments = command(native_executable(), root)
    receipt['invocation'] = arguments
    rpc = Rpc(arguments, work, env, 30, None)
    try:
        receipt['initialize'] = rpc.request('initialize', {'clientInfo': {'name': 'astra_preflight', 'version': '0.1.0'}, 'capabilities': {'experimentalApi': True}})
        rpc.send({'method': 'initialized', 'params': {}})
        response = rpc.request('config/read', {'cwd': str(work), 'includeLayers': True})
        config = response.get('config') or {}
        receipt['session_layers'] = [layer for layer in response.get('layers', []) if (layer.get('name') or {}).get('type') == 'sessionFlags']
        differences = {}
        for dotted, expected in BASE_CONFIG.items():
            actual = config
            for key in dotted.split('.'):
                actual = actual.get(key) if isinstance(actual, dict) else None
            if expected != actual:
                differences[dotted] = {'expected': expected, 'actual': actual}
        receipt['differences'] = differences
        receipt['isolation'] = check_config(response, root)
        receipt['status'] = 'pass'
    except AstraError as exc:
        receipt.update({'status': 'blocked', 'error_code': exc.code})
    finally:
        rpc.close()
        receipt['cleanup'] = rpc.cleanup
        receipt['created_files'] = [str(path.relative_to(root)) for path in root.rglob('*') if path.is_file()]
receipt['runtime_root_removed'] = not root.exists()
path = ROOT / '.omo/evidence/astra-cli/task-3-isolation-preflight.json'
path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding='utf-8')
print(json.dumps(receipt, ensure_ascii=False))
sys.exit(0 if receipt['status'] == 'pass' else 1)
