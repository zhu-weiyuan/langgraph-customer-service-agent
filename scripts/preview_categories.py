# -*- coding: utf-8 -*-
import sys, json
sys.stdout.reconfigure(encoding='utf-8')

with open('tests/data/golden_set_500_zwy.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

cats = {}
for d in data:
    cats.setdefault(d['category'], 0)
    cats[d['category']] += 1
print('类别统计:', cats)

for cat in ['high_fail_risk', 'adversarial']:
    items = [d for d in data if d['category'] == cat]
    print(f'\n=== {cat} ({len(items)} 条) ===')
    for d in items:
        print(f'  [{d["id"]}] {d["input"][:90]}')
