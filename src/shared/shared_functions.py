import os
import re
import subprocess
import json
import difflib
from typing import List, Dict, Tuple
from openai import OpenAI
from src.shared.shared_constants import REPO_PATH, LLM_MODEL, logger, MAX_CHUNK_LENGTH

# Lazy singletons
_OPENAI_CLIENT = None
_ROSLYN_SERVER = None
_GIT_BATCHER = None

def get_openai_client() -> OpenAI:
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is None:
        _OPENAI_CLIENT = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    return _OPENAI_CLIENT

def get_roslyn_server():
    global _ROSLYN_SERVER
    if _ROSLYN_SERVER is None:
        from src.classes.roslyn_server import RoslynServer
        tool_dir = os.path.abspath("./roslyn_tool")
        _ROSLYN_SERVER = RoslynServer(tool_dir)
    return _ROSLYN_SERVER

def get_git_batcher():
    global _GIT_BATCHER
    if _GIT_BATCHER is None:
        from src.classes.git_batcher import GitBatcher
        _GIT_BATCHER = GitBatcher(REPO_PATH)
    return _GIT_BATCHER

def execute_git(cmd: str, cwd: str = REPO_PATH, check=True) -> str:
    logger.info(f"Executing Git: {cmd}")
    try:
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

def _parse_llm_json(response_str: str, default):
    if not response_str:
        return default
    for attempt in [response_str, re.sub(r'^```[\w]*\n?|\n?```$', '', response_str.strip())]:
        try:
            return json.loads(attempt)
        except Exception:
            continue
    match = re.search(r'(\{.*\}|\[.*\])', response_str, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass
    logger.warning(f"_parse_llm_json: could not parse response: {response_str[:120]}")
    return default

def get_diff_char_count(clean_old: str, clean_new: str) -> int:
    if not clean_old and not clean_new: 
        return 0
    str_old = re.sub(r'\s+', '', clean_old)
    str_new = re.sub(r'\s+', '', clean_new)
    if str_old == str_new: 
        return 0
    diff = difflib.ndiff(str_old, str_new)
    changed_chars = sum(1 for d in diff if d.startswith('+ ') or d.startswith('- '))
    return changed_chars

def minify_code(text: str) -> str:
    if not text: return text
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'(?m)^[ \t]+', '', text)
    return text.strip()

# Unused but kept as requested
def disaggregate_commit_intent(commit: Dict) -> None:
    from src.shared.shared_constants import ENABLE_INTENT_DISAGGREGATION
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
                        if changed_chars < 10:
                            chunk_data["manual_review"] = True
                        expanded.append(chunk_data)
                    continue
            except Exception as e:
                logger.error(f"Phase C split failed: {e}")
        expanded.append(diff)
    file_obj["file_diffs"] = expanded

def _xml_extract_parent_block(xml_text: str, search_text: str) -> str:
    if not search_text or not xml_text:
        return ""
    idx = xml_text.find(search_text)
    if idx == -1:
        first = next((ln.strip() for ln in search_text.splitlines() if ln.strip()), "")
        idx = xml_text.find(first)
    if idx == -1:
        return xml_text[:2000]
    start = xml_text.rfind('<', 0, idx)
    while start > 0 and xml_text[start + 1] in ('!', '?', '/'):
        start = xml_text.rfind('<', 0, start)
    if start == -1:
        return xml_text[:2000]
    tag_end = start + 1
    while tag_end < len(xml_text) and xml_text[tag_end] not in (' ', '\t', '\n', '\r', '>'):
        tag_end += 1
    tag_name = xml_text[start + 1:tag_end]
    if not tag_name:
        return xml_text[:2000]
    close_tag = f"</{tag_name}>"
    end = xml_text.find(close_tag, idx)
    if end == -1:
        end = min(start + 3000, len(xml_text))
    else:
        end += len(close_tag)
    return xml_text[start:end]
