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

"""Preserve supported mandatory extensions without claiming execution support.

The opaque envelope is internal serialization state, not a Dask schema model.
Standard requirements still use the public generated CWL validators.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from cwl_utils.parser import LoadingOptions
from cwl_utils.parser import cwl_v1_2 as cwl
from schema_salad.exceptions import ValidationException
from schema_salad.runtime import Loader

_DASK_REQUIREMENTS = frozenset(
    (
        "https://calrissian-cwl.github.io/schema#DaskGatewayRequirement",
        "http://calrissian-cwl.org/schema#DaskGatewayRequirement",
    )
)


class RequirementLoadingError(ValueError):
    """A mandatory requirement cannot be represented or validated."""


def _expanded_class(name: str, options: LoadingOptions) -> str:
    prefix, separator, local = name.partition(":")
    if separator and prefix in options.namespaces:
        return options.namespaces[prefix] + local
    return name


class _PreservedRequirement(cwl.ProcessRequirement):
    """Retain an opaque requirement payload under its original expanded URI."""

    def __init__(self, payload: Mapping[str, Any], class_uri: str) -> None:
        self.class_ = class_uri
        self._payload = deepcopy(dict(payload))
        self._payload["class"] = class_uri

    @classmethod
    def fromDoc(
        cls,
        doc: Any,
        baseuri: str,
        loadingOptions: LoadingOptions,
        docRoot: str | None = None,
    ) -> _PreservedRequirement:
        """Capture the payload without interpreting extension-specific fields."""
        return cls(doc, _expanded_class(doc["class"], loadingOptions))

    def save(
        self, top: bool = False, base_url: str = "", relative_uris: bool = True
    ) -> dict[str, Any]:
        """Serialize independently of the surrounding document's namespaces."""
        return deepcopy(self._payload)


class _RequirementLoader(Loader):
    """Dispatch requirements to public CWL classes or the preservation envelope."""

    def load(
        self,
        doc: Any,
        baseuri: str,
        loadingOptions: LoadingOptions,
        docRoot: str | None = None,
        lc: Any | None = None,
    ) -> cwl.ProcessRequirement:
        """Load a requirement, preserving supported Calrissian payloads.

        Raises:
            RequirementLoadingError: If the class is missing, unsupported, or invalid.
        """
        if not isinstance(doc, Mapping) or not isinstance(doc.get("class"), str):
            raise RequirementLoadingError(f"Requirement in {baseuri} needs a string 'class'")
        class_uri = _expanded_class(doc["class"], loadingOptions)
        if class_uri in _DASK_REQUIREMENTS:
            return _PreservedRequirement.fromDoc(doc, baseuri, loadingOptions)

        # Use the local name only to select a validator. fromDoc also validates
        # the complete class URI, so foreign namespaces cannot impersonate CWL.
        local_name = class_uri.rsplit("#", 1)[-1].rsplit(":", 1)[-1]
        requirement_type = getattr(cwl, local_name, None)
        if (
            isinstance(requirement_type, type)
            and issubclass(requirement_type, cwl.ProcessRequirement)
            and requirement_type is not cwl.ProcessRequirement
        ):
            try:
                return requirement_type.fromDoc(doc, baseuri, loadingOptions, docRoot)
            except ValidationException as exc:
                raise RequirementLoadingError(
                    f"Invalid mandatory requirement {class_uri!r} in {baseuri}: {exc}"
                ) from exc
        raise RequirementLoadingError(
            f"Unsupported mandatory requirement {class_uri!r} in {baseuri}. "
            "Keep it under requirements; this loader needs support for its extension."
        )


def requirement_loading_options(version: str, uri: str) -> LoadingOptions | None:
    """Create per-document extension support for the normalized CWL v1.2 DOM."""
    if version != "v1.2":
        return None
    return LoadingOptions(fileuri=uri, loaders={"ProcessRequirementLoader": _RequirementLoader()})
