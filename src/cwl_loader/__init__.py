# Copyright 2025 Terradue
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

"""Load, normalize, and serialize CWL documents while preserving metadata."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import MutableMapping as MutableMappingABC
from gzip import GzipFile
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urldefrag, urlparse
from urllib.request import url2pathname

import requests
from cwl_utils.parser import Process, load_document_by_yaml, save
from loguru import logger
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from ._cwlupgrader import _upgrade_document
from ._dereference import _dereference_steps, remove_refs
from .sort import order_graph_by_dependencies
from .utils import assert_connected_graph

__DEFAULT_BASE_URI__ = "io://"
__TARGET_CWL_VERSION__ = "v1.2"
__DEFAULT_ENCODING__ = "utf-8"
__CWL_VERSION__ = "cwlVersion"
__CWL_GRAPH__ = "$graph"
__CWL_DOCUMENT_METADATA_ATTR__ = "_cwl_loader_document_metadata"
__CWL_DOCUMENT_HAS_GRAPH_ATTR__ = "_cwl_loader_document_has_graph"
__CWL_DOCUMENT_CONTROL_FIELDS__ = ("$namespaces", "$schemas", "$base")

_yaml = YAML()
_global_session = requests.Session()


def _as_process_list(process: Process | list[Process]) -> list[Process]:
    return process if isinstance(process, list) else [process]


def _extract_document_metadata(
    raw_process: Mapping[str, Any] | CommentedMap,
    process: Process | list[Process] | None = None,
) -> CommentedMap:
    metadata = CommentedMap()

    process_fields: set[str] = set()
    if __CWL_GRAPH__ not in raw_process and process is not None:
        for p in _as_process_list(process):
            process_fields.update(getattr(p, "attrs", ()))

    for key, value in raw_process.items():
        if key not in (__CWL_VERSION__, __CWL_GRAPH__) and key not in process_fields:
            metadata[key] = value

    return metadata


def _preserve_document_metadata(
    process: Process | list[Process],
    document_metadata: CommentedMap,
    document_has_graph: bool,
) -> None:
    """Attach document metadata to each parsed process for serialization."""
    if not document_metadata:
        return

    for p in _as_process_list(process):
        metadata = CommentedMap(document_metadata)
        setattr(p, __CWL_DOCUMENT_METADATA_ATTR__, metadata)
        setattr(p, __CWL_DOCUMENT_HAS_GRAPH_ATTR__, document_has_graph)

        loading_options = getattr(p, "loadingOptions", None)
        if loading_options is not None:
            loading_options.addl_metadata.update(metadata)


def _preserved_document_metadata(
    process: Process | list[Process],
) -> Mapping[str, Any] | None:
    """Find preserved document metadata, falling back to parser metadata."""
    for p in _as_process_list(process):
        metadata = getattr(p, __CWL_DOCUMENT_METADATA_ATTR__, None)
        if isinstance(metadata, Mapping) and metadata:
            return metadata

        loading_options = getattr(p, "loadingOptions", None)
        metadata = getattr(loading_options, "addl_metadata", None)
        if isinstance(metadata, Mapping) and metadata:
            return metadata

    return None


def _has_preserved_graph_document(process: Process | list[Process]) -> bool:
    return any(
        bool(getattr(p, __CWL_DOCUMENT_HAS_GRAPH_ATTR__, False)) for p in _as_process_list(process)
    )


def _strip_nested_document_controls(
    data: MutableMappingABC[str, Any], document_metadata: Mapping[str, Any]
) -> None:
    """Remove document-level controls from nested graph entries in place."""
    graph = data.get(__CWL_GRAPH__)

    if not isinstance(graph, list):
        return

    for item in graph:
        if not isinstance(item, MutableMappingABC):
            continue

        for field in __CWL_DOCUMENT_CONTROL_FIELDS__:
            if field in document_metadata:
                item.pop(field, None)


def _serialized_extension_metadata_keys(
    process: Process | list[Process], document_metadata: Mapping[str, Any]
) -> set[str]:
    """Return serialized extension keys that duplicate preserved source keys."""
    serialized_keys: set[str] = set()

    for p in _as_process_list(process):
        extension_fields = getattr(p, "extension_fields", {})
        loading_options = getattr(p, "loadingOptions", None)
        namespaces = getattr(loading_options, "namespaces", {})

        for key in document_metadata:
            if key.startswith("$"):
                continue

            expanded_key = key
            if ":" in key and not key.startswith(("http://", "https://")):
                prefix, local_name = key.split(":", 1)
                if prefix in namespaces:
                    expanded_key = f"{namespaces[prefix]}{local_name}"

            if expanded_key in extension_fields:
                serialized_keys.add(expanded_key)

    return serialized_keys


def _strip_serialized_extension_metadata(
    data: MutableMappingABC[str, Any],
    process: Process | list[Process],
    document_metadata: Mapping[str, Any],
) -> None:
    """Remove serialized extension fields duplicated by document metadata."""
    keys = _serialized_extension_metadata_keys(process, document_metadata)
    if not keys:
        return

    graph = data.get(__CWL_GRAPH__)
    targets = graph if isinstance(graph, list) else [data]
    for target in targets:
        if isinstance(target, MutableMappingABC):
            for key in keys:
                target.pop(key, None)


def _restore_graph_document(data: MutableMappingABC[str, Any]) -> CommentedMap:
    restored = CommentedMap()
    graph_item = CommentedMap((key, value) for key, value in data.items() if key != __CWL_VERSION__)
    if __CWL_VERSION__ in data:
        restored[__CWL_VERSION__] = data[__CWL_VERSION__]
    restored[__CWL_GRAPH__] = [graph_item]
    return restored


def _merge_document_metadata(
    restored: MutableMappingABC[str, Any], document_metadata: Mapping[str, Any]
) -> CommentedMap:
    result = CommentedMap()
    if __CWL_VERSION__ in restored:
        result[__CWL_VERSION__] = restored[__CWL_VERSION__]

    for key, value in document_metadata.items():
        if key not in (__CWL_VERSION__, __CWL_GRAPH__) and key not in result:
            result[key] = value

    for key, value in restored.items():
        if key not in result:
            result[key] = value

    return result


def _restore_document_metadata(data: Any, process: Process | list[Process]) -> Any:
    document_metadata = _preserved_document_metadata(process)

    if not document_metadata or not isinstance(data, MutableMappingABC):
        return data

    if _has_preserved_graph_document(process) and __CWL_GRAPH__ not in data:
        restored = _restore_graph_document(data)
    else:
        restored = CommentedMap(data)

    _strip_nested_document_controls(restored, document_metadata)
    if not _has_preserved_graph_document(process):
        _strip_serialized_extension_metadata(restored, process, document_metadata)
    return _merge_document_metadata(restored, document_metadata)


def _is_url(path_or_url: str, session: requests.Session) -> bool:
    try:
        result = urlparse(path_or_url)
        return all([f"{result.scheme}://" in session.adapters, result.netloc])
    except Exception:
        return False


def load_cwl_from_yaml(
    raw_process: Mapping[str, Any] | CommentedMap,
    uri: str = __DEFAULT_BASE_URI__,
    cwl_version: str = __TARGET_CWL_VERSION__,
    sort: bool = True,
    session: requests.Session = _global_session,
) -> Process | list[Process]:
    """
    Loads a CWL document from a raw dictionary.

    Args:
        `raw_process` (`dict`): The dictionary representing the CWL document
        `uri` (`Optional[str]`): The CWL document URI. Default to `io://`
        `cwl_version` (`Optional[str]`): The CWL document version. Default to `v1.2`

    Returns:
        `Processes`: The parsed CWL Process or Processes (if the CWL document is a `$graph`).
    """
    updated_process = raw_process
    document_has_graph = __CWL_GRAPH__ in raw_process

    if cwl_version != raw_process[__CWL_VERSION__]:
        logger.debug(
            f"Updating the model from version '{raw_process[__CWL_VERSION__]}' to version '{cwl_version}'..."
        )

        updated_process = _upgrade_document(
            raw_process=raw_process,
            cwl_version=cwl_version,
            uri=uri,
        )

        logger.debug(f"Raw CWL document successfully updated to {cwl_version}!")
    else:
        logger.debug(
            f"No needs to update the Raw CWL document since it targets already the {cwl_version}"
        )

    logger.debug("Parsing the raw CWL document to the CWL Utils DOM...")

    clean_uri, fragment = urldefrag(uri)

    if fragment:
        logger.debug(f"Ignoring fragment #{fragment} from URI {clean_uri}")

    process = load_document_by_yaml(yaml=updated_process, uri=clean_uri, load_all=True)

    logger.debug("Raw CWL document successfully parsed to the CWL Utils DOM!")

    logger.debug("Dereferencing the steps[].run...")

    dereferenced_process = _dereference_steps(
        process=process,
        uri=uri,
        session=session,
        loader=load_cwl_from_location,
    )

    logger.debug("steps[].run successfully dereferenced! Dereferencing the FQNs...")

    remove_refs(dereferenced_process)

    logger.debug("CWL document successfully dereferenced! Now verifying steps[].run integrity...")

    assert_connected_graph(dereferenced_process)

    logger.debug("All steps[].run link are resolvable! ")

    if sort:
        logger.debug("Sorting Process instances by dependencies....")
        dereferenced_process = order_graph_by_dependencies(dereferenced_process)
        logger.debug("Sorting process is over.")

    document_metadata = _extract_document_metadata(raw_process, process=dereferenced_process)
    _preserve_document_metadata(
        process=dereferenced_process,
        document_metadata=document_metadata,
        document_has_graph=document_has_graph,
    )

    return dereferenced_process if len(dereferenced_process) > 1 else dereferenced_process[0]


def load_cwl_from_stream(
    content: TextIO,
    uri: str = __DEFAULT_BASE_URI__,
    cwl_version: str = __TARGET_CWL_VERSION__,
    sort: bool = True,
    session: requests.Session = _global_session,
) -> Process | list[Process]:
    """
    Loads a CWL document from a stream of data.

    Args:
        `content` (`TextIO`): The stream where reading the CWL document
        `uri` (`Optional[str]`): The CWL document URI. Default to `io://`
        `cwl_version` (`Optional[str]`): The CWL document version. Default to `v1.2`

    Returns:
        `Processes`: The parsed CWL Process or Processes (if the CWL document is a `$graph`).
    """
    cwl_content = _yaml.load(content)

    logger.debug(f"CWL data of type {type(cwl_content)} successfully loaded from stream")

    return load_cwl_from_yaml(
        raw_process=cwl_content,
        uri=uri,
        cwl_version=cwl_version,
        sort=sort,
        session=session,
    )


def _local_source_path(path: str, session: requests.Session) -> Path | None:
    """Resolve local paths and file URIs, returning None for remote URLs.

    Raises:
        ValueError: If a file URI has a remote authority, query, or relative path.
    """
    parsed = urlparse(path)
    source_path = None
    if parsed.scheme == "file":
        if parsed.netloc.lower() not in ("", "localhost"):
            raise ValueError(f"Non-local file URI authority is not supported: {parsed.netloc}")
        if parsed.query:
            raise ValueError(f"File URI queries are not supported: {path}")
        source_path = Path(url2pathname(parsed.path))
        if not source_path.is_absolute():
            raise ValueError(f"File URI must contain an absolute path: {path}")
    elif not _is_url(path, session):
        source_path = Path(path)

    return source_path


def load_cwl_from_location(
    path: str,
    cwl_version: str = __TARGET_CWL_VERSION__,
    sort: bool = True,
    session: requests.Session = _global_session,
) -> Process | list[Process]:
    """
    Loads a CWL document from an HTTP(S) URL, local path, or local file URI.

    Local file URIs may use an empty authority or localhost. Percent-encoded
    paths are decoded before opening; relative references use the file URI.
    As for other sources, URI fragments do not select a process: the complete
    document is loaded.

    Args:
        `path` (`str`): The URL or a file on the local File System where reading the CWL document
        `uri` (`Optional[str]`): The CWL document URI. Default to `io://`
        `cwl_version` (`Optional[str]`): The CWL document version. Default to `v1.2`

    Returns:
        `Processes`: The parsed CWL Process or Processes (if the CWL document is a `$graph`).
    """
    logger.debug(f"Loading CWL document from {path}...")

    document_uri = path
    source_path = _local_source_path(path, session)

    if source_path is not None:
        document_uri = source_path.resolve().as_uri()

    def _load_cwl_from_stream(stream: TextIO) -> Process | list[Process]:
        """Load a stream using the resolved source URI and requested options."""
        logger.debug(f"Reading stream from {path}...")

        loaded = load_cwl_from_stream(
            content=stream,
            uri=document_uri,
            cwl_version=cwl_version,
            sort=sort,
            session=session,
        )

        logger.debug(f"Stream from {path} successfully load!")

        return loaded

    if source_path is None:
        response = session.get(path, stream=True)
        response.raise_for_status()

        # Read first 2 bytes to check for gzip
        magic = response.raw.read(2)
        remaining = response.raw.read()  # Read rest of the stream
        combined = BytesIO(magic + remaining)

        buffer = GzipFile(fileobj=combined) if magic == b"\x1f\x8b" else combined

        return _load_cwl_from_stream(TextIOWrapper(buffer, encoding=__DEFAULT_ENCODING__))
    if source_path.is_file():
        with source_path.open(encoding=__DEFAULT_ENCODING__) as f:
            return _load_cwl_from_stream(f)
    else:
        raise ValueError(f"Invalid source {path}: not a URL or existing file path")


def load_cwl_from_string_content(
    content: str,
    uri: str = __DEFAULT_BASE_URI__,
    cwl_version: str = __TARGET_CWL_VERSION__,
    sort: bool = True,
) -> Process | list[Process]:
    """
    Loads a CWL document from its textual representation.

    Args:
        `content` (`str`): The string text representing the CWL document
        `uri` (`Optional[str]`): The CWL document URI. Default to `io://`
        `cwl_version` (`Optional[str]`): The CWL document version. Default to `v1.2`

    Returns:
        `Processes`: The parsed CWL Process or Processes (if the CWL document is a `$graph`)
    """
    return load_cwl_from_stream(
        content=StringIO(content), uri=uri, cwl_version=cwl_version, sort=sort
    )


def dump_cwl(process: Process | list[Process], stream: TextIO) -> None:
    """Write a CWL process or graph to a text stream as YAML.

    Args:
        process: Process or graph to serialize with preserved document metadata.
        stream: Destination for the serialized document.
    """
    data = save(
        val=process,  # type: ignore
        relative_uris=False,
    )
    data = _restore_document_metadata(data=data, process=process)

    _yaml.dump(data=data, stream=stream)
