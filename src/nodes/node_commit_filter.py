import os
import re
from src.shared.shared_constants import REPO_PATH, BRANCH, MAX_PRS, logger
from src.shared.shared_functions import execute_git

def is_valid_file(filepath: str, valid_dirs: list) -> bool:
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in [".cs", ".xaml", ".csproj"]: 
        return False
        
    filepath_lower = filepath.lower()
    if filepath_lower.endswith(".designer.cs") or filepath_lower.endswith(".resx") or ".g." in filepath_lower: 
        return False
        
    if ".test/" in filepath_lower or ".tests/" in filepath_lower or ".unittests/" in filepath_lower or \
       filepath_lower.endswith("test.cs") or filepath_lower.endswith("tests.cs"):
        return False
    
    filepath = filepath.replace("\\", "/")
    for d in valid_dirs:
        if d in filepath:
            return True
    return False

def resolve_pr_description(initial_description: str, current_commit_hash: str = "", max_depth: int = 3, visited: set = None) -> str:
    if visited is None:
        visited = set()
    
    if max_depth <= 0:
        return initial_description
        
    pattern = re.compile(r'(?i)cher[ry]+[\s-]*pick(?:ed\s+from|\s+of)?\s*[#!]?\s*(\d+)')
    m = pattern.search(initial_description)
    
    if m:
        pr_id = m.group(1)
        if pr_id in visited:
            return initial_description
        visited.add(pr_id)
        
        cmd = f'git log --all --grep="[#!]?{pr_id}\\b" -E --merges --format="===COMMIT===%H|||%b" -n 20'
        log_output = execute_git(cmd, check=False)
        
        if log_output:
            current_description_normalized = initial_description.strip()
            commits = [c for c in log_output.split('===COMMIT===') if c.strip()]
            for commit_raw in commits:
                parts = commit_raw.split('|||', 1)
                if len(parts) == 2:
                    found_hash = parts[0].strip()
                    body = parts[1].strip()
                    
                    if found_hash == current_commit_hash.strip():
                        continue
                    if body.strip() == current_description_normalized:
                        continue
                    if 'cherry' in body.lower():
                        continue
                    
                    resolved_fetched = resolve_pr_description(body, found_hash, max_depth - 1, visited)
                    initial_description += f"\n\n--- Original PR [{pr_id}] Context ---\n{resolved_fetched}"
                    break
            
    return initial_description

def node_commit_filter(state):
    logger.info("--- NODE 3: History & Commit Filter (Flat Commit Mode) ---")
    raw_valid_dirs = state.get("valid_project_dirs", [])
    
    if not raw_valid_dirs:
        logger.warning("No valid directories provided to node_commit_filter.")
        return {"commits": []}
    
    sln_folder_name = "TCPOS.DroidPos"
    sln_cwd = os.path.join(REPO_PATH, sln_folder_name)
    
    normalized_dirs = set()
    git_dirs = []
    
    for d in raw_valid_dirs:
        safe_d = d if d else "."
        git_dirs.append(f'"{safe_d.replace(chr(92), "/")}"')
        norm_path = os.path.normpath(os.path.join(sln_folder_name, safe_d))
        normalized_dirs.add(norm_path.replace('\\', '/'))
        
    dirs_string = " ".join(git_dirs)
    valid_dirs_normalized = list(normalized_dirs)
    
    logger.info(f"Fetching commits for valid paths...")
    git_log_cmd = f'git --no-pager log origin/{BRANCH} --pretty=format:"%H|%s" -- {dirs_string}'
    
    commits_out = execute_git(git_log_cmd, cwd=sln_cwd)
    lines = commits_out.split("\n") if commits_out else []
    
    extracted_commits = []
    for line in lines:
        if len(extracted_commits) >= MAX_PRS:
            break
            
        if not line: continue
        parts = line.split("|", 1)
        if len(parts) < 2: continue
        
        cHash = parts[0].strip()
        cTitle = parts[1].strip()
        
        changed_c_files = execute_git(f'git show --name-only --format="" {cHash}', cwd=sln_cwd).split("\n")
        
        files_to_process = []
        for f in changed_c_files:
            f_stripped = f.strip()
            if not f_stripped: continue
            if is_valid_file(f_stripped, valid_dirs_normalized):
                files_to_process.append({"name": f_stripped})
        
        if files_to_process:
            logger.info(f"    Found {len(files_to_process)} solution files in commit {cHash[:8]}")
            c_desc_raw = execute_git(f'git show -s --format=%b {cHash}', cwd=sln_cwd)
            c_desc_body = c_desc_raw.strip() if c_desc_raw else ""
            full_description = f"{cTitle}\n\n{c_desc_body}".strip()
            full_description = resolve_pr_description(full_description, cHash)
            
            extracted_commits.append({
                "commit_hash": cHash,
                "commit_description": full_description,
                "files_to_process": files_to_process
            })

    logger.info(f"Commit Filter finished. Extracted {len(extracted_commits)} valid commits.")
    return {"commits": extracted_commits}
