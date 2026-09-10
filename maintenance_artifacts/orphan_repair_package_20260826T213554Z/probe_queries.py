#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys
from pathlib import Path
REPO=Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine')
if str(REPO) not in sys.path: sys.path.insert(0, str(REPO))
from rag_engine.query import answer
GEN=Path('/tmp/orphan_repair_diag_20260826T213554Z/repaired_clone')
old=os.environ.copy()
os.environ['RAG_DB_PATH']=str(GEN)
os.environ['CE_LIBRARY_ROOT']='/Users/vladymyrzub/CE_Library'
queries=[
 ('maker-manuals','3 LGIP service experience LGIP Owners Forum Dec 2022 Peter Quaade'),
 ('maker-manuals','LGIP Update November 2022 Peter C Quaade'),
 ('maker-manuals','01 Introduction to ME-GI Mar 2026 company policy do not record the training session'),
 ('maker-manuals','SL2022-725 Action code when convenient PrimeServ Teglholmsgade 41'),
 ('maker-manuals','Action code WHEN CONVENIENT MAN Energy Solutions PrimeServ service letter'),
]
for scope,q in queries:
    r=answer(q, scope=scope, k=5)
    print('QUERY',q)
    print(' status',r.status,'gate',r.gate,'best',r.best_distance)
    print(' top', (r.sources[0]['path'] if r.sources else None))
os.environ.clear(); os.environ.update(old)
