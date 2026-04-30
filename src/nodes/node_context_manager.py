from src.shared.shared_constants import REPO_PATH, BRANCH, logger
from src.shared.shared_functions import execute_git
import os
import subprocess

def node_context_manager(state):
    logger.info("--- NODE 1: Git Context Manager ---")
    logger.info(f"Targeting Repository: {REPO_PATH}")
    
    lock_file = os.path.join(REPO_PATH, ".git", "index.lock")
    if os.path.exists(lock_file):
        logger.info("Removing stale git index lock...")
        try: os.remove(lock_file)
        except: pass
    
    status = execute_git("git status --porcelain", check=False)
    if status:
        logger.info("Local changes detected. Stashing...")
        try:
            execute_git("git stash", check=True)
        except subprocess.CalledProcessError:
            logger.warning("Git stash failed. Forcing reset instead...")
    
    execute_git("git reset --hard HEAD", check=True)
    execute_git("git clean -fd", check=True)
    
    execute_git("git fetch origin", check=True)
    logger.info(f"Checking out branch: {BRANCH}")
    execute_git(f"git checkout {BRANCH}", check=True)
    execute_git(f"git pull origin {BRANCH}", check=True)
    
    logger.info("Context Manager Finished.")
    return state
