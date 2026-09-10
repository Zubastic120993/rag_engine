#!/usr/bin/env python3
from __future__ import annotations

import json

if __name__ == '__main__':
    print(json.dumps({
        'status': 'SUPERSEDED_LIFECYCLE_UNSAFE',
        'message': 'run_full_package_rehearsal.py is superseded for lifecycle work. Use chroma_worker.py, orphan_repair_executor.py, and lifecycle_smoke_test.py instead.',
        'replacement': ['chroma_worker.py', 'orphan_repair_executor.py', 'lifecycle_smoke_test.py'],
    }, indent=2))
