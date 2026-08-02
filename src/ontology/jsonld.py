"""Generate TerminusDB JSON-LD schema documents from the pydantic ontology.

The mapping below was validated against TerminusDB 12.0.6 by round-tripping each
shape through the live server, not inferred from documentation:

======================================  ==============================================
Python annotation                       TerminusDB schema value
======================================  ==============================================
``str``                                 ``"xsd:string"``
``int``                                 ``"xsd:integer"``
``float``                               ``"xsd:decimal"``
``bool``                                ``"xsd:boolean"``
``datetime``                            ``"xsd:dateTime"``
``dict[str, Any]``                      ``"sys:JSON"``
``StrEnum`` subclass                    a generated ``Enum`` class, linked by name
``Annotated[str, Link("Task")]``        ``"Task"`` (a document link)
``BaseModel`` subclass                  a generated ``@subdocument`` class
``X | None``                            ``{"@type": "Optional", "@class": X}``
``list[X]``                             ``{"@type": "List", "@class": X}`` (order-preserving)
``set[X]``                              ``{"@type": "Set", "@class": X}``
======================================  ==============================================

Two measured behaviours drive the generator:

1. ``@key`` is **not** inherited. A subclass of an abstract parent that omits
   ``@key`` receives a random id, so links built from ``entity_id`` silently miss.
   Every concrete class therefore gets an explicit ``Lexical`` key.
2. Referential integrity is enforced at insert time (``references_untyped_object``),
   including within a single request -- but a single request *is* a single
   transaction, so a batch that contains both endpoints of a link succeeds.
   Callers should submit an entity graph as one batch.
"""

from __future__ import annotations

import types
from datetime import datetime
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from ontology.schema import ALL_MODELS, ControlPlaneEntity, Link

__all__ = [
    "SCHEMA_CONTEXT",
    "terminus_schema_documents",
    "to_terminus_document",
    "from_terminus_document",
]

_SCALARS: dict[type, str] = {
    str: "xsd:string",
    bool: "xsd:boolean",
    int: "xsd:integer",
    float: "xsd:decimal",
    datetime: "xsd:dateTime",
}

#: The default prefix context TerminusDB writes into a fresh database. Emitted
#: alongside the classes so a schema dump is self-describing.
SCHEMA_CONTEXT: dict[str, Any] = {
    "@type": "@context",
    "@base": "terminusdb:///data/",
    "@schema": "terminusdb:///schema#",
}


class SchemaGenerationError(TypeError):
    """An annotation the generator cannot express in TerminusDB.

    Raised rather than silently degraded to ``sys:JSON``: an ontology field the
    graph cannot type is a field the graph cannot enforce, and Section 8.1
    forbids a silent default standing in for a material decision.
    """


def _link_target(annotation: Any) -> str | None:
    for meta in getattr(annotation, "__metadata__", ()):
        if isinstance(meta, Link):
            return meta.target
    return None


def _unwrap_annotated(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is not None and hasattr(annotation, "__metadata__"):
        return get_args(annotation)[0]
    return annotation


def _optional_inner(annotation: Any) -> Any | None:
    """Return the non-``None`` member of ``X | None``, else ``None``."""
    origin = get_origin(annotation)
    if origin is not Union and origin is not types.UnionType:
        return None
    args = [a for a in get_args(annotation) if a is not type(None)]
    if len(args) != len(get_args(annotation)) and len(args) == 1:
        return args[0]
    return None


def _class_type(annotation: Any, enums: dict[str, dict[str, Any]], subdocs: dict[str, dict[str, Any]]) -> Any:
    """Map a *non-container* annotation to a TerminusDB class or datatype."""
    target = _link_target(annotation)
    if target is not None:
        return target
    annotation = _unwrap_annotated(annotation)

    if annotation in _SCALARS:
        return _SCALARS[annotation]
    if annotation is Any:
        return "sys:JSON"
    origin = get_origin(annotation)
    if origin is dict:
        return "sys:JSON"
    if isinstance(annotation, type):
        if issubclass(annotation, Enum):
            _register_enum(annotation, enums)
            return annotation.__name__
        if issubclass(annotation, BaseModel):
            _register_subdocument(annotation, enums, subdocs)
            return annotation.__name__
    raise SchemaGenerationError(f"cannot express annotation {annotation!r} as a TerminusDB type")


def _field_type(annotation: Any, enums: dict[str, dict[str, Any]], subdocs: dict[str, dict[str, Any]]) -> Any:
    inner = _optional_inner(annotation)
    if inner is not None:
        return {"@type": "Optional", "@class": _field_type(inner, enums, subdocs)}

    bare = annotation if _link_target(annotation) else _unwrap_annotated(annotation)
    origin = get_origin(bare)
    if origin is list:
        # ``List``, not ``Set``: TerminusDB returns a Set in its own order, which
        # would change the entity body between write and read and break the
        # content hash (GATE-D1-02 A4). Measured: List round-trips order exactly,
        # accepts an empty list, and works for links as well as datatypes.
        (item,) = get_args(bare)
        return {"@type": "List", "@class": _class_type(item, enums, subdocs)}
    if origin is set or origin is frozenset:
        (item,) = get_args(bare)
        return {"@type": "Set", "@class": _class_type(item, enums, subdocs)}
    return _class_type(annotation, enums, subdocs)


def _register_enum(enum_cls: type[Enum], enums: dict[str, dict[str, Any]]) -> None:
    name = enum_cls.__name__
    if name in enums:
        return
    enums[name] = {
        "@type": "Enum",
        "@id": name,
        "@value": [str(member.value) for member in enum_cls],
    }


def _register_subdocument(
    model: type[BaseModel], enums: dict[str, dict[str, Any]], subdocs: dict[str, dict[str, Any]]
) -> None:
    name = model.__name__
    if name in subdocs:
        return
    subdocs[name] = {}  # placeholder guards against recursion
    doc: dict[str, Any] = {
        "@type": "Class",
        "@id": name,
        "@subdocument": [],
        "@key": {"@type": "Random"},
    }
    doc.update(_property_map(model, enums, subdocs, skip=()))
    subdocs[name] = doc


def _property_map(
    model: type[BaseModel],
    enums: dict[str, dict[str, Any]],
    subdocs: dict[str, dict[str, Any]],
    *,
    skip: tuple[str, ...],
) -> dict[str, Any]:
    hints = get_type_hints(model, include_extras=True)
    props: dict[str, Any] = {}
    for name in model.model_fields:
        if name in skip:
            continue
        props[name] = _field_type(hints[name], enums, subdocs)
    return props


def terminus_schema_documents(
    models: tuple[type[ControlPlaneEntity], ...] = ALL_MODELS,
    *,
    include_context: bool = False,
) -> list[dict[str, Any]]:
    """Return the JSON-LD schema documents that create the ontology.

    Ordering matters to a reader, not to TerminusDB: enums and subdocuments come
    first, then the abstract parent, then the concrete classes in contract order.
    """
    enums: dict[str, dict[str, Any]] = {}
    subdocs: dict[str, dict[str, Any]] = {}

    base_props = _property_map(ControlPlaneEntity, enums, subdocs, skip=())
    base_doc: dict[str, Any] = {
        "@type": "Class",
        "@id": ControlPlaneEntity.__name__,
        "@abstract": [],
        **base_props,
    }

    inherited = set(ControlPlaneEntity.model_fields)
    classes: list[dict[str, Any]] = []
    for model in models:
        doc: dict[str, Any] = {
            "@type": "Class",
            "@id": model.__name__,
            "@inherits": [ControlPlaneEntity.__name__],
            # Measured: @key is not inherited from an abstract parent.
            "@key": {"@type": "Lexical", "@fields": ["entity_id"]},
        }
        doc.update(_property_map(model, enums, subdocs, skip=tuple(inherited)))
        classes.append(doc)

    documents: list[dict[str, Any]] = []
    if include_context:
        documents.append(dict(SCHEMA_CONTEXT))
    documents.extend(enums[name] for name in sorted(enums))
    documents.extend(subdocs[name] for name in sorted(subdocs))
    documents.append(base_doc)
    documents.extend(classes)
    return documents


def _encode(value: Any) -> Any:
    if isinstance(value, BaseModel):
        payload = {k: _encode(v) for k, v in value.model_dump().items() if v is not None}
        payload["@type"] = type(value).__name__
        return payload
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


def to_terminus_document(entity: ControlPlaneEntity) -> dict[str, Any]:
    """Serialise one entity to a TerminusDB instance document.

    ``None`` values are omitted rather than written as JSON null: an ``Optional``
    property in TerminusDB is *absent*, and an explicit null is a schema-check
    failure.
    """
    doc: dict[str, Any] = {"@type": type(entity).__name__, "@id": entity.document_id}
    for name in type(entity).model_fields:
        value = getattr(entity, name)
        if value is None:
            continue
        if isinstance(value, BaseModel):
            doc[name] = _encode(value)
        else:
            doc[name] = _encode(value)
    return doc


def from_terminus_document(model: type[ControlPlaneEntity], doc: dict[str, Any]) -> ControlPlaneEntity:
    """Inverse of :func:`to_terminus_document` for one known model type."""
    payload = {k: v for k, v in doc.items() if not k.startswith("@")}
    envelope = payload.get("envelope")
    if isinstance(envelope, dict):
        payload["envelope"] = {k: v for k, v in envelope.items() if not k.startswith("@")}
    return model.model_validate(payload)
