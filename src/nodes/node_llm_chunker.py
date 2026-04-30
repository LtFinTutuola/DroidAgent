from src.shared.shared_constants import logger, ENRICH_DATA, LLM_MODEL, MAX_CHUNK_LENGTH
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


def _expand_and_enrich(client, commit_description: str, file_obj: dict) -> None:
    """
    Mutates file_obj["file_diffs"] in-place:
    - Diffs whose (raw_old + raw_new) length > MAX_CHUNK_LENGTH are discarded (Hard Drop).
    - Diffs within the threshold are enriched directly.
    - Each resulting diff dict gets a "diff_explain" key.
    """
    file_name = file_obj.get("file_name", "")
    final_diffs = []

    for diff in file_obj.get("file_diffs", []):
        raw_old = diff.get("raw_old_code", "") or ""
        raw_new = diff.get("raw_new_code", "") or ""
        diff_length = len(raw_old) + len(raw_new)

        if diff_length > MAX_CHUNK_LENGTH:
            logger.warning(
                f"Discarding diff in {file_name}: length ({diff_length}) exceeds MAX_CHUNK_LENGTH ({MAX_CHUNK_LENGTH})."
            )
            continue

        try:
            diff["diff_explain"] = generate_explanation(
                client, commit_description, file_name, diff
            )
            final_diffs.append(diff)
        except Exception as e:
            logger.error(f"generate_explanation failed for '{file_name}': {e}")
            diff["diff_explain"] = ""
            final_diffs.append(diff)

    file_obj["file_diffs"] = final_diffs


def node_llm_chunker(state):
    if not ENRICH_DATA:
        logger.info("--- NODE 5: LLM Chunker (Passthrough) ---")
        return state

    logger.info("--- NODE 5: LLM Chunker (Enrichment with Hard Drop) ---")
    commits = state.get("commits", [])
    client = get_openai_client()

    total_files = sum(len(c.get("files", [])) for c in commits)

    with tqdm(total=total_files, desc="Enriching files", unit="file") as pbar:
        for commit in commits:
            commit_description = commit.get("commit_description", "")
            for file_obj in commit.get("files", []):
                _expand_and_enrich(client, commit_description, file_obj)
                pbar.update(1)

    total_diffs = sum(
        len(f.get("file_diffs", []))
        for c in commits
        for f in c.get("files", [])
    )
    logger.info(f"Enrichment complete. Total diffs remaining after hard drop: {total_diffs}")
    return state
