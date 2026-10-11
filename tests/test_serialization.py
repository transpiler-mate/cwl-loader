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

from copy import deepcopy
from io import StringIO

import pytest
from cwl_utils.parser.cwl_v1_2 import (
    CommandInputRecordSchema,
    CommandLineTool,
    SchemaDefRequirement,
)
from ruamel.yaml import YAML

from cwl_loader import dump_cwl, load_cwl_from_yaml
from cwl_loader._serialization import normalize_serialized_document


@pytest.mark.parametrize("scatter", ["message", ["message"]])
def test_scatter_references_round_trip(scatter: str | list[str]) -> None:
    document = {
        "cwlVersion": "v1.2",
        "class": "Workflow",
        "id": "main",
        "requirements": {"ScatterFeatureRequirement": {}},
        "inputs": {"messages": "string[]"},
        "outputs": [],
        "steps": {
            "echo": {
                "in": {"message": "messages"},
                "out": [],
                "scatter": scatter,
                "run": {
                    "class": "CommandLineTool",
                    "id": "tool",
                    "inputs": {"message": "string"},
                    "outputs": [],
                    "baseCommand": "echo",
                },
            }
        },
    }
    loaded = load_cwl_from_yaml(document)
    stream = StringIO()
    dump_cwl(loaded, stream)
    saved = YAML().load(stream.getvalue())
    workflow = next(item for item in saved["$graph"] if item["class"] == "Workflow")
    assert workflow["steps"][0]["scatter"] == scatter
    reloaded = load_cwl_from_yaml(saved)
    output = StringIO()
    dump_cwl(reloaded, output)
    reserialized = YAML().load(output.getvalue())
    assert all("cwlVersion" not in item for item in reserialized["$graph"])
    workflow = next(item for item in reserialized["$graph"] if item["class"] == "Workflow")
    assert workflow["steps"][0]["scatter"] == scatter


@pytest.mark.parametrize("name", ["_:type", "io:/#main/_:type", "io://#main/_:type"])
def test_equal_definitions_use_yaml_aliases(name: str) -> None:
    definition = {"name": name, "type": "record", "fields": []}
    requirement = {"class": "SchemaDefRequirement", "types": [definition]}
    data: dict[str, object] = {
        "$graph": [
            {"requirements": [requirement], "inputs": [{"type": deepcopy(definition)}]},
            {"requirements": [deepcopy(requirement)]},
        ]
    }
    normalize_serialized_document(data)
    output = StringIO()
    YAML().dump(data, output)
    saved = YAML().load(output.getvalue())
    first, second = saved["$graph"]
    assert first["requirements"][0] is second["requirements"][0]
    assert first["inputs"][0]["type"] is first["requirements"][0]["types"][0]


def test_different_definitions_are_not_overwritten() -> None:
    data: dict[str, object] = {
        "requirements": [
            {"class": "SchemaDefRequirement", "types": [{"name": "schema#one", "type": "string"}]},
            {"class": "SchemaDefRequirement", "types": [{"name": "schema#two", "type": "int"}]},
        ],
        "inputs": [{"name": "_:same", "type": "string"}, {"name": "_:same", "type": "int"}],
    }
    original = deepcopy(data)
    normalize_serialized_document(data)
    assert data == original


@pytest.mark.parametrize("base", ["io:/", "io://"])
def test_synthetic_namespace_is_declared_without_overriding_existing(base: str) -> None:
    data: dict[str, object] = {"inputs": [{"name": base + "#main/_:type"}]}
    normalize_serialized_document(data)
    assert data["$namespaces"] == {"io": base}
    data["$namespaces"] = {"io": "https://example.org/custom/"}
    normalize_serialized_document(data)
    assert data["$namespaces"] == {"io": "https://example.org/custom/"}


def test_literal_io_text_does_not_declare_namespace() -> None:
    data: dict[str, object] = {"doc": "Example: io://#main"}
    normalize_serialized_document(data)
    assert "$namespaces" not in data


def test_dump_shares_identical_schemas_from_separate_tools() -> None:
    first = CommandLineTool(
        id="first",
        inputs=[],
        outputs=[],
        cwlVersion="v1.2",
        requirements=[
            SchemaDefRequirement(
                types=[
                    CommandInputRecordSchema(
                        name="https://example.org/schema#Record", fields=[], type_="record"
                    )
                ]
            )
        ],
    )
    second = deepcopy(first)
    second.id = "second"
    stream = StringIO()
    dump_cwl([first, second], stream)
    saved = YAML().load(stream.getvalue())
    first_saved, second_saved = saved["$graph"]
    assert first_saved["requirements"][0] is second_saved["requirements"][0]
    assert first.requirements is not second.requirements
