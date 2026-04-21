import sys
sys.path.append('.')
from agent import execute_git, BRANCH

merges_out = execute_git(f'git log origin/{BRANCH} --merges --first-parent --pretty=format:"%H|%s" -n 50')
lines = merges_out.split('\n')
for line in lines:
    if '17196' in line:
        pr_hash, title = line.split('|', 1)
        print('PR_HASH:', pr_hash)
        
        cmd = 'git log --all --grep="[#!]?17008\\b" -E --merges --format="===COMMIT===%H|||%b" -n 10'
        out = execute_git(cmd, check=False)
        commits = [c for c in out.split('===COMMIT===') if c.strip()]
        for c in commits:
            parts = c.split('|||', 1)
            c_hash = parts[0].strip()
            print('  Found log hash:', c_hash, 'Match:', c_hash == pr_hash)
