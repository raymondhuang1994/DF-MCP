"""JSON decoding with a stable container-depth limit across Python versions.

Callers must separately bound the input byte count before decoding. Root
containers have depth 1; scalar values do not add a container level.
"""
import json


def loads(data, *, max_depth=64):
    """Decode JSON, rejecting nesting beyond ``max_depth`` with ValueError."""
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
        raise ValueError("JSON depth limit must be a nonnegative integer.")
    try:
        value = json.loads(data)
    except RecursionError:
        raise ValueError("JSON container depth exceeds the permitted limit.") from None
    # Iterate rather than recurse so the check itself has the same behavior on
    # Python 3.9 and on releases whose JSON decoder permits deeper structures.
    pending = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if isinstance(current, (dict, list)):
            if depth > max_depth:
                raise ValueError("JSON container depth exceeds the permitted limit.")
            children = current.values() if isinstance(current, dict) else current
            pending.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return value
