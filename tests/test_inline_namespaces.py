# Copyright 2026 Terradue
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

from __future__ import annotations

from copy import deepcopy
from io import StringIO
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from cwl_utils.parser import CommandLineTool, Workflow
from ruamel.yaml import YAML
from schema_salad.exceptions import ValidationException
from schema_salad.fetcher import DefaultFetcher

from cwl_loader import dump_cwl, load_cwl_from_string_content, load_cwl_from_yaml
from tests.test_inline_and_file_loading import index, inline, workflow

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def namespace_links_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep namespace resolution tests independent of remote vocabulary availability."""
    monkeypatch.setattr(DefaultFetcher, "check_exists", Mock(return_value=True))


TOOL = """class: CommandLineTool
id: echo-tool
baseCommand: echo
inputs:
  message:
    type: string
    inputBinding: {position: 1}
  file:
    type: File?
    format: s:Dataset
outputs: {result: {type: File, outputBinding: {glob: result.txt}}}
$namespaces: {s: 'https://schema.org/'}
s:description: 's:literal text'
arguments: ['s:literal argument']
requirements:
  s:CustomRequirement: {value: 's:literal payload'}
hints:
  s:CustomHint: {value: 's:literal hint'}
"""


@pytest.mark.parametrize("root_namespace", ["", "https://schema.org/", "https://parent.example/"])
@pytest.mark.parametrize("step_form", ["map", "list"])
def test_inline_namespace_scopes_round_trip(
    tmp_path: Path, root_namespace: str, step_form: str
) -> None:
    document = YAML().load(workflow(inline(TOOL)))
    if root_namespace:
        document["$namespaces"] = {"s": root_namespace}
    if step_form == "list":
        document["steps"] = [{"id": "echo", **document["steps"]["echo"]}]
    original = deepcopy(document)
    source = tmp_path / "inline-namespaces.cwl"
    with source.open("w") as source_stream:
        YAML().dump(document, source_stream)
    loaded = load_cwl_from_yaml(document, uri=source.as_uri())
    assert document == original
    for _ in range(2):
        processes = index(loaded)
        root = processes["main"]
        assert isinstance(root, Workflow)
        tool = processes[root.steps[0].run[1:]]
        assert isinstance(tool, CommandLineTool)
        assert tool.inputs[1].format == "https://schema.org/Dataset"
        assert tool.extension_fields["https://schema.org/description"] == "s:literal text"
        assert tool.arguments == ["s:literal argument"]
        assert tool.requirements is not None
        assert tool.requirements[0].save() == {
            "class": "https://schema.org/CustomRequirement",
            "value": "s:literal payload",
        }
        assert tool.loadingOptions.namespaces["s"] == "https://schema.org/"
        assert root.loadingOptions.namespaces.get("s", "") == root_namespace
        stream = StringIO()
        dump_cwl(loaded, stream)
        loaded = load_cwl_from_string_content(stream.getvalue())


def test_nested_namespace_inheritance_and_sibling_isolation() -> None:
    nested = workflow(inline(TOOL.replace("$namespaces: {s: 'https://schema.org/'}", "")))
    nested = nested.replace("cwlVersion: v1.2\n", "").replace("id: main\n", "")
    document = YAML().load(workflow(inline(nested)))
    document["$namespaces"] = {
        "inherited": "https://inherited.example/",
        "s": "https://parent.example/",
    }
    inner = document["steps"]["echo"]["run"]
    inner["$namespaces"] = {"s": "https://schema.org/"}
    sibling = YAML().load(TOOL)
    sibling.pop("$namespaces")
    document["steps"]["sibling"] = {"in": {"message": "message"}, "out": [], "run": sibling}
    loaded = load_cwl_from_yaml(document)
    for _ in range(2):
        tools = [
            process for process in index(loaded).values() if isinstance(process, CommandLineTool)
        ]
        assert {tool.inputs[1].format for tool in tools} == {
            "https://schema.org/Dataset",
            "https://parent.example/Dataset",
        }
        assert all(
            tool.loadingOptions.namespaces["inherited"] == "https://inherited.example/"
            for tool in tools
        )
        stream = StringIO()
        dump_cwl(loaded, stream)
        loaded = load_cwl_from_string_content(stream.getvalue())


@pytest.mark.parametrize("namespaces", [None, [], {"s": 12}])
def test_invalid_inline_namespaces_rejected(namespaces: object) -> None:
    document = YAML().load(workflow(inline(TOOL)))
    document["steps"]["echo"]["run"]["$namespaces"] = namespaces
    with pytest.raises(ValidationException, match="namespaces must map"):
        load_cwl_from_yaml(document)


def test_inline_namespaces_do_not_bypass_process_validation() -> None:
    document = YAML().load(workflow(inline(TOOL)))
    document["steps"]["echo"]["run"]["invalid_field"] = True
    with pytest.raises(ValidationException, match="invalid_field"):
        load_cwl_from_yaml(document)
