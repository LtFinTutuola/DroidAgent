import sys
sys.path.append('.')
from agent import execute_git

# Test: what does the original PR 17008 body look like on its own branch?
cmd = 'git log --all --grep="[#!]?17008\\b" -E --merges --format="===COMMIT===%H|||%b" -n 20'
out = execute_git(cmd, check=False)
commits = [c for c in out.split('===COMMIT===') if c.strip()]
for c in commits:
    parts = c.split('|||', 1)
    if len(parts) == 2:
        found_hash = parts[0].strip()
        body = parts[1].strip()
        print(f'HASH: {found_hash}')
        print(f'HAS CHERRY: {"cherry" in body.lower()}')
        print(f'BODY:\n{body}')
        print('---')
