# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Notebook helpers for ``colab exec``: cell selection and a local pre-flight check.

Both run on the local machine before anything is sent to the runtime, so a
typo in ``--cells`` or a syntax error in cell 7 is reported before cells 1-6
have already run on a paid accelerator.
"""

import ast
import re
import uuid
from dataclasses import dataclass
from typing import Any, List, Optional

TITLE_REGEX = re.compile(r"^\s*#\s*@title\s+(.*)", re.MULTILINE)

# IPython syntax that is not Python: `!cmd`, `%magic` and `x = !cmd`.
_SHELL_OR_MAGIC_LINE = re.compile(r"^(\s*)[!%]")
_SHELL_OR_MAGIC_ASSIGN = re.compile(r"^(\s*[^=!%#\s][^=!%#]*?=\s*)[!%]")
_RANGE = re.compile(r"(\d+)\s*-\s*(\d+)")


@dataclass
class CodeCell:
    """A code cell, numbered from 1 among the notebook's code cells."""

    number: int
    id: Optional[str]
    title: str
    source: str
    cell: Any = None
    # Overrides the label, e.g. the file name when checking a .py file.
    name: Optional[str] = None

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        return f"cell {self.number}" + (f" ({self.title})" if self.title else "")


class CellSelectionError(ValueError):
    """A ``--cells`` value that does not match the notebook."""


def code_cells(nb) -> List[CodeCell]:
    """The notebook's code cells in order, giving each an id if it has none."""
    cells = []
    for cell in nb.cells:
        # nbformat v4.5+ requires 'id' at the top level
        if not getattr(cell, "id", None):
            cell.id = str(uuid.uuid4())
        if cell.cell_type != "code":
            continue
        match = TITLE_REGEX.search(cell.source)
        cells.append(
            CodeCell(
                number=len(cells) + 1,
                id=cell.id,
                title=match.group(1).strip() if match else "",
                source=cell.source,
                cell=cell,
            )
        )
    return cells


def select_cells(cells: List[CodeCell], spec: Optional[str]) -> List[CodeCell]:
    """Pick cells for ``--cells``, in the order given.

    ``spec`` is a comma-separated list of code-cell numbers (``3``), ranges
    (``2-5``), ``#@title`` values or cell ids. Numbers count code cells only,
    from 1, so markdown cells do not shift them. An entry that looks like a
    number or a range is always read as one, even if a title or cell id reads
    the same (nbformat's random ids are sometimes all digits); select such a
    cell by its number. ``None`` selects every cell.
    """
    if spec is None:
        return list(cells)
    by_number = {c.number: c for c in cells}
    tokens = [t.strip() for t in spec.split(",")]
    if not any(tokens) or any(not t for t in tokens):
        raise CellSelectionError(f"Empty entry in --cells {spec!r}.")

    def by_num(n: int) -> CodeCell:
        if n not in by_number:
            raise CellSelectionError(
                f"--cells: there is no code cell {n}; the notebook has "
                f"{len(cells)} code cell(s)."
            )
        return by_number[n]

    selected: List[CodeCell] = []
    for tok in tokens:
        rng = _RANGE.fullmatch(tok)
        if rng:
            start, end = int(rng.group(1)), int(rng.group(2))
            if start > end:
                raise CellSelectionError(f"--cells: range {tok!r} runs backwards.")
            selected.extend(by_num(n) for n in range(start, end + 1))
        elif tok.isdigit():
            selected.append(by_num(int(tok)))
        else:
            matches = [
                c for c in cells if c.title.casefold() == tok.casefold() or c.id == tok
            ]
            if not matches:
                raise CellSelectionError(
                    f"--cells: no code cell has the title or id {tok!r}."
                )
            if len(matches) > 1:
                numbers = ", ".join(str(c.number) for c in matches)
                raise CellSelectionError(
                    f"--cells: {len(matches)} code cells are titled {tok!r} "
                    f"({numbers}); select them by number instead."
                )
            selected.append(matches[0])
    return selected


def as_python(source: str) -> Optional[str]:
    """IPython cell source as plain Python, for a syntax check.

    Shell and magic lines become ``pass`` (or ``None`` on the right of an
    assignment) with their indentation kept, so line numbers still match.
    Returns None for a cell magic (``%%bash`` ...), whose body is not Python.
    """
    lines = source.splitlines()
    first = next((line for line in lines if line.strip()), "")
    if first.lstrip().startswith("%%"):
        return None
    out = []
    for line in lines:
        assign = _SHELL_OR_MAGIC_ASSIGN.match(line)
        if assign:
            out.append(assign.group(1) + "None")
            continue
        magic = _SHELL_OR_MAGIC_LINE.match(line)
        if magic:
            out.append(magic.group(1) + "pass")
            continue
        out.append(line)
    return "\n".join(out)


def preflight(cells: List[CodeCell]) -> List[str]:
    """Syntax-check ``cells`` locally. Returns one message per problem."""
    problems = []
    for c in cells:
        code = as_python(c.source)
        if code is None:
            continue
        try:
            compile(
                code,
                f"<{c.label}>",
                "exec",
                flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
                dont_inherit=True,
            )
        except SyntaxError as e:
            message = f"{c.label}, line {e.lineno}: {e.msg}"
            if e.text and e.text.strip():
                message += f"\n    {e.text.rstrip()}"
            problems.append(message)
    return problems
