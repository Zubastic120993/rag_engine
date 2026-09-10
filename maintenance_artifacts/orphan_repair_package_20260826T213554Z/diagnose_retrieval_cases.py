#!/usr/bin/env python3
from __future__ import annotations

import json

if __name__ == '__main__':
    print(json.dumps({
        'status': 'SUPERSEDED_LIFECYCLE_UNSAFE',
        'message': 'diagnose_retrieval_cases.py is superseded for lifecycle work. Use chroma_worker.py and lifecycle-safe orchestration instead.',
        'replacement': ['chroma_worker.py', 'orphan_repair_executor.py', 'lifecycle_smoke_test.py'],
    }, indent=2))
