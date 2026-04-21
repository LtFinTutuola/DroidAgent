import json
import glob

files = glob.glob('./output/agent_run_20260421_154223.json')
with open(files[0], 'r', encoding='utf-8') as f:
    data = json.load(f)

for item in data:
    pr = item['pull_request']
    desc = pr.get('pull_request_description', '')
    if '--- Original PR' in desc:
        print('=== PR:', pr['pull_request_title'])
        print(desc)
        print()
