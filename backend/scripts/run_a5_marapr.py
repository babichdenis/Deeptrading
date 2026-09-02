"""A5: инварианты на 2026-03-01..2026-04-30 (AUDIT, baseline не меняет).

Проверка: capital единственный, reconcile 100%, нет generic-гейтов,
MTM доступен. Параллельно E1 review.
"""
import sys, json
sys.path.insert(0, '/Users/Denis/Dev/Deeptrading/backend')
from datetime import datetime, timezone
from app.services.research_pack import build_research_pack

r = build_research_pack(
    datetime(2026,3,1,tzinfo=timezone.utc),
    datetime(2026,4,30,tzinfo=timezone.utc),
    ['BBG008F2T3T2','BBG004S681M2','BBG004S683W7','BBG004S68CP5','BBG004S681B4'],
    capital=10000,
)
print('DONE', json.dumps(r['manifest']['row_counts']))
print('out_dir:', r['out_dir'])
