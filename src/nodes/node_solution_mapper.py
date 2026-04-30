import os
from src.shared.shared_constants import REPO_PATH, SLN_PATH, logger

def node_solution_mapper(state):
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
