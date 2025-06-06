#!/usr/bin/env python3

import difflib
import hashlib
import logging
import math
import os
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from ..code_command import run_formatter_without_commit
from ..common import get_edit_snippet, normalize_file_path
from ..file_utils import (
    async_open_text,
    check_file_path_and_permissions,
    check_git_tracking_for_existing_file,
    write_text_content,
)
from ..git import commit_changes
from ..line_endings import detect_line_endings
from ..mcp import mcp
from .commit_utils import append_commit_hash

# Set up logger
logger = logging.getLogger(__name__)

__all__ = [
    "edit_file",
    "find_similar_file",
    "apply_edit_pure",
]


def find_similar_file(file_path: str) -> str | None:
    """Find a similar file with a different extension.

    Args:
        file_path: The path to the file

    Returns:
        The path to a similar file, or None if none found

    """
    # Import normalize_file_path for tilde expansion
    from ..common import normalize_file_path

    # Normalize the path with tilde expansion
    file_path = normalize_file_path(file_path)

    # Simple implementation - in a real app, would check for files with different extensions
    directory = os.path.dirname(file_path)
    if not os.path.exists(directory):
        return None

    base_name = os.path.splitext(os.path.basename(file_path))[0]
    for f in os.listdir(directory):
        if f.startswith(base_name + ".") and f != os.path.basename(file_path):
            return os.path.join(directory, f)
    return None


def apply_edit_pure(
    content: str,
    old_string: str,
    new_string: str,
) -> Tuple[List[Dict[str, Any]], str, Optional[str]]:
    """Apply an edit to content using robust matching strategies.

    Args:
        content: The original content
        old_string: The text to replace
        new_string: The text to replace it with

    Returns:
        A tuple of (patch, updated_content, error_message)
        error_message is None if the edit was successful, otherwise it contains an error message

    """
    # For creating a new file, just return the new content
    if not old_string.strip():
        updated_file = new_string
        old_lines: List[str] = []
        new_lines = new_string.split("\n")

        # Create a simple patch structure
        result_patch: List[Dict[str, Any]] = [
            {
                "oldStart": 1,
                "oldLines": 0,
                "newStart": 1,
                "newLines": len(new_lines),
                "lines": [f"+{line}" for line in new_lines],
            },
        ]

        return result_patch, updated_file, None

    # First try direct replacement (most common case and efficient)
    if old_string in content:
        # Check for uniqueness of old_string before applying the replacement
        # We need to check this to avoid ambiguity in matches
        if content.count(old_string) > 1:
            # First try to use the dotdotdots approach which handles multiple matches by context
            try:
                test_result = try_dotdotdots(content, old_string, new_string)
                if test_result:
                    # If it worked with dotdotdots, we're good to proceed
                    logger.debug(
                        "Successfully used dotdotdots strategy to handle multiple occurrences",
                    )
                    # Return the result from dotdotdots
                    updated_file = test_result

                    # Create a useful diff/patch structure for dotdotdots result
                    dotdot_patch: List[Dict[str, Any]] = []
                    if (
                        content != updated_file
                    ):  # Only create a patch if there were actual changes
                        old_lines = old_string.split("\n")
                        new_lines = new_string.split("\n")

                        # Try to find the line number where the change occurs
                        try:
                            # This is a simplification; for exact matches this works,
                            # but for fuzzy matches we would need a more sophisticated approach
                            before_text = content.split(old_string)[0]
                            line_num = before_text.count("\n")
                        except Exception:
                            # Fallback: just say it's at the start of the file
                            line_num = 0

                        dotdot_patch.append(
                            {
                                "oldStart": line_num + 1,
                                "oldLines": len(old_lines),
                                "newStart": line_num + 1,
                                "newLines": len(new_lines),
                                "lines": [f"-{line}" for line in old_lines]
                                + [f"+{line}" for line in new_lines],
                            },
                        )

                    return dotdot_patch, updated_file, None
                else:
                    # Fall back to the original error message
                    matches = content.count(old_string)
                    return (
                        [],
                        content,
                        f"Found {matches} matches of the string to replace. For safety, this tool only supports replacing exactly one occurrence at a time. Add more lines of context to your edit and try again.",
                    )
            except ValueError:
                # If dotdotdots approach failed, give the original error message
                matches = content.count(old_string)
                return (
                    [],
                    content,
                    f"Found {matches} matches of the string to replace. For safety, this tool only supports replacing exactly one occurrence at a time. Add more lines of context to your edit and try again.",
                )

        # If we get here, there's only one occurrence of old_string in content
        updated_file = content.replace(old_string, new_string, 1)

        # Create a useful diff/patch structure
        diff_patch: List[Dict[str, Any]] = []
        if content != updated_file:  # Only create a patch if there were actual changes
            old_lines = old_string.split("\n")
            new_lines = new_string.split("\n")

            # Try to find the line number where the change occurs
            try:
                # This is a simplification; for exact matches this works,
                # but for fuzzy matches we would need a more sophisticated approach
                before_text = content.split(old_string)[0]
                line_num = before_text.count("\n")
            except Exception:
                # Fallback: just say it's at the start of the file
                line_num = 0

            diff_patch.append(
                {
                    "oldStart": line_num + 1,
                    "oldLines": len(old_lines),
                    "newStart": line_num + 1,
                    "newLines": len(new_lines),
                    "lines": [f"-{line}" for line in old_lines]
                    + [f"+{line}" for line in new_lines],
                },
            )

        return diff_patch, updated_file, None

    # Try with trailing whitespace stripped from each line
    content_lines = content.splitlines()
    old_lines = old_string.splitlines()

    # Check if we can find a match ignoring trailing whitespace
    content_lines_stripped = [line.rstrip() for line in content_lines]
    old_lines_stripped = [line.rstrip() for line in old_lines]

    old_text_stripped = "\n".join(old_lines_stripped)
    content_stripped = "\n".join(content_lines_stripped)

    if old_text_stripped in content_stripped:
        # Find the position in the stripped content
        start_pos = content_stripped.find(old_text_stripped)

        # Count newlines to find the line number
        line_num = content_stripped[:start_pos].count("\n")

        # Replace those lines with the new content
        result_lines = (
            content_lines[:line_num]
            + new_string.splitlines()
            + content_lines[line_num + len(old_lines) :]
        )
        updated_file = "\n".join(result_lines)
        if not content.endswith("\n") and updated_file.endswith("\n"):
            updated_file = updated_file[:-1]
        elif content.endswith("\n") and not updated_file.endswith("\n"):
            updated_file += "\n"

        # Create a useful diff/patch structure
        whitespace_patch: List[Dict[str, Any]] = []
        if content != updated_file:  # Only create a patch if there were actual changes
            # Try to find the line number where the change occurs
            whitespace_patch.append(
                {
                    "oldStart": line_num + 1,
                    "oldLines": len(old_lines),
                    "newStart": line_num + 1,
                    "newLines": len(new_string.splitlines()),
                    "lines": [f"-{line}" for line in old_lines]
                    + [f"+{line}" for line in new_string.splitlines()],
                },
            )

        return whitespace_patch, updated_file, None
    else:
        logger.debug("All matching techniques failed. No changes made.")
        return [], content, "String to replace not found in file."


async def apply_edit(
    file_path: str,
    old_string: str,
    new_string: str,
) -> Tuple[List[Dict[str, Any]], str, Optional[str]]:
    """Apply an edit to a file using robust matching strategies.

    Args:
        file_path: The path to the file
        old_string: The text to replace
        new_string: The text to replace it with

    Returns:
        A tuple of (patch, updated_file, error_message)
        error_message is None if the edit was successful, otherwise it contains an error message

    """
    # Import normalize_file_path for tilde expansion
    from ..common import normalize_file_path

    # Normalize the path with tilde expansion
    file_path = normalize_file_path(file_path)

    if os.path.exists(file_path):
        content = await async_open_text(file_path, encoding="utf-8")
    else:
        content = ""

    return apply_edit_pure(content, old_string, new_string)


def prep(content: str) -> tuple[str, list[str]]:
    """Prepare content for comparison by ensuring it ends with a newline
    and splitting into lines with preserved line endings.

    Args:
        content: Text content to prepare

    Returns:
        Tuple of (normalized content, list of lines with line endings)

    """
    if content and not content.endswith("\n"):
        content += "\n"
    lines = content.splitlines(keepends=True)
    return content, lines


def perfect_or_whitespace(
    whole_lines: list[str],
    part_lines: list[str],
    replace_lines: list[str],
) -> str | None:
    """Try perfect match first, then try with whitespace flexibility.

    Args:
        whole_lines: Original file lines with line endings
        part_lines: Lines to find/replace with line endings
        replace_lines: Replacement lines with line endings

    Returns:
        Updated content if a match was found, None otherwise

    """
    # Try for a perfect match
    res = perfect_replace(whole_lines, part_lines, replace_lines)
    if res:
        return res

    # Try being flexible about leading whitespace
    res = replace_part_with_missing_leading_whitespace(
        whole_lines,
        part_lines,
        replace_lines,
    )
    if res:
        return res

    return None


def perfect_replace(
    whole_lines: list[str],
    part_lines: list[str],
    replace_lines: list[str],
) -> str | None:
    """Find an exact match of part_lines in whole_lines and replace with replace_lines.

    Args:
        whole_lines: Original file lines with line endings
        part_lines: Lines to find/replace with line endings
        replace_lines: Replacement lines with line endings

    Returns:
        Updated content if a perfect match was found, None otherwise

    """
    part_tup = tuple(part_lines)
    part_len = len(part_lines)

    for i in range(len(whole_lines) - part_len + 1):
        whole_tup = tuple(whole_lines[i : i + part_len])
        whole_tup_stripped = tuple(
            line.rstrip() for line in whole_lines[i : i + part_len]
        )
        part_tup_stripped = tuple(line.rstrip() for line in part_lines)
        if part_tup == whole_tup or part_tup_stripped == whole_tup_stripped:
            res = whole_lines[:i] + replace_lines + whole_lines[i + part_len :]
            return "".join(res)

    return None


def match_but_for_leading_whitespace(
    whole_lines: list[str],
    part_lines: list[str],
) -> str | None:
    """Check if lines match except for consistent leading whitespace.

    Args:
        whole_lines: Original file lines subset
        part_lines: Lines to find/replace

    Returns:
        The consistent leading whitespace prefix to be added, or None if no match

    """
    num = len(whole_lines)

    # does the non-whitespace all agree?
    if not all(whole_lines[i].lstrip() == part_lines[i].lstrip() for i in range(num)):
        return None

    # are they all offset the same?
    add = set(
        whole_lines[i][: len(whole_lines[i]) - len(part_lines[i])]
        for i in range(num)
        if whole_lines[i].strip()
    )

    if len(add) != 1:
        return None

    return add.pop()


def replace_part_with_missing_leading_whitespace(
    whole_lines: list[str],
    part_lines: list[str],
    replace_lines: list[str],
) -> str | None:
    """Handle case where search text is missing the exact leading whitespace.

    Args:
        whole_lines: Original file lines with line endings
        part_lines: Lines to find/replace with line endings
        replace_lines: Replacement lines with line endings

    Returns:
        Updated content if match was found after whitespace normalization, None otherwise

    """
    # Outdent everything in part_lines and replace_lines by the max fixed amount possible
    leading = [len(p) - len(p.lstrip()) for p in part_lines if p.strip()] + [
        len(p) - len(p.lstrip()) for p in replace_lines if p.strip()
    ]

    if leading and min(leading):
        num_leading = min(leading)
        part_lines = [p[num_leading:] if p.strip() else p for p in part_lines]
        replace_lines = [p[num_leading:] if p.strip() else p for p in replace_lines]

    # can we find an exact match not including the leading whitespace
    num_part_lines = len(part_lines)

    for i in range(len(whole_lines) - num_part_lines + 1):
        add_leading = match_but_for_leading_whitespace(
            whole_lines[i : i + num_part_lines],
            part_lines,
        )

        if add_leading is None:
            continue

        replace_lines = [
            add_leading + rline if rline.strip() else rline for rline in replace_lines
        ]
        whole_lines = (
            whole_lines[:i] + replace_lines + whole_lines[i + num_part_lines :]
        )
        return "".join(whole_lines)

    return None


def try_dotdotdots(whole: str, part: str, replace: str) -> str | None:
    """Handle search/replace blocks that use ... to match code sections.

    Args:
        whole: Original file content
        part: Text to find/replace
        replace: Replacement text

    Returns:
        Updated content if dots matching was successful, None if no dots present,
        raises ValueError if dots are inconsistent

    """
    dots_re = re.compile(r"(^\s*\.\.\.\n)", re.MULTILINE | re.DOTALL)

    part_pieces = re.split(dots_re, part)
    replace_pieces = re.split(dots_re, replace)

    if len(part_pieces) != len(replace_pieces):
        raise ValueError("Unpaired ... in search/replace block")

    if len(part_pieces) == 1:
        # no dots in this edit block, just return None
        return None

    # Compare odd strings in part_pieces and replace_pieces
    all_dots_match = all(
        part_pieces[i] == replace_pieces[i] for i in range(1, len(part_pieces), 2)
    )

    if not all_dots_match:
        raise ValueError("Unmatched ... in search/replace block")

    part_pieces = [part_pieces[i] for i in range(0, len(part_pieces), 2)]
    replace_pieces = [replace_pieces[i] for i in range(0, len(replace_pieces), 2)]

    pairs = list(zip(part_pieces, replace_pieces))
    for part, replace in pairs:
        if not part and not replace:
            continue

        if not part and replace:
            if not whole.endswith("\n"):
                whole += "\n"
            whole += replace
            continue

        if whole.count(part) == 0:
            raise ValueError("Search text not found in file")
        if whole.count(part) > 1:
            raise ValueError("Multiple matches for search text - add more context")

        whole = whole.replace(part, replace, 1)

    return whole


def replace_closest_edit_distance(
    whole_lines: list[str],
    part: str,
    part_lines: list[str],
    replace_lines: list[str],
    similarity_thresh: float = 0.8,
) -> str | None:
    """Find and replace the chunk in whole_lines most similar to part_lines.

    Args:
        whole_lines: Original file lines with line endings
        part: Original search text as a single string
        part_lines: Original search text split into lines with line endings
        replace_lines: Replacement lines with line endings
        similarity_thresh: Minimum similarity threshold (0.0-1.0)

    Returns:
        Updated content if a similar enough match was found, None otherwise

    """
    max_similarity = 0
    most_similar_chunk_start = -1
    most_similar_chunk_end = -1

    scale = 0.1
    min_len = math.floor(len(part_lines) * (1 - scale))
    max_len = math.ceil(len(part_lines) * (1 + scale))

    for length in range(min_len, max_len):
        for i in range(len(whole_lines) - length + 1):
            chunk = whole_lines[i : i + length]
            chunk = "".join(chunk)

            similarity = SequenceMatcher(None, chunk, part).ratio()

            if similarity > max_similarity:
                max_similarity = similarity
                most_similar_chunk_start = i
                most_similar_chunk_end = i + length

    if max_similarity < similarity_thresh:
        return None

    modified_whole = (
        whole_lines[:most_similar_chunk_start]
        + replace_lines
        + whole_lines[most_similar_chunk_end:]
    )
    modified_whole = "".join(modified_whole)

    return modified_whole


def find_similar_lines(
    search_lines: str,
    content_lines: str,
    threshold: float = 0.6,
) -> str:
    """Find lines in content that are similar to search_lines.

    Args:
        search_lines: Text we're trying to match
        content_lines: Content of the file to search in
        threshold: Similarity threshold (0.0-1.0)

    Returns:
        String containing the most similar lines, or empty string if none found

    """
    search_lines_list = search_lines.splitlines()
    content_lines_list = content_lines.splitlines()

    # Handle empty input cases
    if not search_lines_list or not content_lines_list:
        return ""

    best_ratio = 0
    best_match: list[str] = []  # Initialize with empty list to avoid None checks
    best_match_i = 0  # Initialize to avoid unbound variable errors

    for i in range(len(content_lines_list) - len(search_lines_list) + 1):
        chunk = content_lines_list[i : i + len(search_lines_list)]
        ratio = SequenceMatcher(None, search_lines_list, chunk).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = chunk
            best_match_i = i

    if best_ratio < threshold:
        return ""

    if (
        best_match[0] == search_lines_list[0]
        and best_match[-1] == search_lines_list[-1]
    ):
        return "\n".join(best_match)

    N = 5
    best_match_end = min(
        len(content_lines_list), best_match_i + len(search_lines_list) + N
    )
    best_match_i = max(0, best_match_i - N)

    best = content_lines_list[best_match_i:best_match_end]
    return "\n".join(best)


def replace_most_similar_chunk(whole: str, part: str, replace: str) -> str | None:
    """Best efforts to find the `part` lines in `whole` and replace them with `replace`.

    Args:
        whole: Original file content
        part: Text to find/replace
        replace: Replacement text

    Returns:
        Updated content if a match was found, None otherwise

    """
    whole, whole_lines = prep(whole)
    part, part_lines = prep(part)
    replace, replace_lines = prep(replace)

    # Try perfect match or whitespace-flexible match
    res = perfect_or_whitespace(whole_lines, part_lines, replace_lines)
    if res:
        return res

    # drop leading empty line, LLMs sometimes add them spuriously
    if len(part_lines) > 2 and not part_lines[0].strip():
        skip_blank_line_part_lines = part_lines[1:]
        res = perfect_or_whitespace(
            whole_lines,
            skip_blank_line_part_lines,
            replace_lines,
        )
        if res:
            return res

    # Try to handle when it elides code with ...
    try:
        res = try_dotdotdots(whole, part, replace)
        if res:
            # If it worked with dotdotdots, we're good to proceed
            logger.debug(
                "Successfully used dotdotdots strategy to handle multiple occurrences",
            )
        else:
            # Fall back to the original error message
            matches = whole.count(part)
            raise ValueError(
                f"Found {matches} matches of the string to replace. For safety, this tool only supports replacing exactly one occurrence at a time. Add more lines of context to your edit and try again."
            )
    except ValueError:
        # If dotdotdots approach failed, give the original error message
        matches = whole.count(part)
        raise ValueError(
            f"Found {matches} matches of the string to replace. For safety, this tool only supports replacing exactly one occurrence at a time. Add more lines of context to your edit and try again."
        )

    # Try fuzzy matching
    res = replace_closest_edit_distance(whole_lines, part, part_lines, replace_lines)
    if res:
        return res

    return None


def debug_string_comparison(
    s1: str,
    s2: str,
    label1: str = "string1",
    label2: str = "string2",
) -> bool:
    """Thoroughly debug string comparison and identify differences.

    Args:
        s1: First string
        s2: Second string
        label1: Label for the first string
        label2: Label for the second string

    Returns:
        True if strings are different, False if they are the same

    """
    # Basic checks
    length_same = len(s1) == len(s2)
    content_same = s1 == s2

    logger.debug("String comparison debug:")
    logger.debug(f"  Length same? {length_same} ({len(s1)} vs {len(s2)})")
    logger.debug(f"  Content same? {content_same}")

    # Hash check
    hash1 = hashlib.md5(s1.encode("utf-8")).hexdigest()
    hash2 = hashlib.md5(s2.encode("utf-8")).hexdigest()
    logger.debug(f"  MD5 hashes: {hash1} vs {hash2}")

    # If strings appear to be the same but should be different
    if content_same:
        # Check for invisible characters or encoding issues
        s1_repr = repr(s1)
        s2_repr = repr(s2)
        logger.debug(f"  Repr comparison: {s1_repr[:100]} vs {s2_repr[:100]}")

        # Check byte by byte
        bytes1 = s1.encode("utf-8")
        bytes2 = s2.encode("utf-8")
        if bytes1 != bytes2:
            logger.debug(
                "  Strings differ at byte level even though they appear equal as strings!",
            )

            # Find the first differing byte
            for i, (b1, b2) in enumerate(zip(bytes1, bytes2, strict=False)):
                if b1 != b2:
                    logger.debug(
                        f"  First byte difference at position {i}: {b1} vs {b2}",
                    )
                    break
    else:
        # Find differences
        diff = list(difflib.ndiff(s1.splitlines(), s2.splitlines()))
        changes = [d for d in diff if d.startswith("+ ") or d.startswith("- ")]
        if changes:
            logger.debug("  Line differences (first 5):")
            for d in changes[:5]:
                logger.debug(f"    {d}")

        # Check if strings are equal after stripping trailing whitespace
        s1_no_trailing = "\n".join([line.rstrip() for line in s1.splitlines()])
        s2_no_trailing = "\n".join([line.rstrip() for line in s2.splitlines()])
        if s1_no_trailing == s2_no_trailing:
            logger.debug(
                "  Strings match when trailing whitespace is stripped from each line!",
            )

        # Check if strings are equal after normalizing only whitespace-only lines
        s1_normalized = "\n".join(
            [line.rstrip() if line.strip() == "" else line for line in s1.splitlines()],
        )
        s2_normalized = "\n".join(
            [line.rstrip() if line.strip() == "" else line for line in s2.splitlines()],
        )
        if s1_normalized == s2_normalized:
            logger.debug("  Strings match when normalizing only whitespace-only lines!")

    return not content_same


@mcp.tool()
async def edit_file(
    path: str,
    old_string: str | None = None,
    new_string: str | None = None,
    read_file_timestamps: dict[str, float] | None = None,
    description: str | None = None,
    chat_id: str | None = None,
    commit_hash: str | None = None,
    no_commit: bool = True,
) -> str:
    """This is a tool for editing files. For larger edits, use the WriteFile tool to overwrite files.
    Provide a short description of the change.

    Before using this tool:

    1. Use the ReadFile tool to understand the file's contents and context

    2. Verify the directory path is correct (only applicable when creating new files):
       - Use the LS tool to verify the parent directory exists and is the correct location

    To make a file edit, provide the following:
    1. path: The absolute path to the file to modify (must be absolute, not relative)
    2. old_string: The text to replace (must be unique within the file, and must match the file contents exactly, including all whitespace and indentation)
    3. new_string: The edited text to replace the old_string

    The tool will replace ONE occurrence of old_string with new_string in the specified file.

    CRITICAL REQUIREMENTS FOR USING THIS TOOL:

    1. UNIQUENESS: The old_string MUST uniquely identify the specific instance you want to change. This means:
       - Include AT LEAST 3-5 lines of context BEFORE the change point
       - Include AT LEAST 3-5 lines of context AFTER the change point
       - Include all whitespace, indentation, and surrounding code exactly as it appears in the file

    2. SINGLE INSTANCE: This tool can only change ONE instance at a time. If you need to change multiple instances:
       - Make separate calls to this tool for each instance
       - Each call must uniquely identify its specific instance using extensive context

    3. VERIFICATION: Before using this tool:
       - Check how many instances of the target text exist in the file
       - If multiple instances exist, gather enough context to uniquely identify each one
       - Plan separate tool calls for each instance

    WARNING: If you do not follow these requirements:
       - The tool will fail if old_string matches multiple locations
       - The tool will fail if old_string doesn't match exactly (including whitespace)
       - You may change the wrong instance if you don't include enough context

    When making edits:
       - Ensure the edit results in idiomatic, correct code
       - Do not leave the code in a broken state
       - Always use absolute file paths (starting with /)

    Remember: when making multiple file edits in a row to the same file, you should prefer to send all edits in a single message with multiple calls to this tool, rather than multiple messages with a single call each.

    Args:
        path: The absolute path to the file to edit
        old_string: The text to replace (use empty string for new file creation)
        new_string: The new text to replace old_string with
        read_file_timestamps: Dictionary mapping file paths to timestamps when they were last read
        description: Short description of the change
        chat_id: The unique ID of the current chat session
        commit_hash: Optional Git commit hash for version tracking
        no_commit: Whether to skip creating a git commit (default: True)

    Returns:
        A success message

    Note:
        This function allows creating new files when old_string is empty and the file doesn't exist.
        For existing files, it will reject attempts to edit files that are not tracked by git.
        Files must be tracked in the git repository before they can be modified.

    """
    # Set default values
    old_string = "" if old_string is None else old_string
    new_string = "" if new_string is None else new_string
    description = "" if description is None else description
    chat_id = "" if chat_id is None else chat_id

    # Normalize the file path
    full_file_path = normalize_file_path(path)

    # Normalize string inputs to ensure consistent newlines
    old_string = old_string.replace("\r\n", "\n")
    new_string = new_string.replace("\r\n", "\n")

    # Prevent editing codemcp.toml for security reasons
    if os.path.basename(full_file_path) == "codemcp.toml":
        raise ValueError("Editing codemcp.toml is not allowed for security reasons.")

    # Check file path and permissions
    is_valid, error_message = await check_file_path_and_permissions(full_file_path)
    if not is_valid:
        raise ValueError(error_message)

    # Handle creating a new file
    creating_new_file = old_string == "" and not os.path.exists(full_file_path)

    # Skip git tracking check if no_commit is True
    if not creating_new_file and not no_commit:
        # Only check commit_pending_changes for existing files when we want to commit
        is_tracked, track_error = await check_git_tracking_for_existing_file(
            full_file_path,
            chat_id=chat_id,
        )
        if not is_tracked:
            raise ValueError(track_error)

    # Debug string comparison using our thorough utility
    strings_are_different = debug_string_comparison(
        old_string,
        new_string,
        "old_string",
        "new_string",
    )

    if not strings_are_different:
        return "No changes to make: old_string and new_string are exactly the same."

    # Proceed with the edit now that we've confirmed the strings are different

    # Handle creating a new file
    if old_string == "" and os.path.exists(full_file_path):
        raise FileExistsError("Cannot create new file - file already exists.")

    # Handle creating a new file
    if old_string == "" and not os.path.exists(full_file_path):
        directory = os.path.dirname(full_file_path)
        os.makedirs(directory, exist_ok=True)
        await write_text_content(full_file_path, new_string)

        # Commit the changes (if no_commit is False)
        success, message = await commit_changes(full_file_path, description, chat_id, no_commit=no_commit)
        git_message = ""
        if success:
            git_message = f"\nChanges committed to git: {description}"
            # Include any extra details like previous commit hash if present in the message
            if "previous commit was" in message:
                git_message = f"\n{message}"
        else:
            git_message = f"\nFailed to commit changes to git: {message}"

        result = f"Successfully created {full_file_path}{git_message}"
        # Append commit hash
        result, _ = await append_commit_hash(result, full_file_path, commit_hash)
        return result

    # Check if file exists
    if not os.path.exists(full_file_path):
        # Try to find a similar file
        similar_file = find_similar_file(full_file_path)
        message = f"File does not exist: {full_file_path}"
        if similar_file:
            message += f" Did you mean {similar_file}?"
        raise FileNotFoundError(message)

    # Check if file is a Jupyter notebook
    if full_file_path.endswith(".ipynb"):
        raise ValueError(
            "File is a Jupyter Notebook. Use the NotebookEditTool to edit this file."
        )

    # Check if file has been read
    if read_file_timestamps and full_file_path not in read_file_timestamps:
        raise ValueError(
            "File has not been read yet. Read it first before writing to it."
        )

    # Check if file has been modified since read
    if read_file_timestamps and os.path.exists(full_file_path):
        last_write_time = os.stat(full_file_path).st_mtime
        if last_write_time > read_file_timestamps.get(full_file_path, 0):
            raise ValueError(
                "File has been modified since read, either by the user or by a linter. Read it again before attempting to write it."
            )

    # Use UTF-8 encoding and detect line endings
    line_endings = await detect_line_endings(full_file_path, return_format="format")

    # Read the original file
    content = await async_open_text(full_file_path, encoding="utf-8")

    # Apply the edit with advanced matching if needed
    _, updated_file, error = await apply_edit(full_file_path, old_string, new_string)

    # If there was an error during apply_edit, raise it
    if error:
        raise ValueError(error)

    # If no changes were made (which should never happen at this point),
    # log a warning but continue
    if content == updated_file and old_string.strip():
        logger.warning(
            "No changes were made despite passing all checks. This is unexpected.",
        )

    # Create directory if it doesn't exist
    directory = os.path.dirname(full_file_path)
    os.makedirs(directory, exist_ok=True)

    # Write the modified content back to the file
    await write_text_content(full_file_path, updated_file, "utf-8", line_endings)

    # Update read timestamp
    if read_file_timestamps is not None:
        read_file_timestamps[full_file_path] = os.stat(full_file_path).st_mtime

    # Try to run the formatter on the file
    format_message = ""
    formatter_success, formatter_output = await run_formatter_without_commit(
        full_file_path
    )
    if formatter_success:
        logger.info(f"Auto-formatted {full_file_path}")
        if formatter_output.strip():
            format_message = "\nAuto-formatted the file"
    else:
        # Only log warning if there was actually a format command configured but it failed
        if not "No format command configured" in formatter_output:
            logger.warning(
                f"Failed to auto-format {full_file_path}: {formatter_output}"
            )

    # Generate a snippet of the edited file to show in the response
    snippet = get_edit_snippet(content, old_string, new_string)

    # Commit the changes (if no_commit is False)
    git_message = ""
    success, message = await commit_changes(full_file_path, description, chat_id, no_commit=no_commit)
    if success:
        git_message = f"\n\nChanges committed to git: {description}"
        # Include any extra details like previous commit hash if present in the message
        if "previous commit was" in message:
            git_message = f"\n\n{message}"
    else:
        git_message = f"\n\nFailed to commit changes to git: {message}"

    result = f"Successfully edited {full_file_path}\n\nHere's a snippet of the edited file:\n{snippet}{format_message}{git_message}"

    # Append commit hash
    result, _ = await append_commit_hash(result, full_file_path, commit_hash)
    return result
