"""Strict JSON, with a bounded repair for display-only math delimiters.

Never repairs quotations, answer spans, keys, commands or JSON structure. A
damaged evidence string must still fail rather than silently change its text.
"""
import json
import re


def _display_field(path):
    protected = {"evidence", "citations", "answer_citations", "minimal_answer_citations",
                 "rubric_checks", "answer_spans", "source_blocks", "exact_quote", "quote"}
    if any(part in protected for part in path if isinstance(part, str)):
        return False
    return (bool(path) and path[-1] in {"question", "answer", "minimal_answer"}
            or len(path) >= 3 and path[-3] == "rubric" and isinstance(path[-2], int) and path[-1] == "text")


def _balanced_math(text):
    opened = None
    for delimiter in re.findall(r"(?<!\\)\\([()[\]])", text):
        if delimiter in '([':
            if opened is not None:
                return False
            opened = delimiter
        elif opened != {')': '(', ']': '['}[delimiter]:
            return False
        else:
            opened = None
    return opened is None


def load_model_json(content):
    r"""Keep valid JSON byte semantics; repair only invalid \( \) \[ \] escapes.

    The fallback parses the whole document and changes only an odd trailing
    backslash before a math delimiter in whitelisted presentation strings.
    Other malformed escapes and truncated objects remain errors. No API retry.
    """
    try:
        return json.loads(content)
    except json.JSONDecodeError as original:
        if original.msg != "Invalid \\escape":
            raise
    decoder = json.JSONDecoder()
    pos = 0
    changes = 0

    def whitespace():
        nonlocal pos
        while pos < len(content) and content[pos] in ' \t\r\n':
            pos += 1

    def string(path, key=False):
        nonlocal pos, changes
        start = pos
        previous_changes = changes
        pos += 1
        token = ['"']
        while pos < len(content):
            char = content[pos]
            if char == '"':
                token.append(char)
                pos += 1
                decoded = json.loads(''.join(token))
                if changes > previous_changes and not _balanced_math(decoded):
                    raise json.JSONDecodeError('Unbalanced math delimiters', content, start)
                return decoded
            if char == '\\':
                end = pos
                while end < len(content) and content[end] == '\\':
                    end += 1
                count = end - pos
                if (count % 2 and end < len(content) and content[end] in '()[]'
                        and not key and _display_field(path)):
                    count += 1
                    changes += 1
                token.append('\\' * count)
                if (end - pos) % 2 and end < len(content):
                    token.append(content[end])
                    pos = end + 1
                else:
                    pos = end
            else:
                token.append(char)
                pos += 1
        raise json.JSONDecodeError('Unterminated string', content, start)

    def value(path=(), depth=0):
        nonlocal pos
        if depth > 100:
            raise ValueError('Model JSON is nested too deeply')
        whitespace()
        if pos >= len(content):
            raise json.JSONDecodeError('Missing value', content, pos)
        char = content[pos]
        if char == '"':
            return string(path)
        if char not in '[{':
            result, pos = decoder.raw_decode(content, pos)
            return result
        is_object = char == '{'
        closing = '}' if is_object else ']'
        result = {} if is_object else []
        pos += 1
        whitespace()
        if pos < len(content) and content[pos] == closing:
            pos += 1
            return result
        while True:
            whitespace()
            if is_object:
                if pos >= len(content) or content[pos] != '"':
                    raise json.JSONDecodeError('Expected key', content, pos)
                key = string(path, key=True)
                whitespace()
                if pos >= len(content) or content[pos] != ':':
                    raise json.JSONDecodeError('Expected colon', content, pos)
                pos += 1
                result[key] = value(path + (key,), depth + 1)
            else:
                result.append(value(path + (len(result),), depth + 1))
            whitespace()
            if pos < len(content) and content[pos] == closing:
                pos += 1
                return result
            if pos >= len(content) or content[pos] != ',':
                raise json.JSONDecodeError('Expected comma', content, pos)
            pos += 1

    result = value()
    whitespace()
    if pos != len(content) or not changes:
        raise json.JSONDecodeError('Unrepaired JSON', content, pos)
    return result
