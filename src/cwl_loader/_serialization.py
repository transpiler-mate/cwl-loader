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

"""Normalize serialized CWL type identities without changing their definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


def _mappings(node: object) -> Iterator[dict[str, object]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _mappings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _mappings(value)


def _shared_definition(
    node: dict[str, object], definitions: dict[str, list[dict[str, object]]]
) -> dict[str, object]:
    name = node.get("name")
    if node.get("class") == "SchemaDefRequirement":
        key = "SchemaDefRequirement"
    elif isinstance(name, str) and name.rsplit("/", 1)[-1].startswith("_:"):
        key = name
    else:
        return node
    candidates = definitions.setdefault(key, [])
    for candidate in candidates:
        if candidate == node:
            return candidate
    candidates.append(node)
    return node


def _share_definitions(node: object, definitions: dict[str, list[dict[str, object]]]) -> object:
    if isinstance(node, dict):
        for key, value in node.items():
            node[key] = _share_definitions(value, definitions)
        return _shared_definition(node, definitions)
    if isinstance(node, list):
        return [_share_definitions(value, definitions) for value in node]
    return node


def normalize_serialized_document(data: dict[str, object]) -> None:
    """Share equal type definitions and declare synthetic identifier namespaces.

    Mutates the serialized document so YAML emits aliases for equal schema
    requirements and anonymous named types. Definitions with different payloads
    remain distinct even if their names or schema URLs match. Existing namespace
    declarations take precedence over the default ``io`` identifier namespace.
    Graph members inherit the document's CWL version.
    """
    graph = data.get("$graph")
    if isinstance(graph, list):
        for item in graph:
            if isinstance(item, dict):
                item.pop("cwlVersion", None)
    _share_definitions(data, {})
    bases = {
        "io://" if name.startswith("io://") else "io:/"
        for node in _mappings(data)
        for field in ("id", "name")
        if isinstance(name := node.get(field), str) and name.startswith("io:/")
    }
    if bases:
        namespaces = data.setdefault("$namespaces", {})
        if isinstance(namespaces, dict):
            namespaces.setdefault("io", "io://" if "io://" in bases else "io:/")
