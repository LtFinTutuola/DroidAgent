import os
import json
from src.shared.shared_constants import OUTPUT_FILE, logger
from src.shared.shared_functions import get_roslyn_server, get_git_batcher

def node_json_exporter(state):
    logger.info("--- NODE 6: JSON Exporter ---")
    commits = state.get("commits", [])

    pruned_diff_count = 0
    pruned_file_count = 0
    for commit in commits:
        valid_files = []
        for file_obj in commit.get("files", []):
            valid_diffs = [d for d in file_obj.get("file_diffs", [])]
            pruned_diff_count += len(file_obj.get("file_diffs", [])) - len(valid_diffs)
            if valid_diffs:
                file_obj["file_diffs"] = valid_diffs
                valid_files.append(file_obj)
            else:
                pruned_file_count += 1
        commit["files"] = valid_files

    logger.info(f"Pruned {pruned_diff_count} diffs and {pruned_file_count} empty files.")

    valid_commits = [c for c in commits if c.get("files")]
    logger.info(f"Valid commits after pruning: {len(valid_commits)} (out of {len(commits)})")

    out_dir = os.path.dirname(OUTPUT_FILE)
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    def _clean_commit(c):
        return {k: v for k, v in c.items() if k != "commit_hash_ref"}

    final_output = [{"commit": _clean_commit(c)} for c in valid_commits]

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_output, f, indent=4, ensure_ascii=False)
        logger.info(f"Exported {len(final_output)} Commits to {OUTPUT_FILE}")

    server = get_roslyn_server()
    if server:
        server.stop()
    batcher = get_git_batcher()
    if batcher:
        batcher.stop()

    return state
