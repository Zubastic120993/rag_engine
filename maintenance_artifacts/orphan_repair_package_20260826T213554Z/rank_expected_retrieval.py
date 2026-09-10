#!/usr/bin/env python3
import json
from pathlib import Path
p=Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z/retrieval_diagnostic_report.json')
data=json.loads(p.read_text())
for case in data['cases']:
    print(f"CASE {case['name']}")
    expected=case['repaired']['chunk_rows'][0]['source']
    for label in ('baseline','repaired'):
        r=case[label]
        np=r['normal_path']
        def rank_in(rows):
            for i,row in enumerate(rows,1):
                if row['source']==expected:
                    return i,row['distance']
            return None,None
        rs,ds=rank_in(r['direct_vector_scope']['pairs'])
        ra,da=rank_in(r['direct_vector_all']['pairs'])
        print(f"  {label}: status={np['status']} gate={np['gate']} best={np['best_distance']} scope_rank={rs} scope_dist={ds} all_rank={ra} all_dist={da}")
