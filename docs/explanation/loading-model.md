# The loading model

CWL workflows often span more than one document. A workflow step can point to a
tool or workflow stored in another file, an HTTP URL, an S3 object, or an OCI
artifact. CWL Loader provides one path for turning that distributed input into a
resolved `cwl-utils` object model.

## Loading is separate from execution

CWL Loader does not run workflows. It reads CWL documents, parses them, resolves
references, and writes CWL back to YAML. A workflow runner is still responsible
for executing the resulting workflow.

This separation is useful when you need to inspect, normalize, validate, or
resolve CWL before passing it to another system.

## Mandatory Calrissian requirements

When loading into the default CWL v1.2 DOM, CWL Loader automatically preserves
`DaskGatewayRequirement` under `requirements`. No caller options or changes to
Transpiler Mate resolvers and plugins are needed. This applies to packed graphs,
inline tools, and external step `run` documents as well as standalone tools.

The recognized class URIs are:

- `https://calrissian-cwl.github.io/schema#DaskGatewayRequirement`
- `http://calrissian-cwl.org/schema#DaskGatewayRequirement`

Namespace prefixes are resolved before recognition. Serialization retains the
original expanded URI and a deep copy of every payload field, including fields
unknown to this loader. The requirement stays mandatory; it is never moved to
`hints` or removed. The parsed envelope exposes `class_` and `save()` rather than
typed Dask field attributes.

This is opaque preservation for inspection and packaging, not validation of
the Calrissian field schema or a claim that an execution engine supports Dask.
The execution engine remains responsible for that contract. Standard CWL
requirements continue through the generated `cwl-utils` validators. Unsupported
mandatory classes and malformed standard requirements raise a `ValueError`
with the class and source location, preserving validation errors as causes.

The integration uses per-document loading options and public requirement
classes from `cwl-utils>=0.45`; it does not modify global parser registries.
Explicit non-v1.2 target versions retain their existing parser behavior.

## What resolution means

When the loader encounters a workflow step, it checks the step `run` value. If
the value points outside the current document, the loader imports that referenced
CWL document with the same request session.

After import, the loader rewrites external process references to local fragment
references such as `#stac`. This produces a document graph that can be serialized
as one resolved CWL document.

The loader then removes fully qualified references, verifies that step `run`
links point to available process IDs, and sorts process definitions by
dependency order.

## Source handling

`load_cwl_from_location()` accepts local filesystem paths and URLs supported by
the configured `requests.Session`.

Applications can configure the request session with adapters for additional URL
schemes. HTTP and HTTPS use the default Requests adapters. URL responses that
start with the gzip magic bytes are decompressed before YAML parsing.

## CWL version normalization

The loader targets CWL `v1.2`. If a document declares another CWL version, the
loader asks `cwltool` to update the raw model before parsing it with
`cwl-utils`.

This makes older documents easier to consume through one object model, but it
also means the serialized output can differ from the input even when the input
was already valid CWL.

## Python API and command-line bundling

Use the Python API when a program needs access to the parsed `Process` objects or
when you need to supply a custom request session.

The former `cwl-loader bundle` command has moved to
[Transpiler Mate](https://terradue.github.io/transpiler-mate/). Use Transpiler
Mate when the desired result is a resolved YAML file produced from the command
line.
