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

"""Parse nested processes with their own inherited namespace contexts."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from uuid import uuid4

from cwl_utils.parser import (
    LoadingOptions,
    Process,
    Workflow,
    load_document_by_yaml,
)
from ruamel.yaml.comments import CommentedMap
from schema_salad.exceptions import ValidationException


def _scoped_document(
    document: Mapping[str, object], options: LoadingOptions
) -> tuple[dict[str, object], LoadingOptions]:
    """Copy a process declaration and apply its namespace overrides locally."""
    prepared = deepcopy(CommentedMap(document))
    namespaces = prepared.pop("$namespaces", {})
    if not isinstance(namespaces, Mapping) or any(
        not isinstance(prefix, str) or not isinstance(uri, str)
        for prefix, uri in namespaces.items()
    ):
        raise ValidationException("$namespaces must map string prefixes to string URIs")
    return prepared, LoadingOptions(
        copyfrom=options, namespaces={**options.namespaces, **namespaces}
    )


def _detach_inline_runs(document: dict[str, object]) -> dict[str, Mapping[str, object]]:
    """Temporarily replace inline runs while the enclosing workflow is validated."""
    detached: dict[str, Mapping[str, object]] = {}
    steps = document.get("steps", [])
    if isinstance(steps, Mapping):
        steps = list(steps.values())
    if not isinstance(steps, list):
        return detached
    for step in steps:
        if (
            isinstance(step, dict)
            and isinstance(run := step.get("run"), Mapping)
            and "class" in run
        ):
            reference = f"urn:uuid:{uuid4()}"
            detached[reference] = run
            step["run"] = reference
    return detached


def _attach_inline_runs(
    process: Process,
    detached: Mapping[str, Mapping[str, object]],
    version: str,
) -> None:
    """Validate detached runs in the enclosing step's scope and attach their DOMs."""
    if not isinstance(process, Workflow):
        return
    for step in process.steps:
        if isinstance(step.run, str) and step.run in detached:
            loaded = load_scoped_document(
                {"cwlVersion": version, **detached[step.run]},
                f"{step.id}/run",
                process.loadingOptions,
            )
            if isinstance(loaded, list):
                raise ValidationException("An inline run must be a single process")
            step.run = loaded


def load_scoped_document(
    document: Mapping[str, object], uri: str, options: LoadingOptions | None
) -> Process | list[Process]:
    """Load a document without leaking inline namespace overrides into siblings.

    Namespace directives are removed before generated process validation. The
    effective context stays on each process for URI expansion and serialization.
    The caller's document is not modified.

    Raises:
        ValidationException: If namespaces or process declarations are invalid.
    """
    prepared, scoped_options = _scoped_document(document, options or LoadingOptions(fileuri=uri))
    version = str(document.get("cwlVersion"))
    graph = prepared.get("$graph")
    if isinstance(graph, list):
        processes: list[Process] = []
        controls: dict[str, object] = {
            key: prepared[key] for key in ("$base", "$schemas") if key in prepared
        }
        for member in graph:
            if not isinstance(member, Mapping):
                raise ValidationException("$graph entries must be process objects")
            loaded = load_scoped_document(
                {**controls, **member, "cwlVersion": version}, uri, scoped_options
            )
            processes.extend(loaded if isinstance(loaded, list) else [loaded])
        return processes
    detached = _detach_inline_runs(prepared)
    process: Process = load_document_by_yaml(
        prepared, uri, loadingOptions=scoped_options, load_all=True
    )
    _attach_inline_runs(process, detached, version)
    return process
