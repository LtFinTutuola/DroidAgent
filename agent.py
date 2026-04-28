import os
import re
import atexit
import yaml
import json
import subprocess
import logging
import threading
from datetime import datetime
from typing import TypedDict, List, Dict, Set, Tuple
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv()

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

    def _send_command(self, command: str, code: str) -> str:
        with self.lock:
            for attempt in range(2): # Try twice if pipe breaks
                if not self.process or self.process.poll() is not None:
                    self.start()
                
                try:
                    # Send command line then code + sentinel
                    self.process.stdin.write(command + "\n")
                    self.process.stdin.write(code + "\n" + SENTINEL + "\n")
                    self.process.stdin.flush()

                    # Read until sentinel
                    output = []
                    while True:
                        line = self.process.stdout.readline()
                        if not line: 
                            if attempt == 0: break # Try restart
                            return ""
                        line = line.strip("\r\n")
                        if line == SENTINEL: break
                        output.append(line)
                    
                    if line == SENTINEL:
                        return "\n".join(output)
                    
                    self.stop()
                except (BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Roslyn Server connection lost: {e}. Attempting restart...")
                    self.stop()
                except Exception as e:
                    logger.error(f"Roslyn Server Error: {e}")
                    return ""
            return ""

    def clean_code(self, code: str) -> str:
        return self._send_command("CLEAN|||", code)

    def diff_extract(self, old_code: str, new_code: str, old_lns: List[int], new_lns: List[int]) -> List[Dict]:
        old_lns_str = ",".join(map(str, old_lns))
        new_lns_str = ",".join(map(str, new_lns))
        header = f"DIFF_EXTRACT|||{old_lns_str}|||{new_lns_str}"
        combined_code = f"{old_code}\n---DELIMITER---\n{new_code}"
        res = self._send_command(header, combined_code)
        try:
            return json.loads(res) if res else []
        except Exception as e:
            logger.error(f"Failed to parse Roslyn JSON: {e}. Response: {res[:100]}...")
            return []

    def extract_block(self, code: str, line_num: int) -> Dict:
        """Extracts the semantic node covering line_num. Returns {signature, block_code}."""
        header = f"EXTRACT_BLOCK|||{line_num}"
        res = self._send_command(header, code)
        try:
            return json.loads(res) if res else {"signature": "", "block_code": ""}
        except Exception:
            return {"signature": "", "block_code": res or ""}

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
MAX_PRS = config["repo"].get("max_prs", 10)

LLM_MODEL = config["llm"].get("model_name", "gpt-4o-mini")
MAX_CHUNK_LENGTH = config["llm"].get("max_chunk_length", 4000)
ENABLE_INTENT_DISAGGREGATION = config["llm"].get("enable_intent_disaggregation", False)

# OpenAI client (lazy init)
_OPENAI_CLIENT: OpenAI = None

def get_openai_client() -> OpenAI:
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is None:
        _OPENAI_CLIENT = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    return _OPENAI_CLIENT

# Context cache
CONTEXT_CACHE: Dict[str, str] = {}
_CACHE_DIRTY_COUNT = 0
_CACHE_AUTOSAVE_INTERVAL = 50
CACHE_FILE = f"./cache/context_cache_{run_timestamp}.json"

def init_cache():
    global CACHE_FILE
    os.makedirs("./cache", exist_ok=True)
    if config["llm"].get("reset_cache", False):
        logger.info("reset_cache is True. Starting with empty cache.")
        return
    import glob
    cache_files = glob.glob("./cache/context_cache_*.json")
    if cache_files:
        try:
            latest_file = max(cache_files, key=os.path.getmtime)
            with open(latest_file, "r", encoding="utf-8") as fh:
                CONTEXT_CACHE.update(json.load(fh))
            CACHE_FILE = latest_file
            logger.info(f"Loaded {len(CONTEXT_CACHE)} entries from cache: {latest_file}. Future updates will be saved here.")
        except Exception as e:
            logger.warning(f"Could not load cache file: {e}")

def save_cache():
    try:
        os.makedirs("./cache", exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(CONTEXT_CACHE, fh, indent=2)
        logger.info(f"Saved {len(CONTEXT_CACHE)} cache entries to {CACHE_FILE}")
    except Exception as e:
        logger.error(f"Failed to save cache: {e}")

atexit.register(save_cache)

def maybe_autosave_cache():
    global _CACHE_DIRTY_COUNT
    _CACHE_DIRTY_COUNT += 1
    if _CACHE_DIRTY_COUNT >= _CACHE_AUTOSAVE_INTERVAL:
        save_cache()
        _CACHE_DIRTY_COUNT = 0

def _parse_llm_json(response_str: str, default):
    """Try json.loads, strip markdown wrappers, return default on failure."""
    if not response_str:
        return default
    for attempt in [response_str, re.sub(r'^```[\w]*\n?|\n?```$', '', response_str.strip())]:
        try:
            return json.loads(attempt)
        except Exception:
            continue
            
    # Robust fallback: extract JSON substring
    match = re.search(r'(\{.*\}|\[.*\])', response_str, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass
            
    logger.warning(f"_parse_llm_json: could not parse response: {response_str[:120]}")
    return default

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

out_dir = os.path.dirname(config["output"]["file_path"])
OUTPUT_FILE = os.path.join(out_dir, f"agent_run_{run_timestamp}.json")

class AgentState(TypedDict):
    valid_project_dirs: List[str]
    commits: List[Dict] # Flat: [{commit_hash, commit_description, files_to_process: []}]

def execute_git(cmd: str, cwd: str = REPO_PATH, check=True) -> str:
    logger.info(f"Executing Git: {cmd}")
    try:
        # Aggiunti encoding='utf-8' ed errors='replace' per prevenire UnicodeDecodeError su Windows
        res = subprocess.run(
            cmd, 
            cwd=cwd, 
            shell=True, 
            text=True, 
            capture_output=True, 
            encoding='utf-8', 
            errors='replace', 
            check=check
        )
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
        
    # Ignore designers, resources and generated files
    filepath_lower = filepath.lower()
    if filepath_lower.endswith(".designer.cs") or filepath_lower.endswith(".resx") or ".g." in filepath_lower: 
        return False
        
    # FIX 1: Aggressive Pruning of Test Projects
    if ".test/" in filepath_lower or ".tests/" in filepath_lower or ".unittests/" in filepath_lower or \
       filepath_lower.endswith("test.cs") or filepath_lower.endswith("tests.cs"):
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
    logger.info("--- NODE 3: History & Commit Filter (Flat Commit Mode) ---")
    raw_valid_dirs = state.get("valid_project_dirs", [])
    
    if not raw_valid_dirs:
        logger.warning("No valid directories provided to node_commit_filter.")
        return {"pull_requests": []} # Aggiornato per riflettere il nuovo stato
    
    # --- 1. DEFINIZIONE DEL CONTESTO DI ESECUZIONE ---
    sln_folder_name = "TCPOS.DroidPos"
    sln_cwd = os.path.join(REPO_PATH, sln_folder_name)
    
    # --- 2. PREPARAZIONE PERCORSI ---
    normalized_dirs = set()
    git_dirs = []
    
    for d in raw_valid_dirs:
        safe_d = d if d else "."
        # Per il comando Git
        git_dirs.append(f'"{safe_d.replace(chr(92), "/")}"')
        
        # Per la funzione is_valid_file (normalizziamo rispetto alla root)
        norm_path = os.path.normpath(os.path.join(sln_folder_name, safe_d))
        normalized_dirs.add(norm_path.replace('\\', '/'))
        
    dirs_string = " ".join(git_dirs)
    valid_dirs_normalized = list(normalized_dirs)
    
    # --- 3. ESTRAZIONE DEI COMMIT ---
    logger.info(f"Fetching commits for valid paths...")
    git_log_cmd = f'git --no-pager log origin/{BRANCH} --pretty=format:"%H|%s" -- {dirs_string}'
    
    commits_out = execute_git(git_log_cmd, cwd=sln_cwd)
    lines = commits_out.split("\n") if commits_out else []
    
    # Nuova lista piatta per i commit
    extracted_commits = []

    for line in lines:
        if len(extracted_commits) >= MAX_PRS: # Puoi rinominare MAX_PRS in MAX_COMMITS nel tuo config
            break
            
        if not line: continue
        parts = line.split("|", 1)
        if len(parts) < 2: continue
        
        cHash = parts[0].strip()
        cTitle = parts[1].strip()
        
        # --- 4. ESTRAZIONE E FILTRAGGIO DEI FILE ---
        changed_c_files = execute_git(f'git show --name-only --format="" {cHash}', cwd=sln_cwd).split("\n")
        
        files_to_process = []
        for f in changed_c_files:
            f_stripped = f.strip()
            if not f_stripped: continue
            
            # FILTRO RIPRISTINATO: Teniamo solo i file che appartengono ai progetti della solution
            if is_valid_file(f_stripped, valid_dirs_normalized):
                files_to_process.append({"name": f_stripped})
        
        # --- 5. ASSEMBLAGGIO DEL SINGOLO COMMIT ---
        if files_to_process:
            logger.info(f"    Found {len(files_to_process)} solution files in commit {cHash[:8]}")
            
            # Recuperiamo il body del commit
            c_desc_raw = execute_git(f'git show -s --format=%b {cHash}', cwd=sln_cwd)
            c_desc_body = c_desc_raw.strip() if c_desc_raw else ""
            
            # Uniamo il titolo (soggetto) e il corpo per creare una descrizione completa
            full_description = f"{cTitle}\n\n{c_desc_body}".strip()
            full_description = resolve_pr_description(full_description, cHash)
            
            # Popoliamo la nuova struttura dati
            extracted_commits.append({
                "commit_hash": cHash,
                "commit_description": full_description,
                "files_to_process": files_to_process
            })

    logger.info(f"Commit Filter finished. Extracted {len(extracted_commits)} valid commits.")
    
    # Restituiamo il nuovo oggetto
    return {"commits": extracted_commits}

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

def get_changed_line_numbers(old_text: str, new_text: str) -> Tuple[List[int], List[int]]:
    old_lines = []
    new_lines = []
    # Use n=0 to get only changed lines with no context
    diff = list(difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        n=0, lineterm=''
    ))
    
    curr_old = 0
    curr_new = 0
    
    for line in diff:
        if line.startswith('@@'):
            m = re.match(r'@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@', line)
            if m:
                curr_old = int(m.group(1))
                curr_new = int(m.group(3))
        elif line.startswith('-'):
            if curr_old > 0:
                old_lines.append(curr_old)
                curr_old += 1
        elif line.startswith('+'):
            if curr_new > 0:
                new_lines.append(curr_new)
                curr_new += 1
        elif line.startswith(' '):
            curr_old += 1
            curr_new += 1
            
    return sorted(list(set(old_lines))), sorted(list(set(new_lines)))

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

def minify_code(text: str) -> str:
    if not text: return text
    # Collapse multiple horizontal spaces/tabs
    text = re.sub(r'[ \t]+', ' ', text)
    # Remove leading spaces/indentation on every line
    text = re.sub(r'(?m)^[ \t]+', '', text)
    return text.strip()

def get_diff_char_count(clean_old: str, clean_new: str) -> int:
    """Returns 0 if purely whitespace/identical, otherwise returns the number of changed characters."""
    if not clean_old and not clean_new: 
        return 0
    
    str_old = re.sub(r'\s+', '', clean_old)
    str_new = re.sub(r'\s+', '', clean_new)
    
    if str_old == str_new: 
        return 0
        
    diff = difflib.ndiff(str_old, str_new)
    changed_chars = sum(1 for d in diff if d.startswith('+ ') or d.startswith('- '))
    
    return changed_chars

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
            # 1. Saltiamo se i file crudi sono identici
            if old_content == new_content:
                continue
                
            # 2. Calcoliamo i numeri di riga sui file ORIGINALI (raw). 
            # È vitale per mantenere l'allineamento corretto quando passiamo il testo a Roslyn.
            old_lns, new_lns = get_changed_line_numbers(old_content, new_content)
            
            # 3. Passiamo i file ORIGINALI a Roslyn, in modo che CreateChunk abbia accesso ai commenti per il raw_code!
            aligned_chunks = server.diff_extract(old_content, new_content, old_lns, new_lns)
            
            file_hunks = []
            for chunk in aligned_chunks:
                raw_old   = minify_code(chunk.get("raw_old_code",   ""))
                clean_old = minify_code(chunk.get("clean_old_code", ""))
                raw_new   = minify_code(chunk.get("raw_new_code",   ""))
                clean_new = minify_code(chunk.get("clean_new_code", ""))
                
                changed_chars = get_diff_char_count(clean_old, clean_new)
                if changed_chars == 0:
                    continue
                
                chunk_data = {
                    "raw_old_code":   raw_old,
                    "clean_old_code": clean_old,
                    "raw_new_code":   raw_new,
                    "clean_new_code": clean_new,
                }
                
                # Aggiunge il flag SOLO se i caratteri cambiati sono meno di 10
                if changed_chars < 10:
                    chunk_data["manual_review"] = True
                    
                file_hunks.append(chunk_data)
                
            if file_hunks:
                logger.info(f"Adding {f}: extracted {len(file_hunks)} semantic chunks")
                files_data.append({
                    "file_name": f,
                    "file_diffs": file_hunks
                })
        elif f.endswith(".xaml") or f.endswith(".csproj"):
            if old_content:
                clean_old_content = re.sub(r'\n\s*\n', '\n\n', old_content).strip()
            if new_content:
                clean_new_content = re.sub(r'\n\s*\n', '\n\n', new_content).strip()
                
            if clean_old_content == clean_new_content:
                continue
                
            diff_lines = list(difflib.unified_diff(
                clean_old_content.splitlines(), 
                clean_new_content.splitlines(), 
                n=5, lineterm=''
            ))
            
            parsed_hunks = parse_unified_diff(diff_lines)
            filtered_hunks = []
            for h in parsed_hunks:
                changed_chars = get_diff_char_count(h["old_code"], h["new_code"])
                if changed_chars > 0:
                    if changed_chars < 10:
                        h["manual_review"] = True
                    filtered_hunks.append(h)
                    
            if filtered_hunks:
                logger.info(f"Adding {f}: found {len(filtered_hunks)} expanded context hunks")
                files_data.append({
                    "file_name": f,
                    "file_diffs": filtered_hunks
                })
    
    commit["files"] = files_data
    # Preserve hash for Phase A git lookup; remove staging key
    commit["commit_hash_ref"] = commit.pop("commit_hash")
    del commit["files_to_process"]

def node_roslyn_preprocessor(state: AgentState):
    logger.info("--- NODE 4: Roslyn Preprocessor (Parallel & Persistent) ---")
    commits = state["commits"]
    
    if not commits:
        return state
        
    prepare_roslyn_tool()
    tool_dir = os.path.abspath("./roslyn_tool")
    
    logger.info(f"Using Parallel processing for {len(commits)} commits...")
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda c: process_commit(c, tool_dir), commits))
            
    return {"commits": commits}

# ── Phase B: Commit Intent Disaggregation ──────────────────────────────────────
def disaggregate_commit_intent(commit: Dict) -> None:
    if not ENABLE_INTENT_DISAGGREGATION:
        return
    description = commit.get("commit_description", "").strip()
    if not description:
        return
    prompt = (
        "<Role>Technical Lead</Role>\n"
        "<Task>Deconstruct the provided commit description into a list of atomic, independent technical tasks or sub-intents.</Task>\n"
        "<Constraints>\n"
        "1. Extract only actionable/functional changes.\n"
        "2. Ignore metadata (e.g., ticket numbers, reviewer names).\n"
        "3. Output STRICTLY as a valid JSON array of strings. Do not use markdown formatting (no ```json). Do not include any preamble or text.\n"
        "</Constraints>\n"
        f"<InputDescription>\n{description}\n</InputDescription>"
    )
    try:
        resp = get_openai_client().chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        raw = resp.choices[0].message.content
        commit["disaggregated_intents"] = _parse_llm_json(raw, [])
    except Exception as e:
        logger.error(f"Phase B failed for commit: {e}")


# ── Phase C: Atomic Diff Splitting (4-Key Preservation) ───────────────────────
_NO_CONTEXT_STRINGS = frozenset([
    "[NO_CONTEXT]",
    "[Summarization failed.]",
    "[Could not extract a semantic block at this location.]",
    "No code content available for summarization.",
])

def split_large_diffs(file_obj: Dict) -> None:
    server = get_roslyn_server()
    expanded = []
    is_cs = file_obj.get("file_name", "").endswith(".cs")
    for diff in file_obj.get("file_diffs", []):
        total_len = len(diff.get("raw_old_code", "")) + len(diff.get("raw_new_code", ""))
        if total_len > MAX_CHUNK_LENGTH:
            prompt = (
                "<Role>Git & Code Review Expert</Role>\n"
                "<Task>Split the provided large diff into smaller, logically atomic sub-diffs based on independent functional changes.</Task>\n"
                "<Constraints>\n"
                "1. Each sub-diff must represent a standalone logical change.\n"
                "2. Do NOT alter, omit, or hallucinate code content; strictly segment the existing code.\n"
                '3. Output STRICTLY as a JSON object without markdown wrapping (no ```json). Do not include any preamble, explanations, or conversational text. Your entire response must be valid, parsable JSON: {"sub_diffs": [{"raw_old_code": "...", "raw_new_code": "..."}]}\n'
                "</Constraints>\n"
                f"<InputDiff>\nOld Code: {diff.get('raw_old_code','')}\nNew Code: {diff.get('raw_new_code','')}\n</InputDiff>"
            )
            try:
                resp = get_openai_client().chat.completions.create(
                    model=LLM_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                )
                parsed = _parse_llm_json(resp.choices[0].message.content, {})
                sub_diffs = parsed.get("sub_diffs", [])
                if sub_diffs:
                    for sd in sub_diffs:
                        raw_old = sd.get("raw_old_code", "")
                        raw_new = sd.get("raw_new_code", "")
                        # Deterministic clean-track via Roslyn (only for C# files)
                        if is_cs:
                            clean_old = server.clean_code(raw_old) if raw_old else ""
                            clean_new = server.clean_code(raw_new) if raw_new else ""
                        else:
                            clean_old, clean_new = raw_old, raw_new
                            
                        changed_chars = get_diff_char_count(clean_old, clean_new)
                        if changed_chars == 0:
                            continue
                            
                        chunk_data = {
                            "raw_old_code":   raw_old,
                            "clean_old_code": clean_old,
                            "raw_new_code":   raw_new,
                            "clean_new_code": clean_new,
                        }
                        
                        # Aggiunge il flag SOLO se i caratteri cambiati sono meno di 10
                        if changed_chars < 10:
                            chunk_data["manual_review"] = True
                            
                        expanded.append(chunk_data)
                    continue  # original diff replaced by sub-diffs
            except Exception as e:
                logger.error(f"Phase C split failed: {e}")
        expanded.append(diff)  # unchanged or fallback
    file_obj["file_diffs"] = expanded


# ── Phase A: Per-diff Context Summarization (Dual-Track) ──────────────────────
_NO_CONTEXT_REPLY = "[NO_CONTEXT]"

def _xml_extract_parent_block(xml_text: str, search_text: str) -> str:
    """Find the enclosing XML/XAML element that contains search_text using simple tag walking."""
    if not search_text or not xml_text:
        return ""
    idx = xml_text.find(search_text)
    if idx == -1:
        # Try the first non-whitespace line of search_text
        first = next((ln.strip() for ln in search_text.splitlines() if ln.strip()), "")
        idx = xml_text.find(first)
    if idx == -1:
        return xml_text[:2000]  # fallback: first 2000 chars

    # Walk backwards to find the opening tag of the enclosing element
    start = xml_text.rfind('<', 0, idx)
    while start > 0 and xml_text[start + 1] in ('!', '?', '/'):
        start = xml_text.rfind('<', 0, start)

    if start == -1:
        return xml_text[:2000]

    # Tag name
    tag_end = start + 1
    while tag_end < len(xml_text) and xml_text[tag_end] not in (' ', '\t', '\n', '\r', '>'):
        tag_end += 1
    tag_name = xml_text[start + 1:tag_end]
    if not tag_name:
        return xml_text[:2000]

    # Find the matching closing tag
    close_tag = f"</{tag_name}>"
    end = xml_text.find(close_tag, idx)
    if end == -1:
        end = min(start + 3000, len(xml_text))
    else:
        end += len(close_tag)

    return xml_text[start:end]


def _build_context_prompt(file_name: str, block: str, raw_old: str, raw_new: str) -> str:
    """Route to the correct specialised prompt based on file extension."""
    no_ctx_instruction = (
        "PRE-FLIGHT CHECK: First evaluate the InputDiff. If the change solely consists of irrelevant noise such as typo corrections, "
        "basic variable renaming with no functional shift, or dead-code deletion, YOU MUST output exactly: [NO_CONTEXT]\n"
        "If the change is semantically meaningful, provide the 2-3 sentence summary of the InputCode block, ignoring the diff details.\n"
    )
    diff_payload = f"<InputDiff>\nOld:\n{raw_old}\nNew:\n{raw_new}\n</InputDiff>\n"
    if file_name.endswith(".csproj"):
        return (
            "<Role>.NET Architect</Role>\n"
            "<Task>Analyze this .csproj project configuration block. Describe the architectural impact "
            "(e.g., added NuGet dependency, target framework change, project reference).</Task>\n"
            "<Constraints>\n"
            "1. Maximum 2-3 sentences.\n"
            "2. Focus on 'what changed' and 'why it matters architecturally'.\n"
            "3. Do not include code snippets, markdown blocks, or conversational filler.\n"
            f"4. {no_ctx_instruction}"
            "5. Return only the plain text summary or [NO_CONTEXT].\n"
            "</Constraints>\n"
            f"{diff_payload}"
            f"<InputCode>\n{block}\n</InputCode>"
        )
    elif file_name.endswith(".xaml") and not file_name.endswith(".xaml.cs"):
        return (
            "<Role>UI/UX Developer</Role>\n"
            "<Task>Analyze this XAML UI code block. Describe which part of the screen or visual component "
            "is being defined or altered in this POS system.</Task>\n"
            "<Constraints>\n"
            "1. Maximum 2-3 sentences.\n"
            "2. Focus on the visual/interaction purpose, not XML syntax.\n"
            "3. Do not include code snippets, markdown blocks, or conversational filler.\n"
            f"4. {no_ctx_instruction}"
            "5. Return only the plain text summary or [NO_CONTEXT].\n"
            "</Constraints>\n"
            f"{diff_payload}"
            f"<InputCode>\n{block}\n</InputCode>"
        )
    else:  # .cs and .xaml.cs
        return (
            "<Role>Senior C# Software Engineer</Role>\n"
            "<Task>Analyze the provided C# code block and summarize its core functional responsibility "
            "within this POS system.</Task>\n"
            "<Constraints>\n"
            "1. Maximum 2-3 sentences.\n"
            "2. Focus on 'what' it does and 'why', avoiding line-by-line descriptions.\n"
            "3. Do not include code snippets, markdown blocks, or conversational filler.\n"
            f"4. {no_ctx_instruction}"
            "5. Return only the plain text summary or [NO_CONTEXT].\n"
            "</Constraints>\n"
            f"{diff_payload}"
            f"<InputCode>\n{block}\n</InputCode>"
        )


def enrich_file_diffs_with_context(file_obj: Dict, commit_hash: str) -> None:
    if not commit_hash:
        return
    server    = get_roslyn_server()
    batcher   = get_git_batcher()
    file_name = file_obj.get("file_name", "")

    # Fetch the current state of the file at this commit
    full_file = batcher.get_file_content(commit_hash, file_name)
    if not full_file:
        return  # File was deleted — skip summarization

    is_cs   = file_name.endswith(".cs")          # includes .xaml.cs
    is_xml  = file_name.endswith(".csproj") or (file_name.endswith(".xaml") and not is_cs)

    for diff in file_obj.get("file_diffs", []):
        raw_old = diff.get("raw_old_code", "")
        raw_new = diff.get("raw_new_code", "")

        representative_code = raw_new if raw_new else raw_old
        if not representative_code:
            diff["context_summarization"] = _NO_CONTEXT_REPLY
            continue

        # ── Track 1: C# → Roslyn EXTRACT_BLOCK ────────────────────────────────
        if is_cs:
            # Find the first non-empty line to locate a line number in the full file
            first_line = next((ln.strip() for ln in representative_code.splitlines() if ln.strip()), "")
            line_num = 1
            if first_line:
                for idx, file_line in enumerate(full_file.splitlines(), start=1):
                    if first_line in file_line:
                        line_num = idx
                        break

            result = server.extract_block(full_file, line_num)  # returns {signature, block_code}
            signature  = result.get("signature", "")
            block      = result.get("block_code", "").strip()
            cache_key  = f"{file_name}::{signature}" if signature else f"{file_name}::line{line_num}"

            # Token-saving short-circuit
            if block and block == raw_new.strip():
                diff["context_summarization"] = "[Current code block is identical to new_code — no broader context available.]"
                continue
            if block and block == raw_old.strip():
                diff["context_summarization"] = "[Current code block is identical to old_code — change may have been reverted.]"
                continue
            if not block:
                diff["context_summarization"] = _NO_CONTEXT_REPLY
                continue

        # ── Track 2: XML/XAML → Python parent-node extraction ─────────────────
        elif is_xml:
            block     = _xml_extract_parent_block(full_file, representative_code)
            cache_key = f"{file_name}::{block[:80].strip()}"
            if not block.strip():
                diff["context_summarization"] = _NO_CONTEXT_REPLY
                continue

        else:
            diff["context_summarization"] = _NO_CONTEXT_REPLY
            continue

        # ── Cache lookup / LLM call ────────────────────────────────────────────
        if cache_key in CONTEXT_CACHE:
            diff["context_summarization"] = CONTEXT_CACHE[cache_key]
        else:
            prompt = _build_context_prompt(file_name, block, raw_old, raw_new)
            try:
                resp = get_openai_client().chat.completions.create(
                    model=LLM_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                )
                summary = resp.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"Phase A LLM call failed for {file_name}: {e}")
                summary = "[Summarization failed.]"

            CONTEXT_CACHE[cache_key] = summary
            maybe_autosave_cache()
            diff["context_summarization"] = summary


# ── Node 5: LLM Enrichment ──────────────────────────────────────────────────────
def node_llm_chunker(state: AgentState):
    logger.info("--- NODE 5: LLM Enrichment (Context / Intent / Splitting) ---")
    commits = state["commits"]
    init_cache()

    # Phase B: per-commit intent disaggregation (optional)
    if ENABLE_INTENT_DISAGGREGATION:
        logger.info("Phase B: Commit Intent Disaggregation...")
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(disaggregate_commit_intent, commits))
    else:
        logger.info("Phase B: Disabled (enable_intent_disaggregation=false)")

    # Build flat (file_obj, commit_hash) pairs for Phase A & C
    file_commit_pairs = [
        (f, c.get("commit_hash_ref", ""))
        for c in commits for f in c.get("files", [])
    ]

    # Phase C: split large diffs first (before summarization)
    logger.info(f"Phase C: Atomic Diff Splitting on {len(file_commit_pairs)} files...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda p: split_large_diffs(p[0]), file_commit_pairs))

    # Phase A: per-diff context summarization
    logger.info(f"Phase A: Context Summarization on {len(file_commit_pairs)} files...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda p: enrich_file_diffs_with_context(p[0], p[1]), file_commit_pairs))

    save_cache()
    return {"commits": commits}

def node_json_exporter(state: AgentState):
    logger.info("--- NODE 6: JSON Exporter ---")
    commits = state.get("commits", [])

    # ── Prune [NO_CONTEXT] diffs and empty files ───────────────────────────────
    pruned_diff_count  = 0
    pruned_file_count  = 0
    for commit in commits:
        valid_files = []
        for file_obj in commit.get("files", []):
            valid_diffs = [
                d for d in file_obj.get("file_diffs", [])
                if d.get("context_summarization", "") not in _NO_CONTEXT_STRINGS
            ]
            pruned_diff_count += len(file_obj.get("file_diffs", [])) - len(valid_diffs)
            if valid_diffs:
                file_obj["file_diffs"] = valid_diffs
                valid_files.append(file_obj)
            else:
                pruned_file_count += 1
        commit["files"] = valid_files

    logger.info(f"Pruned {pruned_diff_count} [NO_CONTEXT] diffs and {pruned_file_count} empty files.")

    # Prune empty commits
    valid_commits = [c for c in commits if c.get("files")]
    logger.info(f"Valid commits after pruning: {len(valid_commits)} (out of {len(commits)})")

    out_dir = os.path.dirname(OUTPUT_FILE)
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    # Strip internal helper fields before export
    def _clean_commit(c: Dict) -> Dict:
        return {k: v for k, v in c.items() if k != "commit_hash_ref"}

    final_output = [{"commit": _clean_commit(c)} for c in valid_commits]

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_output, f, indent=4)
        logger.info(f"Exported {len(final_output)} Commits to {OUTPUT_FILE}")

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
        "commits": []
    })
    logger.info("Pipeline Finished Successfully!")
