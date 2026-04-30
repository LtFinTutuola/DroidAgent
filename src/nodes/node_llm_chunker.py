from src.shared.shared_constants import logger, ENRICH_DATA, LLM_MODEL
from src.shared.shared_functions import get_openai_client
from src.prompt_templates.enrichment_prompts import SYSTEM_PROMPT, USER_PROMPT_CSHARP, USER_PROMPT_XAML, USER_PROMPT_CSPROJ
from tqdm import tqdm

def generate_explanation(client, commit_description: str, file_name: str, diff: dict) -> str:
    raw_old = diff.get("raw_old_code", "") or ""
    raw_new = diff.get("raw_new_code", "") or ""

    if file_name.endswith(".xaml"):
        prompt_template = USER_PROMPT_XAML
    elif file_name.endswith(".csproj"):
        prompt_template = USER_PROMPT_CSPROJ
    else:
        prompt_template = USER_PROMPT_CSHARP

    user_msg = prompt_template.format(
        commit_description=commit_description,
        file_name=file_name,
        raw_old_code=raw_old,
        raw_new_code=raw_new,
    )

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        max_tokens=512,
        temperature=0.2,
    )
    return response.choices[0].message.content.strip()

def node_llm_chunker(state):
    if not ENRICH_DATA:
        logger.info("--- NODE 5: LLM Chunker (Passthrough) ---")
        return state

    logger.info("--- NODE 5: LLM Chunker (Enrichment) ---")
    commits = state.get("commits", [])
    client = get_openai_client()
    
    total_diffs = sum(len(f.get("file_diffs", [])) for c in commits for f in c.get("files", []))
    
    errors = 0
    with tqdm(total=total_diffs, desc="Enriching diffs", unit="diff") as pbar:
        for commit in commits:
            commit_description = commit.get("commit_description", "")
            for file_obj in commit.get("files", []):
                file_name = file_obj.get("file_name", "")
                for diff in file_obj.get("file_diffs", []):
                    try:
                        explanation = generate_explanation(client, commit_description, file_name, diff)
                        diff["diff_explain"] = explanation
                    except Exception as e:
                        errors += 1
                        logger.error(f"Error on diff in {file_name}: {e}")
                        diff["diff_explain"] = ""
                    finally:
                        pbar.update(1)

    logger.info(f"Enrichment complete. Errors: {errors}/{total_diffs}")
    return state
