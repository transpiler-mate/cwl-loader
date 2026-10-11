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
from typing import TYPE_CHECKING, Any

import pytest
from cwl_utils.parser.cwl_v1_2 import CommandLineTool, DockerRequirement, Workflow
from ruamel.yaml import YAML

from cwl_loader import (
    dump_cwl,
    load_cwl_from_location,
    load_cwl_from_stream,
    load_cwl_from_string_content,
    load_cwl_from_yaml,
)
from cwl_loader._requirements import RequirementLoadingError

if TYPE_CHECKING:
    from pathlib import Path

    from cwl_utils.parser import Process

NAMESPACE = "https://calrissian-cwl.github.io/schema#"
LEGACY_NAMESPACE = "http://calrissian-cwl.org/schema#"
PAYLOAD = {
    "workerCores": 1,
    "workerMemory": "2G",
    "clusterMaxCores": 2,
    "custom": {"relative": "config.json", "values": [1, None, "$(inputs.count)"]},
}


def tool(requirements: object) -> dict[str, Any]:
    return {
        "cwlVersion": "v1.2",
        "class": "CommandLineTool",
        "id": "tool",
        "baseCommand": "true",
        "inputs": [],
        "outputs": [],
        "$namespaces": {"calrissian": NAMESPACE},
        "requirements": requirements,
    }


def write_document(path: Path, document: dict[str, Any]) -> Path:
    with path.open("w") as stream:
        YAML().dump(document, stream)
    return path


def processes(loaded: Process | list[Process]) -> list[Process]:
    return loaded if isinstance(loaded, list) else [loaded]


def assert_preserved(loaded: Process | list[Process], uri: str = NAMESPACE) -> None:
    requirements = [
        requirement
        for process in processes(loaded)
        for requirement in (process.requirements or [])
        if requirement.class_ == uri + "DaskGatewayRequirement"
    ]
    assert len(requirements) == 1
    assert requirements[0].save() == {**PAYLOAD, "class": uri + "DaskGatewayRequirement"}


@pytest.mark.parametrize("namespace", [NAMESPACE, LEGACY_NAMESPACE])
@pytest.mark.parametrize("form", ["map", "list", "expanded"])
def test_requirement_forms_round_trip(tmp_path: Path, namespace: str, form: str) -> None:
    name = (
        namespace + "DaskGatewayRequirement"
        if form == "expanded"
        else "dask:DaskGatewayRequirement"
    )
    requirements = [{"class": name, **PAYLOAD}] if form == "list" else {name: PAYLOAD}
    document = tool(requirements)
    document["$namespaces"] = {"dask": namespace}
    before = deepcopy(document)
    path = write_document(tmp_path / "tool.cwl", document)
    loaded = load_cwl_from_yaml(document, uri=path.as_uri())
    assert document == before
    assert_preserved(loaded, namespace)
    stream = StringIO()
    dump_cwl(loaded, stream)
    saved = YAML().load(stream.getvalue())
    assert "DaskGatewayRequirement" not in str(saved.get("hints", []))
    assert_preserved(load_cwl_from_string_content(stream.getvalue(), uri=path.as_uri()), namespace)


@pytest.mark.parametrize("entrypoint", ["location", "stream", "string"])
def test_public_entrypoints_work_without_options(tmp_path: Path, entrypoint: str) -> None:
    path = write_document(
        tmp_path / "tool.cwl", tool({"calrissian:DaskGatewayRequirement": PAYLOAD})
    )
    if entrypoint == "location":
        loaded = load_cwl_from_location(str(path))
    elif entrypoint == "stream":
        loaded = load_cwl_from_stream(StringIO(path.read_text()), uri=path.as_uri())
    else:
        loaded = load_cwl_from_string_content(path.read_text(), uri=path.as_uri())
    assert_preserved(loaded)


@pytest.mark.parametrize("placement", ["external", "inline", "graph"])
def test_workflow_tools_preserve_requirements(tmp_path: Path, placement: str) -> None:
    child = tool({"calrissian:DaskGatewayRequirement": PAYLOAD})
    write_document(tmp_path / "tool.cwl", child)
    child.pop("$namespaces")
    root: dict[str, Any] = {
        "cwlVersion": "v1.2",
        "class": "Workflow",
        "id": "main",
        "inputs": [],
        "outputs": [],
        "$namespaces": {"calrissian": NAMESPACE},
        "steps": {"execute": {"in": [], "out": [], "run": "tool.cwl"}},
    }
    if placement == "inline":
        root["steps"]["execute"]["run"] = child
    elif placement == "graph":
        root["steps"]["execute"]["run"] = "#tool"
        root.pop("$namespaces")
        root = {
            "cwlVersion": "v1.2",
            "$namespaces": {"calrissian": NAMESPACE},
            "$graph": [root, child],
        }
    path = write_document(tmp_path / "main.cwl", root)
    loaded = load_cwl_from_location(str(path))
    assert_preserved(loaded)
    assert any(isinstance(process, Workflow) for process in processes(loaded))
    stream = StringIO()
    dump_cwl(loaded, stream)
    assert_preserved(load_cwl_from_string_content(stream.getvalue(), uri=path.as_uri()))


def test_standard_requirements_keep_typed_dom(tmp_path: Path) -> None:
    document = tool(
        {
            "DockerRequirement": {"dockerPull": "busybox"},
            "calrissian:DaskGatewayRequirement": PAYLOAD,
        }
    )
    loaded = load_cwl_from_location(str(write_document(tmp_path / "tool.cwl", document)))
    assert isinstance(loaded, CommandLineTool)
    assert loaded.requirements is not None
    assert isinstance(loaded.requirements[0], DockerRequirement)
    assert loaded.requirements[0].dockerPull == "busybox"


@pytest.mark.parametrize(
    "name", ["DockerRequrement", "https://w3id.org/cwl/cwl#UnknownRequirement"]
)
def test_unknown_requirements_have_actionable_errors(tmp_path: Path, name: str) -> None:
    path = write_document(tmp_path / "tool.cwl", tool({name: {}}))
    with pytest.raises(RequirementLoadingError, match="Unsupported mandatory requirement") as error:
        load_cwl_from_location(str(path))
    assert name.split(":")[-1] in str(error.value)
    assert "tool.cwl" in str(error.value)


def test_standard_validation_is_not_bypassed(tmp_path: Path) -> None:
    path = write_document(tmp_path / "tool.cwl", tool({"ResourceRequirement": {"coresMin": []}}))
    with pytest.raises(
        RequirementLoadingError, match=r"Invalid mandatory requirement.*ResourceRequirement"
    ):
        load_cwl_from_location(str(path))


def test_foreign_uri_cannot_impersonate_standard_requirement(tmp_path: Path) -> None:
    path = tmp_path / "tool.cwl"
    write_document(path, tool({path.as_uri() + "#DockerRequirement": {"dockerPull": "busybox"}}))
    with pytest.raises(RequirementLoadingError, match="Invalid mandatory requirement"):
        load_cwl_from_location(str(path))


def test_extension_loader_does_not_change_global_parser(tmp_path: Path) -> None:
    from cwl_utils.parser import LoadingOptions, load_document_by_yaml
    from schema_salad.exceptions import ValidationException

    document = tool({"calrissian:DaskGatewayRequirement": PAYLOAD})
    path = write_document(tmp_path / "tool.cwl", document)
    assert_preserved(load_cwl_from_location(str(path)))
    with pytest.raises(ValidationException):
        load_document_by_yaml(document, path.as_uri(), LoadingOptions(no_link_check=True))


@pytest.mark.parametrize(
    "namespace", ["http://calrissian.example.com/", "https://example.org/ext#"]
)
@pytest.mark.parametrize("form", ["map", "list", "expanded"])
def test_arbitrary_extensions_round_trip(namespace: str, form: str) -> None:
    name = namespace + "CustomRequirement" if form == "expanded" else "custom:CustomRequirement"
    requirements = [{"class": name, **PAYLOAD}] if form == "list" else {name: PAYLOAD}
    document = tool(requirements)
    document["$namespaces"] = {"custom": namespace}
    original = deepcopy(document)
    loaded = load_cwl_from_yaml(document)
    # Loading another document must not replace the first document's payload.
    load_cwl_from_yaml(tool({"DockerRequirement": {"dockerPull": "busybox"}}))
    stream = StringIO()
    dump_cwl(loaded, stream)
    reloaded = load_cwl_from_string_content(stream.getvalue())
    assert not isinstance(reloaded, list)
    assert reloaded.requirements is not None
    assert reloaded.requirements[0].save() == {**PAYLOAD, "class": namespace + "CustomRequirement"}
    assert document == original


def test_custom_hints_remain_optional() -> None:
    document = tool({"calrissian:CustomRequirement": PAYLOAD})
    document["hints"] = {"calrissian:OptionalRequirement": {"value": "optional"}}
    stream = StringIO()
    dump_cwl(load_cwl_from_yaml(document), stream)
    saved = YAML().load(stream.getvalue())
    assert [entry["class"] for entry in saved["requirements"]] == [NAMESPACE + "CustomRequirement"]
    assert saved["hints"][0]["class"] == "calrissian:OptionalRequirement"
    assert saved["$namespaces"]["calrissian"] == NAMESPACE
