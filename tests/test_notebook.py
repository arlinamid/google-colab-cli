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

"""Cell selection and the local pre-flight check behind `colab exec`."""

import nbformat
import pytest

from colab_cli.notebook import (
    CellSelectionError,
    as_python,
    code_cells,
    preflight,
    select_cells,
)


def _nb(*sources, markdown_first=True):
    nb = nbformat.v4.new_notebook()
    cells = [nbformat.v4.new_markdown_cell("# intro")] if markdown_first else []
    cells += [nbformat.v4.new_code_cell(src) for src in sources]
    nb.cells = cells
    return nb


@pytest.fixture
def cells():
    return code_cells(
        _nb(
            "#@title Setup\nx = 1",
            "#@title Train\ny = x + 1",
            "print(y)",
            "#@title Plot\nprint('plot')",
        )
    )


def test_code_cells_numbers_code_cells_only(cells):
    assert [c.number for c in cells] == [1, 2, 3, 4]
    assert [c.title for c in cells] == ["Setup", "Train", "", "Plot"]
    assert cells[0].label == "cell 1 (Setup)"
    assert cells[2].label == "cell 3"
    assert all(c.id for c in cells)


def test_select_all_when_no_spec(cells):
    assert select_cells(cells, None) == cells


@pytest.mark.parametrize(
    "spec, numbers",
    [
        ("3", [3]),
        ("4,1", [4, 1]),  # the given order, not notebook order
        ("2-4", [2, 3, 4]),
        ("1, 3-4", [1, 3, 4]),
        ("plot,setup", [4, 1]),  # titles, case-insensitive
        ("1,1", [1, 1]),
    ],
)
def test_select_cells(cells, spec, numbers):
    assert [c.number for c in select_cells(cells, spec)] == numbers


def test_select_by_cell_id(cells):
    assert select_cells(cells, cells[2].id) == [cells[2]]


@pytest.mark.parametrize(
    "spec, message",
    [
        ("5", "there is no code cell 5; the notebook has 4"),
        ("3-6", "there is no code cell 5"),
        ("4-2", "runs backwards"),
        ("Nope", "no code cell has the title or id 'Nope'"),
        ("1,,2", "Empty entry"),
        ("", "Empty entry"),
    ],
)
def test_select_cells_errors(cells, spec, message):
    with pytest.raises(CellSelectionError, match=message):
        select_cells(cells, spec)


def test_select_ambiguous_title():
    cells = code_cells(_nb("#@title Same\na = 1", "#@title Same\nb = 2"))
    with pytest.raises(CellSelectionError, match=r"2 code cells are titled 'Same' \(1, 2\)"):
        select_cells(cells, "Same")


@pytest.mark.parametrize(
    "source",
    [
        "!pip install -q x",
        "%cd /content\nprint(1)",
        "files = !ls\nprint(files)",
        "env = %env\n",
        "if True:\n    !echo hi\n    %time x = 1",
        "await something()",
        "#@title T\nx = 1 #@param {type:'integer'}",
        "if a != b:\n    pass",
        "y = 5 % 3",
    ],
)
def test_preflight_accepts_ipython_cells(source):
    assert preflight(code_cells(_nb(source))) == []


def test_preflight_skips_cell_magics():
    assert as_python("%%bash\necho $((1 +\n") is None
    assert preflight(code_cells(_nb("%%bash\nif then fi"))) == []


def test_preflight_keeps_line_numbers():
    assert as_python("!ls\nx = (1,\n").splitlines()[0] == "pass"


def test_preflight_reports_each_broken_cell_with_line():
    cells = code_cells(
        _nb("#@title Ok\nx = 1", "#@title Broken\nx = 1\ny = (2,\n", "def f(:\n    pass")
    )
    problems = preflight(cells)
    assert len(problems) == 2
    assert problems[0].startswith("cell 2 (Broken), line ")
    assert problems[1].startswith("cell 3, line 1:")
    assert "def f(:" in problems[1]
