import json

with open('output/agent_run_20260423_135449.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

commit = data[0]['commit']
file0 = commit['files'][0]
diff0 = file0['file_diffs'][0]

print("Total commits exported:   ", len(data))
print("Sample file:              ", file0["file_name"])
print("Diff keys:                ", list(diff0.keys()))
print("context_summarization:    ", str(diff0.get("context_summarization","MISSING"))[:120])
print()

no_ctx_count = sum(
    1 for item in data
    for fi in item['commit']['files']
    for d in fi['file_diffs']
    if d.get('context_summarization','') in ('[NO_CONTEXT]', '[Summarization failed.]')
)
print("[NO_CONTEXT] leaking into output:", no_ctx_count)

with open('cache/context_cache_20260423_135449.json') as c:
    print("Cache entries:            ", len(json.load(c)))
