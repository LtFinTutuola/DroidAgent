import re

cases = [
    'cherry pick of !5850',
    'cherry-pick #123',
    'cheryy pick 456',
    'cherry picked from 789',
    'cherRy-pick 1234',
    'chery pick of !000'
]

pattern = re.compile(r'(?i)cher[ry]+[\s-]*pick(?:ed\s+from|\s+of)?\s*[#!]?\s*(\d+)')

for c in cases:
    m = pattern.search(c)
    print(c, '->', m.group(1) if m else 'NO MATCH')
