#!/usr/bin/env python3
import json
from pathlib import Path
p=Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z/retrieval_diagnostic_report.json')
data=json.loads(p.read_text())
for case in data['cases']:
    print(f"CASE {case['name']}")
    for label in ('baseline','repaired'):
        r=case[label]
        np=r['normal_path']
        print(f"  {label}: tracker_collection={r['tracker_collection']} chunk_count={r['chunk_count']}")
        print(f"    status={np['status']} gate={np['gate']} best={np['best_distance']} floor={np['score_floor']} resolved_scope={np['resolved_scope']}")
        diag=np['diagnostics']
        print('    diag=', {k: diag.get(k) for k in ['raw_count','post_admissibility_count','post_scope_count','post_rerank_count','post_dedupe_count','final_retained_count','final_confidence_passed','gate']})
        print('    query_admissibility=', diag.get('query_admissibility'))
        print('    direct_scope_top3=')
        for row in r['direct_vector_scope']['pairs'][:3]:
            print('     ', row['distance'], row['source'], row['collection'], row['document_type'], row['authority_rank'])
        print('    direct_all_top3=')
        for row in r['direct_vector_all']['pairs'][:3]:
            print('     ', row['distance'], row['source'], row['collection'], row['document_type'], row['authority_rank'])
        print('    first_chunk=', r['chunk_rows'][0])
