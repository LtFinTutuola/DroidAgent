from typing import TypedDict, List, Dict

class AgentState(TypedDict):
    valid_project_dirs: List[str]
    commits: List[Dict] # Flat: [{commit_hash, commit_description, files_to_process: []}]