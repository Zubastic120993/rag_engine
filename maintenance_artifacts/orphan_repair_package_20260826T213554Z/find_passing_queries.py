#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys
from pathlib import Path
REPO=Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine')
if str(REPO) not in sys.path: sys.path.insert(0, str(REPO))
from rag_engine.query import answer
MANIFEST=Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z/atomic_orphan_repair_manifest.json')
GEN=Path('/tmp/orphan_repair_diag_20260826T213554Z/repaired_clone')
ops=json.loads(MANIFEST.read_text())['operations']
old=os.environ.copy(); os.environ['RAG_DB_PATH']=str(GEN); os.environ['CE_LIBRARY_ROOT']='/Users/vladymyrzub/CE_Library'
for op in ops:
    if op['op_type']=='RETIRE_UNRECOVERABLE':
        continue
    path=op['canonical_live_path']
    q=Path(path).stem.replace('_',' ')
    r=answer(q, scope=op['derived_collection'], k=5)
    top=r.sources[0]['path'] if r.sources else None
    ok=(top==path)
    if ok:
        print('PASS',op['derived_collection'],q,'=>',top)
os.environ.clear(); os.environ.update(old)
