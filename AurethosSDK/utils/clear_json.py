import json
import re
from typing import Optional

_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_FENCE_ANY_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*([\s\S]*?)\s*```")


def _fix_trailing_commas(s: str) -> str:
    """Remove commas that trail before ``}`` or ``]`` — a common LLM slip."""
    return _TRAILING_COMMA_RE.sub(r"\1", s)


def _find_first_json_object(s: str) -> Optional[dict]:
    """Scan for the first `{...}` block that parses as a valid JSON object.

    Ignores prose before/after and inside strings (raw_decode handles quoting).
    """
    decoder = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(s[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def robust_parse_json(raw: str) -> Optional[dict]:
    """Best-effort JSON-object extraction from LLM output.

    Tries in order:
      1. Direct ``json.loads``.
      2. Strip ``---`` frontmatter/ad separator, retry direct load.
      3. Fenced ``` blocks (with or without lang tag).
      4. Balanced ``{...}`` scan anywhere in the text.
      5. Trailing-comma sanitizer, then re-run steps 1 & 4.
      6. Give up → return None.

    Never raises. Callers must handle None.
    """
    if not raw or not isinstance(raw, str):
        return None

    def _try(s: str) -> Optional[dict]:
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    if (obj := _try(raw)) is not None:
        return obj

    stripped = re.split(r"(?m)^[ \t]*-{3,}[ \t]*$", raw, maxsplit=1)[0]
    if stripped != raw and (obj := _try(stripped)) is not None:
        return obj

    for match in _FENCE_ANY_RE.finditer(raw):
        candidate = match.group(1).strip()
        if (obj := _try(candidate)) is not None:
            return obj
        if (obj := _try(_fix_trailing_commas(candidate))) is not None:
            return obj

    if (obj := _find_first_json_object(raw)) is not None:
        return obj

    fixed = _fix_trailing_commas(raw)
    if fixed != raw:
        if (obj := _try(fixed)) is not None:
            return obj
        if (obj := _find_first_json_object(fixed)) is not None:
            return obj

    return None


def clean_markdown_in_json_blocks(content: str) -> str:
    def clean_json_block(match):
        raw_json = match.group(1).strip()
        try:
            lines = raw_json.splitlines()
            clean_lines = []
            for line in lines:
                line = re.sub(r"^\s*[*>\-`]+", "", line)
                line = re.sub(r"//.*", "", line)
                line = re.sub(r"#.*", "", line)
                clean_lines.append(line)
            cleaned_raw = "\n".join(clean_lines).strip()
            parsed = json.loads(cleaned_raw)
            return (
                "```json\n" + json.dumps(parsed, indent=2, ensure_ascii=False) + "\n```"
            )
        except Exception:
            return match.group(0)

    return re.sub(r"```json\s*([\s\S]*?)\s*```", clean_json_block, content)


clear_trash = lambda text: text.replace("`", "").replace("json", "").strip()


def extract_first_json_from_markdown(md: str) -> Optional[dict]:
    """Backward-compatible entry point — delegates to the robust parser."""
    return robust_parse_json(md)


def clear_html_block(content: str, block_name: str = "think") -> str:
    block_name = re.escape(block_name)
    pattern = rf"^<{block_name}>.*?</{block_name}>\s*"
    return re.sub(pattern, "", content, flags=re.DOTALL)


def has_multiple_top_level_jsons(content: str) -> bool:
    """
    Check if string is containing multiple json blocks

    Args:
        text (str): string content

    Returns:
        bool: True if string has more then 1 json block, False if not
    """
    decoder = json.JSONDecoder()
    pos = 0
    json_count = 0

    while pos < len(content):
        try:
            # Skip whitespace before checking for JSON
            pos = (
                json.decoder.scanstring(content, pos)[1]
                if content[pos].isspace()
                else pos
            )

            # Try to parse a JSON object
            obj, new_pos = decoder.raw_decode(content[pos:])
            json_count += 1
            pos += new_pos

            # If we found more than one, return True
            if json_count > 1:
                return True
        except (json.JSONDecodeError, ValueError):
            # Move to next character if parsing fails
            pos += 1

    return False
