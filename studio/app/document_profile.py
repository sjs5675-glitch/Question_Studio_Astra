from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

from astra_runtime import inside

from .document_storage import confined
from .document_types import JsonMap


class FreshProfile:
    """Only this instance's exclusively allocated scratch is eligible for one cleanup."""

    def __init__(self, parent: Path) -> None:
        root = inside(parent, 'new browser profile parent')
        root.mkdir(parents=True, exist_ok=True)
        self.path = confined(root, uuid4().hex)
        self.path.mkdir()
        metadata = self.path.stat()
        self.identity = (metadata.st_dev, metadata.st_ino)
        self.consumed = False

    def cleanup(self, *, settled: bool) -> JsonMap:
        receipt: JsonMap = {'profile': str(self.path), 'settled': settled, 'removed': False}
        if self.consumed or not settled:
            return {**receipt, 'reason': 'already_disposed' if self.consumed else 'settlement_unproven'}
        self.consumed = True
        try:
            actual = confined(self.path.parent, self.path.name)
            metadata = actual.stat()
            if (metadata.st_dev, metadata.st_ino) != self.identity:
                return {**receipt, 'reason': 'profile_identity_changed'}
            for directory, children, files in os.walk(actual, followlinks=False):
                for name in [*children, *files]:
                    path = Path(directory) / name
                    if path.is_symlink() or path.is_junction():
                        return {**receipt, 'reason': 'linked_profile_entry'}
            shutil.rmtree(actual)
            return {**receipt, 'removed': True}
        except (OSError, ValueError) as exc:
            return {**receipt, 'reason': 'cleanup_error_no_retry', 'error': str(exc)}
