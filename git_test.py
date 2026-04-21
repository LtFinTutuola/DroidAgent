import sys
sys.path.append('.')
from agent import execute_git
cmd = 'git log --all --grep="[#!]?17008\\b" -E --merges --format="===COMMIT===%H|||%b" -n 10'
out = execute_git(cmd, check=False)
commits = [c for c in out.split('===COMMIT===') if c.strip()]
for c in commits:
    parts = c.split('|||', 1)
    if len(parts) == 2:
        print('HASH:', parts[0].strip())
        print('BODY[:50]:', repr(parts[1].strip()[:50]))
