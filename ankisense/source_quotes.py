"""Restore generated quotations to literal source spans after layout changes."""
import copy
import re


def _cjk(char):
    return bool(char) and ("\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff")


def _layout(text):
    tokens = list(re.finditer(r"\S|\s+", text))
    chars, offsets = [], []
    for index, token in enumerate(tokens):
        value = token.group()
        if value.isspace():
            if index == 0 or index == len(tokens) - 1:
                continue
            before, after = tokens[index - 1].group()[-1], tokens[index + 1].group()[0]
            # Chinese wrapping may split a word at any character. Elsewhere
            # retain word/operator boundaries: 'a b' must not become 'ab'.
            if _cjk(before) or _cjk(after):
                continue
            value = " "
        # Full/half-width parentheses have the same delimiting meaning. Keep
        # offsets into the untouched source; do not normalize digits/operators.
        chars.append({"（": "(", "）": ")"}.get(value, value))
        offsets.append((token.start(), token.end()))
    return "".join(chars), offsets


def source_quote(quote, source):
    if not isinstance(quote, str) or not quote.strip():
        return quote
    if quote in source:
        return quote
    wanted, _ = _layout(quote)
    available, offsets = _layout(source)
    if not wanted:
        return quote
    start = available.find(wanted)
    if start < 0 or available.find(wanted, start + 1) >= 0:
        return quote
    # Save the actual source, including its original whitespace, rather than
    # weakening the literal-substring validator or accepting a fuzzy match.
    return source[offsets[start][0]:offsets[start + len(wanted) - 1][1]]


def align_model_quotes(value, blocks):
    """Copy model JSON and align only source-identified Evidence objects."""
    mapping = {block.block_id: block.text for block in blocks}

    def visit(item):
        if isinstance(item, list):
            return [visit(child) for child in item]
        if not isinstance(item, dict):
            return copy.deepcopy(item)
        result = {key: visit(child) for key, child in item.items()}
        if isinstance(item.get("block_id"), str) and item["block_id"] in mapping:
            for key in ("quote", "exact_quote"):
                if isinstance(item.get(key), str):
                    result[key] = source_quote(item[key], mapping[item["block_id"]])
        return result

    return visit(value)
