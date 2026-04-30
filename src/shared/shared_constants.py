import os
import yaml
import logging
from datetime import datetime
from dotenv import load_dotenv

# Load .env file
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
logger = logging.getLogger("DroidAgent")

# 2. Global Sentinel
SENTINEL = "===END_OF_CODE==="

# 3. Load config
config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
with open(config_path, "r") as f:
    config = yaml.safe_load(f)

REPO_PATH = config["repo"]["path"]
SLN_PATH = config["repo"]["solution_path"]
BRANCH = config["repo"]["target_branch"]
MAX_PRS = config["repo"].get("max_prs", 10)

LLM_MODEL = config["llm"].get("model_name", "gpt-4o-mini")
MAX_CHUNK_LENGTH = config["llm"].get("max_chunk_length", 4000)
ENABLE_INTENT_DISAGGREGATION = config["llm"].get("enable_intent_disaggregation", False)
ENRICH_DATA = config["llm"].get("enrich_data", False)

# Output Setup
out_dir = os.path.dirname(config["output"]["file_path"])
OUTPUT_FILE = os.path.join(out_dir, f"agent_run_{run_timestamp}.json")
