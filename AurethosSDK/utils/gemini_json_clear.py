import json
import re
import textwrap
from typing import Any, Callable, Dict, List, Optional, TypeVar, Union

T = TypeVar("T")


class MarkdownJSONError(Exception):
    """Custom exception thrown when JSON extraction/parsing from Markdown fails."""

    pass


def extract_fenced_blocks(
    text: str,
    fence_language: str = "json",
    fence_char: str = "`",
    flags: int = re.IGNORECASE | re.MULTILINE | re.DOTALL,
) -> List[str]:
    start_esc = re.escape(fence_char)
    lang_esc = re.escape(fence_language)

    pattern = rf"""
        (?P<fence> {start_esc}{{3,}} )      # Fence: 3 or more backticks
        [ \t]*
        (?P<lang> {lang_esc} )             # Language identifier (json)
        [ \t]* \r?\n                       # Newline after language
        (?P<content> .*? )                 # Block content (non-greedy)
        (?P=fence) [ \t]*(?:\r?\n|$)       # Closing fence
    """
    regex = re.compile(pattern, flags | re.VERBOSE)
    return [m.group("content") for m in regex.finditer(text)]


def escape_control_characters(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace('"', '\\"')
    )


def recursive_escape(obj):
    if isinstance(obj, str):
        return escape_control_characters(obj)
    elif isinstance(obj, list):
        return [recursive_escape(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: recursive_escape(v) for k, v in obj.items()}
    return obj


def safe_json_load(raw: str) -> Any:
    """
    Parses a raw JSON string into a Python object.
    On failure, retries with escaped content.
    If still fails, raises MarkdownJSONError with context around the error position.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        try:
            print(
                f"[safe_json_load] JSONDecodeError, retrying with escaped content. Reason: {e}"
            )
            fallback = json.loads(escape_control_characters(raw))
            return fallback
        except Exception as esc_error:
            pos = e.pos
            start = max(0, pos - 30)
            end = min(len(raw), pos + 30)
            context = raw[start:end]
            raise MarkdownJSONError(
                f"Initial JSONDecodeError: {e.msg} @ pos {pos}.\n"
                f"Context (chars {start}-{end}):\n{repr(context)}\n"
                f"Failed to recover: {esc_error}"
            ) from e


def find_top_level_json_end(s: str) -> int:
    in_string = False
    escape = False
    stack = []
    start_char = None

    for i, ch in enumerate(s):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch in ["{", "["]:
                stack.append(ch)
                if start_char is None:
                    start_char = ch
            elif ch in ["}", "]"]:
                if not stack:
                    raise MarkdownJSONError(f"Unexpected closing {ch} at position {i}")
                opening = stack.pop()
                if (opening == "{" and ch != "}") or (opening == "[" and ch != "]"):
                    raise MarkdownJSONError(
                        f"Mismatched {opening} and {ch} at position {i}"
                    )
                if not stack:
                    return i
    raise MarkdownJSONError("No matching closing brace/bracket found for JSON content")


def load_json_from_markdown(
    text: str,
    *,
    fence_language: str = "json",
    fence_char: str = "`",
    return_all: bool = False,
    validator: Optional[Callable[[Any], Union[bool, None]]] = None,
) -> Union[Dict, List[Dict]]:
    try:
        return json.loads(text)
    except:
        raw_blocks = extract_fenced_blocks(text, fence_language, fence_char)
        if not raw_blocks:
            json_start = min(
                [i for i in (text.find("{"), text.find("[")) if i != -1],
                default=None,
            )
            if json_start is None:
                raise MarkdownJSONError(
                    f"No fenced block or inline JSON object/array found in the text."
                )

            try:
                json_candidate = text[json_start:]
                end_idx = find_top_level_json_end(json_candidate)
                json_content = json_candidate[: end_idx + 1]
                parsed = safe_json_load(json_content)
                raw_blocks = [json_content]
            except Exception as e:
                raise MarkdownJSONError(
                    f"Failed to parse inline JSON in plain text.\n{e}"
                ) from e

        parsed_list: List[Dict] = []
        for idx, raw in enumerate(raw_blocks):
            clean = textwrap.dedent(raw).lstrip()
            end_idx = find_top_level_json_end(clean)
            json_content = clean[: end_idx + 1]
            parsed = safe_json_load(json_content)

            if validator:
                try:
                    valid_result = validator(parsed)
                    if valid_result is False:
                        raise MarkdownJSONError(
                            f"Validator returned False for block #{idx}"
                        )
                except Exception as ve:
                    raise MarkdownJSONError(
                        f"Validator exception for block #{idx}: {ve}"
                    ) from ve

            parsed_list.append(parsed)
            if not return_all:
                break

        return parsed_list if return_all else parsed_list[0]
