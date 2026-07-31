"""
Docstring contract tests.

Open WebUI v0.10.2 parses tool docstrings with reST ``:param`` directives
(``backend/open_webui/utils/tools.py``: ``parse_description`` and
``parse_docstring``, applied inside ``convert_function_to_pydantic_model``).

The two parse functions are copied verbatim below (with attribution) so the
tests assert against the *exact* upstream semantics:

- The function description is everything before the first ``:param`` /
  ``:return`` line.
- A parameter description is captured per line by
  ``:param (\w+):\s*(.+)`` — **only the first line** is used. Continuation
  lines are silently dropped from the parameter description and, because they
  do not start with ``:param``, they leak into the function description.

Contract enforced here:

1. Every public method documents exactly its non-internal signature
   parameters (``__request__`` is injected by the harness and must NOT be
   documented).
2. Every ``:param`` description fits on a single line.
3. The function description is non-empty and contains no ``:param`` line
   (i.e. no continuation leaked past ``parse_description``).
"""

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import owui_meta


# ── Copied verbatim from open-webui v0.10.2 backend/open_webui/utils/tools.py ──

def parse_description(docstring: str | None) -> str:
    """Parse a function's docstring to extract the description.

    Args:
        docstring (str): The docstring to parse.

    Returns:
        str: The description.
    """
    if not docstring:
        return ''
    lines = [line.strip() for line in docstring.strip().split('\n')]
    description_lines: list[str] = []
    for line in lines:
        if re.match(r':param', line) or re.match(r':return', line):
            break
        description_lines.append(line)
    return '\n'.join(description_lines)


def parse_docstring(docstring):
    """Parse a function's docstring to extract parameter descriptions in reST format.

    Args:
        docstring (str): The docstring to parse.

    Returns:
        dict: A dictionary where keys are parameter names and values are descriptions.
    """
    if not docstring:
        return {}
    param_pattern = re.compile(r':param (\w+):\s*(.+)')
    param_descriptions = {}
    for line in docstring.splitlines():
        match = param_pattern.match(line.strip())
        if not match:
            continue
        param_name, param_description = match.groups()
        if param_name.startswith('__'):
            continue
        param_descriptions[param_name] = param_description
    return param_descriptions


# ── Mirror of get_functions_from_tool: public, callable, non-class attrs ──

def public_functions(cls) -> list:
    return [
        getattr(cls, name)
        for name in dir(cls)
        if callable(getattr(cls, name))
        and not name.startswith('_')
        and not inspect.isclass(getattr(cls, name))
    ]


def signature_params(func) -> set[str]:
    # OWUI introspects bound methods (``get_functions_from_tool(instance)``),
    # which exclude ``self``; the tests use the class, so filter it out.
    return {n for n in inspect.signature(func).parameters if n not in ('self',)}


def test_every_public_method_documents_exactly_its_params():
    for func in public_functions(owui_meta.Tools):
        doc = inspect.getdoc(func) or ''
        parsed = parse_docstring(doc)
        expected = {n for n in signature_params(func) if not n.startswith('__')}
        assert set(parsed) == expected, (
            f"{func.__name__}: documented params {sorted(parsed)} != "
            f"signature params {sorted(expected)}"
        )


def test_param_descriptions_are_single_line():
    param_pattern = re.compile(r':param (\w+):\s*(.+)')
    directive = re.compile(r':(param|return|type|raises|note)\b')
    for func in public_functions(owui_meta.Tools):
        doc = inspect.getdoc(func) or ''
        lines = [l.strip() for l in doc.splitlines()]
        for i, line in enumerate(lines):
            if not param_pattern.match(line):
                continue
            # the next non-blank line must be another directive (or the end):
            # a bare line would be a continuation, dropped by parse_docstring
            # and leaked into the function description by parse_description.
            j = i + 1
            while j < len(lines) and not lines[j]:
                j += 1
            if j < len(lines) and not directive.match(lines[j]):
                raise AssertionError(
                    f"{func.__name__}: :param description spans multiple lines "
                    f"({lines[j]!r} leaked as continuation)"
                )


def test_function_description_is_clean():
    for func in public_functions(owui_meta.Tools):
        doc = inspect.getdoc(func) or ''
        desc = parse_description(doc)
        assert desc.strip(), f"{func.__name__}: empty function description"
        assert not re.match(r':(param|return)\b', desc.strip().splitlines()[0]), (
            f"{func.__name__}: function description starts with a directive"
        )
        assert not any(re.match(r':param\b', l) for l in desc.splitlines()), (
            f"{func.__name__}: ':param' line leaked into the function description"
        )


def test_request_param_never_documented():
    for func in public_functions(owui_meta.Tools):
        doc = inspect.getdoc(func) or ''
        assert '__request__' not in doc, (
            f"{func.__name__}: __request__ must not be documented (harness-injected)"
        )
