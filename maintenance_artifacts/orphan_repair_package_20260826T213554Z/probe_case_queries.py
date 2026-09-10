#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys, re
from pathlib import Path
REPO=Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine')
if str(REPO) not in sys.path: sys.path.insert(0, str(REPO))
from rag_engine.query import answer
PKG=Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z')
report=json.loads((PKG/'retrieval_diagnostic_report.json').read_text())
GEN=Path('/tmp/orphan_repair_diag_20260826T213554Z/repaired_clone')
os.environ['RAG_DB_PATH']=str(GEN)
os.environ['CE_LIBRARY_ROOT']='/Users/vladymyrzub/CE_Library'
for case in report['cases']:
    exp=case['repaired']['chunk_rows'][0]['source']
    print('\nCASE',case['name'])
    texts=[row['text_head'] for row in case['repaired']['chunk_rows'][:5]]
    stem=Path(exp).stem.replace('_',' ')
    candidates=[stem, case['repaired']['chunk_rows'][0]['text_head'][:120], texts[1][:120] if len(texts)>1 else '', stem+' '+texts[0][:80]]
    extra=[]
    for t in texts:
        toks=[x for x in re.findall(r'[A-Za-z0-9-]{4,}', t) if not x.isdigit()]
        extra.extend(toks[:8])
    candidates.extend([' '.join(extra[:8]), ' '.join(extra[8:16])])
    seen=set()
    for q in candidates:
        q=' '.join(q.split())
        if not q or q in seen: continue
        seen.add(q)
        r=answer(q, scope='maker-manuals', k=5)
        top=r.sources[0]['path'] if r.sources else None
        print('Q:',q[:140])
        print(' status',r.status,'gate',r.gate,'top',top)
        if top==exp:
            print('  MATCH!')
