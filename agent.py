import os
import yaml
import json
import subprocess
import logging
import threading
from datetime import datetime
from typing import TypedDict, List, Dict, Set
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END
from concurrent.futures import ThreadPoolExecutor

# Note: We keep these imports for future use when LLM is unstubbed
from langchain_huggingface import HuggingFacePipeline
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline, BitsAndBytesConfig
import torch

# 1. Setup Logging
log_dir = "log"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

run_timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
log_file = os.path.join(log_dir, f"agent_run_{run_timestamp}.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 2. Global Sentinel
SENTINEL = "===END_OF_CODE==="

class RoslynServer:
    def __init__(self, tool_dir: str):
        self.tool_dir = tool_dir
        self.process = None
        self.lock = threading.Lock()

    def start(self):
        logger.info("Starting Persistent Roslyn Server...")
        # Use the built binary instead of 'dotnet run' for speed and reliability
        exe_path = os.path.join(self.tool_dir, "bin", "Release", "net8.0", "roslyn_tool.exe")
        if not os.path.exists(exe_path):
            exe_path = "dotnet run -c Release" # Fallback
            
        self.process = subprocess.Popen(
            exe_path if os.path.exists(exe_path) else ["dotnet", "run", "-c", "Release"],
            cwd=self.tool_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8", 
            bufsize=1
        )
        # Wait for "READY"
        while True:
            line = self.process.stdout.readline()
            if not line:
                err = self.process.stderr.read()
                logger.error(f"Roslyn Server failed to start. Stderr: {err}")
                break
            if "READY" in line:
                logger.info("Roslyn Server is READY.")
                break

    def clean_code(self, code: str) -> str:
        with self.lock:
            for attempt in range(2): # Try twice if pipe breaks
                if not self.process or self.process.poll() is not None:
                    self.start()
                
                try:
                    # Send code + sentinel
                    # Ensure each code block ends with a newline so the sentinel is on its own line
                    self.process.stdin.write(code + "\n" + SENTINEL + "\n")
                    self.process.stdin.flush()

                    # Read until sentinel
                    output = []
                    while True:
                        line = self.process.stdout.readline()
                        if not line: 
                            if attempt == 0: break # Try restart
                            return code
                        line = line.strip("\r\n")
                        if line == SENTINEL: break
                        output.append(line)
                    
                    if line == SENTINEL:
                        return "\n".join(output)
                    
                    # If we got here without sentinel, something went wrong
                    logger.warning(f"Roslyn Server didn't return sentinel. Output so far: {len(output)} lines. Sample: {output[:3]}")
                    stderr = ""
                    try:
                        import fcntl
                        import os
                        # This works only on Unix-like, but we are on Windows...
                        # On Windows we can't easily do non-blocking read without a thread
                        pass 
                    except: pass
                    
                    self.stop()
                except (BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Roslyn Server connection lost: {e}. Attempting restart...")
                    self.stop()
                except Exception as e:
                    logger.error(f"Roslyn Server Error: {e}")
                    return code
            return code

    def stop(self):
        if self.process:
            logger.info("Stopping Roslyn Server...")
            self.process.terminate()
            self.process = None

class GitBatcher:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self.process = None
        self.lock = threading.Lock()

    def start(self):
        logger.info("Starting Git Batch Reader (cat-file)...")
        self.process = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            cwd=self.repo_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False, # Use binary for cat-file
            bufsize=0
        )

    def get_file_content(self, commit_hash: str, filepath: str) -> str:
        with self.lock:
            if not self.process or self.process.poll() is not None:
                self.start()
            
            try:
                # git cat-file --batch expects "<sha1>:<path>\n"
                query = f"{commit_hash}:{filepath}\n".encode("utf-8")
                self.process.stdin.write(query)
                self.process.stdin.flush()

                # Response format: "<sha> <type> <size>\n<contents>\n"
                header_line = self.process.stdout.readline()
                if not header_line:
                    return ""
                
                header = header_line.decode("utf-8").strip()
                if "missing" in header or not header:
                    # Log if it's not a known case like a deleted file
                    if "missing" not in header:
                        logger.warning(f"Git Batcher unexpected header: '{header}' for {commit_hash}:{filepath}")
                    return ""
                
                parts = header.split()
                if len(parts) < 3: 
                    logger.warning(f"Git Batcher malformed header: '{header}'")
                    return ""
                
                try:
                    size = int(parts[2])
                    
                    bytes_read = 0
                    chunks = []
                    while bytes_read < size:
                        chunk = self.process.stdout.read(size - bytes_read)
                        if not chunk: break
                        chunks.append(chunk)
                        bytes_read += len(chunk)
                    
                    content = b"".join(chunks)
                    
                    # Thoroughly consume the trailing newline
                    terminator = self.process.stdout.read(1)
                    if terminator != b"\n":
                        logger.warning(f"Git Batcher expected \\n after contents, got {terminator}")
                    
                    return content.decode("utf-8", errors="replace")
                except ValueError:
                    logger.error(f"Git Batcher size parse error in header: '{header}'")
                    # Desync alert! We need to restart the process to clear the pipes
                    self.stop()
                    return ""
            except Exception as e:
                logger.error(f"Git Batcher Error: {e}")
                self.stop()
                return ""

    def stop(self):
        if self.process:
            logger.info("Stopping Git Batcher...")
            self.process.terminate()
            self.process = None

# 3. Load config
with open("config.yaml", "r") as f:
    config = yaml.safe_load(f)

REPO_PATH = config["repo"]["path"]
SLN_PATH = config["repo"]["solution_path"]
BRANCH = config["repo"]["target_branch"]

LLM_MODEL = config["llm"]["model_name"]
API_KEY = config["llm"].get("api_key", "no-key-required")

# Global Git Batcher
GIT_BATCHER = GitBatcher(REPO_PATH)

def get_git_batcher():
    return GIT_BATCHER

# Global Roslyn Server instance
ROSLYN_SERVER = None

def get_roslyn_server():
    global ROSLYN_SERVER
    if ROSLYN_SERVER is None:
        tool_dir = os.path.abspath("./roslyn_tool")
        ROSLYN_SERVER = RoslynServer(tool_dir)
    return ROSLYN_SERVER
max_chunk_size = config["llm"]["chunker"]["max_chunk_size"] if "llm" in config and "chunker" in config["llm"] else 1000

out_dir = os.path.dirname(config["output"]["file_path"])
OUTPUT_FILE = os.path.join(out_dir, f"agent_run_{run_timestamp}.json")

class AgentState(TypedDict):
    valid_project_dirs: List[str]
    pull_requests: List[Dict] # Hierarchical: [{pull_request_title, pull_request_description, commits_list: []}]

def execute_git(cmd: str, cwd: str = REPO_PATH, check=True) -> str:
    logger.info(f"Executing Git: {cmd}")
    try:
        res = subprocess.run(cmd, cwd=cwd, shell=True, text=True, capture_output=True, check=check)
        return res.stdout.strip() if res.stdout else ""
    except subprocess.CalledProcessError as e:
        logger.error(f"Git command failed: {cmd} - {e.stderr}")
        if check:
            raise
        return ""

def node_context_manager(state: AgentState):
    logger.info("--- NODE 1: Git Context Manager ---")
    logger.info(f"Targeting Repository: {REPO_PATH}")
    
    # Cleanup any stale locks
    lock_file = os.path.join(REPO_PATH, ".git", "index.lock")
    if os.path.exists(lock_file):
        logger.info("Removing stale git index lock...")
        try: os.remove(lock_file)
        except: pass
    
    # Check for uncommitted changes and stash them
    status = execute_git("git status --porcelain", check=False)
    if status:
        logger.info("Local changes detected. Stashing...")
        # Stash can fail if index is corrupted or locked, try to proceed anyway if possible
        try:
            execute_git("git stash", check=True)
        except subprocess.CalledProcessError:
            logger.warning("Git stash failed. Forcing reset instead...")
    
    # Enforce hard reset
    execute_git("git reset --hard HEAD", check=True)
    execute_git("git clean -fd", check=True)
    
    execute_git("git fetch origin", check=True)
    logger.info(f"Checking out branch: {BRANCH}")
    execute_git(f"git checkout {BRANCH}", check=True)
    execute_git(f"git pull origin {BRANCH}", check=True)
    
    logger.info("Context Manager Finished.")
    return state

def node_solution_mapper(state: AgentState):
    logger.info("--- NODE 2: Solution Mapper ---")
    sln_full = os.path.join(REPO_PATH, SLN_PATH)
    valid_dirs = []
    
    if os.path.exists(sln_full):
        logger.info(f"Found Solution file at: {sln_full}")
        with open(sln_full, "r", encoding="utf-8-sig") as f:
            for line in f:
                if line.startswith("Project("):
                    parts = line.split(",")
                    if len(parts) >= 2:
                        relative_csproj = parts[1].strip().strip('"')
                        if relative_csproj.endswith(".csproj"):
                            proj_dir = os.path.dirname(relative_csproj).replace("\\", "/")
                            valid_dirs.append(proj_dir)
        logger.info(f"Mapped {len(valid_dirs)} projects from solution.")
    else:
        logger.warning(f"SLN at {sln_full} not found.")

    essential_dirs = ["TCPOS.Droid.", "TCPOS.Maui.Embedding", "TCPOS.Maui.Views"]
    for ed in essential_dirs:
        if not any(ed in vd for vd in valid_dirs):
            logger.info(f"Adding essential directory filter: {ed}")
            valid_dirs.append(ed)

    logger.info(f"Final valid project directories: {len(valid_dirs)}")
    return {"valid_project_dirs": valid_dirs}

def is_valid_file(filepath: str, valid_dirs: List[str]) -> bool:
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in [".cs", ".xaml", ".csproj"]: 
        return False
        
    if filepath.endswith(".designer.cs") or filepath.endswith(".resx") or ".g." in filepath: 
        return False
        
    # FIX 1: Aggressive Pruning of Test Projects
    if ".Test/" in filepath or ".Tests/" in filepath or filepath.endswith("Test.cs") or filepath.endswith("Tests.cs"):
        return False
    
    # Check if the file is inside any of the valid project dirs
    filepath = filepath.replace("\\", "/")
    for d in valid_dirs:
        if d in filepath:
            return True
    return False

import re

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
        
        # Fetch multiple results — we need to semantically filter out backports
        cmd = f'git log --all --grep="[#!]?{pr_id}\\b" -E --merges --format="===COMMIT===%H|||%b" -n 20'
        log_output = execute_git(cmd, check=False)
        fetched_description = ""
        fetched_hash = ""
        
        if log_output:
            current_description_normalized = initial_description.strip()
            commits = [c for c in log_output.split('===COMMIT===') if c.strip()]
            for commit_raw in commits:
                parts = commit_raw.split('|||', 1)
                if len(parts) == 2:
                    found_hash = parts[0].strip()
                    body = parts[1].strip()
                    
                    # Safety filter 1: skip the current PR itself (hash match)
                    if found_hash == current_commit_hash.strip():
                        continue
                    
                    # Safety filter 2: skip content-identical bodies (backport on another branch with same message)
                    if body.strip() == current_description_normalized:
                        continue
                    
                    # Semantic filter: skip any body that is itself a backport/cherry-pick
                    # The TRUE original PR will not contain the words "cherry" in its body
                    if 'cherry' in body.lower():
                        continue
                    
                    # This is the true original PR
                    fetched_hash = found_hash
                    fetched_description = body
                    break
        
        if fetched_description:
            resolved_fetched = resolve_pr_description(fetched_description, fetched_hash, max_depth - 1, visited)
            initial_description += f"\n\n--- Original PR [{pr_id}] Context ---\n{resolved_fetched}"
            
    return initial_description

def node_commit_filter(state: AgentState):
    logger.info("--- NODE 3: History & Commit Filter ---")
    valid_dirs = state["valid_project_dirs"]
    
    # Fetch a larger batch of PRs to ensure we can find 10 relevant ones
    merges_out = execute_git(f'git log origin/{BRANCH} --merges --first-parent --pretty=format:"%H|%s" -n 50')
    lines = merges_out.split("\n") if merges_out else []
    
    pull_requests = []

    for line in lines:
        if len(pull_requests) >= 10:
            break
            
        if not line: continue
        parts = line.split("|")
        if len(parts) < 2: continue
        
        pr_hash, pr_title = parts[0], parts[1]
        
        pr_description_raw = execute_git(f"git show -s --format=%b {pr_hash}")
        pr_description = pr_description_raw.strip() if pr_description_raw else "No description provided"
        if not pr_description:
            pr_description = "No description provided"
            
        pr_description = resolve_pr_description(pr_description, pr_hash)
        
        parents_out = execute_git(f"git show -s --format=%P {pr_hash}")
        parents = parents_out.split()
        if len(parents) < 2: 
            logger.info(f"  Skipping PR commit {pr_hash[:8]} (not a merge)")
            continue
        base_commit, branch_commit = parents[0], parents[1]
        
        changed_pr_files = execute_git(f"git diff --name-only {base_commit}...{branch_commit}").split("\n")
        relevant_files = [f for f in changed_pr_files if f and is_valid_file(f, valid_dirs)]
        
        if relevant_files:
            logger.info(f"  PR '{pr_title}' is relevant ({len(relevant_files)} files).")
            commits_in_pr = execute_git(f"git log --pretty=format:\"%H\" {base_commit}..{branch_commit}").split("\n")
            
            commits_list = []
            for cHash in commits_in_pr:
                if not cHash: continue
                changed_c_files = execute_git(f"git show --name-only --format=\"\" {cHash}").split("\n")
                
                # Only process files that are in DroidPos and are valid types
                files_to_process = []
                for f in changed_c_files:
                    if not f: continue
                    if is_valid_file(f, valid_dirs):
                        files_to_process.append({"name": f})
                
                if files_to_process:
                    logger.info(f"    Found {len(files_to_process)} relevant files in commit {cHash[:8]}")
                    commits_list.append({
                        "commit_hash": cHash,
                        "files_to_process": files_to_process
                    })
            
            if commits_list:
                pull_requests.append({
                    "pull_request_title": pr_title,
                    "pull_request_description": pr_description,
                    "commits_list": commits_list
                })

    logger.info(f"Commit Filter finished. Extracted PRs: {len(pull_requests)}")
    return {"pull_requests": pull_requests}

def prepare_roslyn_tool():
    tool_dir = os.path.abspath("./roslyn_tool")
    if not os.path.exists(tool_dir):
        os.makedirs(tool_dir)
        subprocess.run("dotnet new console --use-program-main", cwd=tool_dir, shell=True, check=True)
        subprocess.run("dotnet add package Microsoft.CodeAnalysis.CSharp --version 4.9.2", cwd=tool_dir, shell=True, check=True)
        
        # Copy our custom source over
        script_dir = os.path.dirname(os.path.abspath(__file__))
        source_code_path = os.path.join(script_dir, "RoslynPreprocessor.cs")
        
        # Read from source
        with open(source_code_path, "r") as f:
            code = f.read()
            
        with open(os.path.join(tool_dir, "Program.cs"), "w") as f:
            f.write(code)
            
        # Build it
        subprocess.run("dotnet build -c Release", cwd=tool_dir, shell=True, check=True)
        
    return os.path.join(tool_dir, "bin", "Release", "net8.0", "roslyn_tool.exe") # Windows exe name

import difflib

def parse_unified_diff(diff_lines: List[str]) -> List[Dict[str, str]]:
    hunks = []
    current_old = []
    current_new = []
    in_hunk = False
    
    for line in diff_lines:
        if line.startswith('--- ') or line.startswith('+++ '):
            continue
        if line.startswith('@@ '):
            if in_hunk:
                hunks.append({
                    "old_code": "\n".join(current_old).strip(),
                    "new_code": "\n".join(current_new).strip()
                })
            in_hunk = True
            current_old = []
            current_new = []
            continue
            
        if in_hunk:
            if line.startswith('-'):
                current_old.append(line[1:])
            elif line.startswith('+'):
                current_new.append(line[1:])
            elif line.startswith(' '):
                # context line
                current_old.append(line[1:])
                current_new.append(line[1:])
            else:
                if line == "":
                    current_old.append("")
                    current_new.append("")
            
    if in_hunk:
        hunks.append({
            "old_code": "\n".join(current_old).strip(),
            "new_code": "\n".join(current_new).strip()
        })
        
    return [h for h in hunks if h["old_code"] or h["new_code"]]

def process_commit(commit: Dict, tool_dir: str):
    cHash = commit["commit_hash"]
    commit["commit_description"] = execute_git(f"git show -s --format=%B {cHash}")
    
    files_data = []
    server = get_roslyn_server()
    batcher = get_git_batcher()
    
    parent_hash = execute_git(f'git rev-parse "{cHash}~1"', check=False)
    
    import re
    for file_info in commit["files_to_process"]:
        f = file_info["name"]
        
        old_content = batcher.get_file_content(parent_hash, f) if parent_hash else ""
        new_content = batcher.get_file_content(cHash, f)
        
        if not old_content and not new_content:
            continue
            
        clean_old_content = old_content
        clean_new_content = new_content
        
        if f.endswith(".cs"):
            if old_content:
                clean_old_content = server.clean_code(old_content)
            if new_content:
                clean_new_content = server.clean_code(new_content)
        elif f.endswith(".xaml") or f.endswith(".csproj"):
            if old_content:
                clean_old_content = re.sub(r'\n\s*\n', '\n\n', old_content).strip()
            if new_content:
                clean_new_content = re.sub(r'\n\s*\n', '\n\n', new_content).strip()
        
        if clean_old_content == clean_new_content:
            logger.info(f"Skipping {f}: clean_old_content == clean_new_content")
            continue
            
        diff_lines = list(difflib.unified_diff(
            clean_old_content.splitlines(), 
            clean_new_content.splitlines(), 
            fromfile="old", 
            tofile="new", 
            lineterm=''
        ))
        
        parsed_hunks = parse_unified_diff(diff_lines)
        if not parsed_hunks:
            logger.info(f"Skipping {f}: parsed_hunks is empty! Diff lines: {len(diff_lines)}")
            continue
            
        logger.info(f"Adding {f}: found {len(parsed_hunks)} hunks")
        files_data.append({
            "file_name": f,
            "file_diffs": parsed_hunks
        })
    
    commit["files"] = files_data
    # Cleanup temporary list
    del commit["files_to_process"]
    del commit["commit_hash"]

def node_roslyn_preprocessor(state: AgentState):
    logger.info("--- NODE 4: Roslyn Preprocessor (Parallel & Persistent) ---")
    pull_requests = state["pull_requests"]
    
    if not pull_requests:
        return state
        
    prepare_roslyn_tool()
    tool_dir = os.path.abspath("./roslyn_tool")
    
    # Process commits in parallel across all PRs
    all_commits = []
    for pr in pull_requests:
        all_commits.extend(pr["commits_list"])
    
    logger.info(f"Using Parallel processing for {len(all_commits)} commits...")
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda c: process_commit(c, tool_dir), all_commits))
            
    return {"pull_requests": pull_requests}

class FileChunks(BaseModel):
    should_split: bool = Field(description="True if the code is large and contains multiple distinct semantic logical blocks that should be separated.")
    chunks: List[str] = Field(description="The source code separated into logical, cohesive parts (methods, related classes). If should_split is false, this should just contain one item with the original text.")

def node_llm_chunker(state: AgentState):
    logger.info("--- NODE 5: LLM-Assisted Semantic Chunker (STUBBED) ---")
    logger.info("Note: LLM chunking is currently stubbed per user request.")
    # In the future, this would iterate through state["pull_requests"] -> commits_list -> files
    # and split the 'text' into chunks if needed.
    return state

def node_json_exporter(state: AgentState):
    logger.info("--- NODE 6: JSON Exporter ---")
    pull_requests = state.get("pull_requests", [])
    
    # Prune empty commits and empty PRs
    valid_prs = []
    for pr in pull_requests:
        valid_commits = [c for c in pr.get("commits_list", []) if len(c.get("files", [])) > 0]
        if valid_commits:
            pr["commits_list"] = valid_commits
            valid_prs.append(pr)
            
    logger.info(f"Pruned empty commits and PRs. Valid PRs: {len(valid_prs)} (out of {len(pull_requests)})")
    
    out_dir = os.path.dirname(OUTPUT_FILE)
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
        
    # Wrap in the requested structure
    final_output = [{"pull_request": pr} for pr in valid_prs]
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_output, f, indent=4)
        logger.info(f"Exported {len(final_output)} Pull Requests to {OUTPUT_FILE}")
    
    # Shutdown helpers
    if ROSLYN_SERVER:
        ROSLYN_SERVER.stop()
    if GIT_BATCHER:
        GIT_BATCHER.stop()
        
    return state

# ---- BUILD LANGGRAPH PIPELINE ----
workflow = StateGraph(AgentState)

workflow.add_node("context_manager", node_context_manager)
workflow.add_node("solution_mapper", node_solution_mapper)
workflow.add_node("commit_filter", node_commit_filter)
workflow.add_node("roslyn_processor", node_roslyn_preprocessor)
workflow.add_node("llm_chunker", node_llm_chunker)
workflow.add_node("json_exporter", node_json_exporter)

# Define edges
workflow.add_edge(START, "context_manager")
workflow.add_edge("context_manager", "solution_mapper")
workflow.add_edge("solution_mapper", "commit_filter")
workflow.add_edge("commit_filter", "roslyn_processor")
workflow.add_edge("roslyn_processor", "llm_chunker")
workflow.add_edge("llm_chunker", "json_exporter")
workflow.add_edge("json_exporter", END)

app = workflow.compile()

if __name__ == "__main__":
    logger.info("Starting DroidAgent Pipeline...")
    result = app.invoke({
        "valid_project_dirs": [],
        "pull_requests": []
    })
    logger.info("Pipeline Finished Successfully!")
