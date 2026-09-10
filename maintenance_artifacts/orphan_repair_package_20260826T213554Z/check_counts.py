#!/usr/bin/env python3
import json
from collections import Counter
from pathlib import Path
p=Path('/tmp/rag_orphan_rehearsal.nhe84f/atomic_orphan_repair_manifest.json')
data=json.loads(p.read_text())
ops=data['operations']
print('ops',len(ops))
print('sum old_paths deterministic',sum(len(op['old_paths']) for op in ops if op['op_type']!='RETIRE_UNRECOVERABLE'))
print('retire old_paths',sum(len(op['old_paths']) for op in ops if op['op_type']=='RETIRE_UNRECOVERABLE'))
print(Counter(op['op_type'] for op in ops))
