import os
import yaml
import json
import subprocess
from typing import TypedDict, List, Dict, Set
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END

from langchain_huggingface import HuggingFacePipeline
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline, BitsAndBytesConfig
import torch

# 1. Load config
with open("config.yaml", "r") as f:
    config = yaml.safe_load(f)

REPO_PATH = config["repo"]["path"]
SLN_PATH = config["repo"]["solution_path"]
BRANCH = config["repo"]["target_branch"]

LLM_MODEL = config["llm"]["model_name"]
API_KEY = config["llm"]["api_key"]
MAX_CHUNK_SIZE = config["llm"]["chunker"]["max_chunk_size"]

OUTPUT_FILE = config["output"]["file_path"]

# os.environ["OPENAI_API_KEY"] = API_KEY # No longer needed for local HF

class AgentState(TypedDict):
    valid_project_dirs: List[str]
    valid_commits_files: Dict[str, List[str]] # commit_hash -> [file_paths]
    cleaned_commits_data: List[Dict] # [{commit: hash, msg: str, file: file, content: cleaned_str}, ...]
    chunked_data: List[Dict] # [{text: "...", original_commit: hash, original_file: file}, ...]

def execute_git(cmd: str, cwd: str = REPO_PATH) -> str:
    try:
        res = subprocess.run(cmd, cwd=cwd, shell=True, text=True, capture_output=True, check=True)
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Git command failed: {cmd} - {e.stderr}")
        return ""

def node_context_manager(state: AgentState):
    print("--- NODE 1: Git Context Manager ---")
    execute_git("git fetch origin")
    execute_git(f"git checkout {BRANCH}")
    execute_git(f"git pull origin {BRANCH}")
    return state

def node_solution_mapper(state: AgentState):
    print("--- NODE 2: Solution Mapper ---")
    sln_full = os.path.join(REPO_PATH, SLN_PATH)
    valid_dirs = []
    
    if os.path.exists(sln_full):
        with open(sln_full, "r", encoding="utf-8-sig") as f:
            for line in f:
                if line.startswith("Project("):
                    # Project("{...}") = "Name", "Path\To\Proj.csproj", "{...}"
                    parts = line.split(",")
                    if len(parts) >= 2:
                        relative_csproj = parts[1].strip().strip('"')
                        if relative_csproj.endswith(".csproj"):
                            proj_dir = os.path.dirname(relative_csproj).replace("\\", "/")
                            valid_dirs.append(proj_dir)
    else:
        print(f"Warning: SLN at {sln_full} not found.")

    # Always ensure MAUI and DROID are preserved as asked initially
    essential_dirs = ["TCPOS.Droid.", "TCPOS.Maui.Embedding", "TCPOS.Maui.Views"]
    for ed in essential_dirs:
        if not any(ed in vd for vd in valid_dirs):
            valid_dirs.append(ed)

    return {"valid_project_dirs": valid_dirs}

def is_valid_file(filepath: str, valid_dirs: List[str]) -> bool:
    if not filepath.endswith(".cs"): return False
    if filepath.endswith(".designer.cs") or filepath.endswith(".resx") or ".g." in filepath: return False
    
    # Check if the file is inside any of the valid project dirs
    filepath = filepath.replace("\\", "/")
    for d in valid_dirs:
        if d in filepath:
            return True
    return False

def node_commit_filter(state: AgentState):
    print("--- NODE 3: History & Commit Filter ---")
    valid_dirs = state["valid_project_dirs"]
    
    # 1. Get PR Merge commits
    merges_out = execute_git(f"git log origin/{BRANCH} --merges --first-parent --pretty=format:\"%H\"")
    pr_hashes = merges_out.split("\n") if merges_out else []
    
    valid_commits_files = {}

    # For sanity and testability, let's limit processing to the latest 5 PRs
    # In full prod, remove the [:5] slicing
    for pr in pr_hashes[:5]:
        if not pr: continue
        
        # Get parents of PR
        parents_out = execute_git(f"git show -s --format=%P {pr}")
        parents = parents_out.split()
        if len(parents) < 2: continue
        base_commit, branch_commit = parents[0], parents[1]
        
        # Files changed in PR
        changed_pr_files = execute_git(f"git diff --name-only {base_commit}...{branch_commit}").split("\n")
        
        # If PR hits our projects, process individual commits
        if any(is_valid_file(f, valid_dirs) for f in changed_pr_files if f):
            commits_in_pr = execute_git(f"git log --pretty=format:\"%H\" {base_commit}..{branch_commit}").split("\n")
            
            for cHash in commits_in_pr:
                if not cHash: continue
                changed_c_files = execute_git(f"git show --name-only --format=\"\" {cHash}").split("\n")
                valid_f = [f for f in changed_c_files if f and is_valid_file(f, valid_dirs)]
                
                if valid_f:
                    valid_commits_files[cHash] = valid_f

    return {"valid_commits_files": valid_commits_files}

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

def node_roslyn_preprocessor(state: AgentState):
    print("--- NODE 4: Roslyn Preprocessor ---")
    valid_map = state["valid_commits_files"]
    cleaned_commits_data = []
    
    # Only if there's work to do
    if not valid_map:
        return {"cleaned_commits_data": []}
        
    roslyn_exe = prepare_roslyn_tool()
    # On non-windows, this might be a dll 'dotnet run ...' or just `./roslyn_tool`
    # Let's use dotnet run from the directory to be OS-agnostic instead of forcing .exe execution
    tool_dir = os.path.abspath("./roslyn_tool")
    
    for cHash, files in valid_map.items():
        commit_msg = execute_git(f"git show -s --format=%B {cHash}")
        
        for f in files:
            # Get file content at that commit
            file_content = execute_git(f"git show {cHash}:{f}")
            if not file_content: continue
            
            # Save temporarily
            tmp_path = os.path.abspath("./temp_code.cs")
            with open(tmp_path, "w", encoding="utf-8") as tmpF:
                tmpF.write(file_content)
                
            # Run Roslyn AST tool
            try:
                # Use dotnet run in project folder to be cross platform
                res = subprocess.run(f"dotnet run -c Release -- \"{tmp_path}\"", cwd=tool_dir, shell=True, text=True, capture_output=True, check=True)
                cleaned_text = res.stdout
                
                if cleaned_text.strip():
                    cleaned_commits_data.append({
                        "commit": cHash,
                        "msg": commit_msg,
                        "file": f,
                        "content": cleaned_text
                    })
            except subprocess.CalledProcessError as e:
                print(f"Roslyn Error on {f}: {e.stderr}")
                
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
                
    return {"cleaned_commits_data": cleaned_commits_data}

class FileChunks(BaseModel):
    should_split: bool = Field(description="True if the code is large and contains multiple distinct semantic logical blocks that should be separated.")
    chunks: List[str] = Field(description="The source code separated into logical, cohesive parts (methods, related classes). If should_split is false, this should just contain one item with the original text.")

def node_llm_chunker(state: AgentState):
    print("--- NODE 5: LLM-Assisted Semantic Chunker ---")
    
    # 1. Initialize Local Model (This happens once inside the node for simplicity here, 
    # but in production you might cache this globally)
    try:
        model_id = LLM_MODEL
        print(f"Loading local model: {model_id}...")
        
        # Configure quantization for mid-level PC
        bnb_config = None
        if config["llm"].get("quantization") == "4bit":
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16
            )

        tokenizer = AutoTokenizer.from_pretrained(model_id)
        model = AutoModelForCausalLM.from_pretrained(
            model_id,
            quantization_config=bnb_config,
            device_map=config["llm"].get("device", "auto"),
            trust_remote_code=True
        )
        
        hf_pipeline = pipeline(
            "text-generation",
            model=model,
            tokenizer=tokenizer,
            max_new_tokens=2048,
            temperature=0.1,
            top_p=0.95,
            repetition_penalty=1.1,
            do_sample=True,
            pad_token_id=tokenizer.eos_token_id
        )
        
        llm = HuggingFacePipeline(pipeline=hf_pipeline)
    except Exception as e:
        print("LLM Error Initialize (Make sure transformers/bitsandbytes are installed):", e)
        return {"chunked_data": [{"text": d["content"], "commit": d["commit"], "file": d["file"]} for d in state["cleaned_commits_data"]]}
    
    parser = JsonOutputParser(pydantic_object=FileChunks)

    prompt = ChatPromptTemplate.from_messages([
        ("system", f"You are an expert C# refactoring and data engineering agent. Your job is to semantically chunk source files into coherent pieces for fine-tuning.\n{parser.get_format_instructions()}"),
        ("user", "Assess the following C# code and chunk it according to the rules. Ensure total chunk size is around {max_size} chars:\n\n{code}")
    ]).partial(max_size=MAX_CHUNK_SIZE)
    
    chain = prompt | llm | parser
    
    chunked_data = []
    
    for item in state.get("cleaned_commits_data", []):
        content = item["content"]
        if len(content) < MAX_CHUNK_SIZE // 2:
            # Optimization: Very small files don't need LLM processing
            chunked_data.append({
                "text": content,
                "commit": item["commit"],
                "file": item["file"]
            })
            continue
            
        # Run through LLM
        try:
            res: FileChunks = chain.invoke({"code": content[:40000]}) # Prevent massive context blowout
            if res.should_split and res.chunks:
                for c in res.chunks:
                    chunked_data.append({"text": c, "commit": item["commit"], "file": item["file"]})
            else:
                 chunked_data.append({"text": content, "commit": item["commit"], "file": item["file"]})
        except Exception as e:
            print(f"LLM Chunking failed for {item['file']}, keeping whole code: {e}")
            chunked_data.append({"text": content, "commit": item["commit"], "file": item["file"]})
            
    return {"chunked_data": chunked_data}

def node_json_exporter(state: AgentState):
    print("--- NODE 6: JSON Exporter ---")
    data = state.get("chunked_data", [])
    
    out_dir = os.path.dirname(OUTPUT_FILE)
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
        
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
        print(f"Exported {len(data)} chunks to {OUTPUT_FILE}")
        
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
    print("Starting LangGraph Preprocessor Agent...")
    result = app.invoke({
        "valid_project_dirs": [],
        "valid_commits_files": {},
        "cleaned_commits_data": [],
        "chunked_data": []
    })
    print("Pipeline Finished!")
