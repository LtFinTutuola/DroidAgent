import os
import subprocess
import difflib
import re
from typing import List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor
from src.shared.shared_constants import REPO_PATH, logger
from src.shared.shared_functions import execute_git, get_roslyn_server, get_git_batcher, get_diff_char_count, minify_code
from src.classes.xml_preprocessor import XmlPreprocessor

def prepare_roslyn_tool():
    tool_dir = os.path.abspath("./roslyn_tool")
    if not os.path.exists(tool_dir):
        os.makedirs(tool_dir)
        subprocess.run("dotnet new console --use-program-main", cwd=tool_dir, shell=True, check=True)
        subprocess.run("dotnet add package Microsoft.CodeAnalysis.CSharp --version 4.9.2", cwd=tool_dir, shell=True, check=True)
        
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        source_code_path = os.path.join(script_dir, "RoslynPreprocessor.cs")
        
        with open(source_code_path, "r") as f:
            code = f.read()
            
        with open(os.path.join(tool_dir, "Program.cs"), "w") as f:
            f.write(code)
            
        subprocess.run("dotnet build -c Release", cwd=tool_dir, shell=True, check=True)
        
    return os.path.join(tool_dir, "bin", "Release", "net8.0", "roslyn_tool.exe")

def get_changed_line_numbers(old_text: str, new_text: str) -> Tuple[List[int], List[int]]:
    old_lines = []
    new_lines = []
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

def process_commit(commit: Dict, tool_dir: str):
    cHash = commit["commit_hash"]
    server = get_roslyn_server()
    batcher = get_git_batcher()
    
    parent_hash = execute_git(f'git rev-parse "{cHash}~1"', check=False)
    
    files_data = []
    for file_info in commit["files_to_process"]:
        f = file_info["name"]
        
        old_content = batcher.get_file_content(parent_hash, f) if parent_hash else ""
        new_content = batcher.get_file_content(cHash, f)
        
        if not old_content and not new_content:
            continue
            
        if f.endswith(".cs"):
            if old_content == new_content:
                continue
                
            old_lns, new_lns = get_changed_line_numbers(old_content, new_content)
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
                
                if changed_chars < 10:
                    chunk_data["manual_review"] = True
                    
                file_hunks.append(chunk_data)
                
            if file_hunks:
                logger.info(f"Adding {f}: extracted {len(file_hunks)} semantic chunks")
                files_data.append({
                    "file_name": f,
                    "file_diffs": file_hunks
                })
        elif f.endswith((".xaml", ".csproj", ".xml")):
            if old_content == new_content:
                continue
                
            old_lns, new_lns = get_changed_line_numbers(old_content, new_content)
            processor = XmlPreprocessor()
            filtered_hunks = []
            
            for h in processor.process(old_content, new_content, old_lns, new_lns):
                changed_chars = get_diff_char_count(h["clean_old_code"], h["clean_new_code"])
                if changed_chars == 0:
                    continue
                if changed_chars < 10:
                    h["manual_review"] = True
                filtered_hunks.append(h)
                    
            if filtered_hunks:
                logger.info(f"Adding {f}: found {len(filtered_hunks)} xml skeleton hunks")
                files_data.append({
                    "file_name": f,
                    "file_diffs": filtered_hunks
                })
    
    commit["files"] = files_data
    commit["commit_hash_ref"] = commit.pop("commit_hash")
    del commit["files_to_process"]

def node_roslyn_preprocessor(state):
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
