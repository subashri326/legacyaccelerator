"""Informatica PowerCenter XML reader producing Neutral IR.

PowerCenter wires transformations port to port (CONNECTOR elements), not table to table. This
reader keeps that wiring so the generated SQL uses the right column under the right name:

* Source Definition + Source Qualifier become one source query that selects the qualifier's
  ports, applying its source filter, user-defined join, distinct flag or SQL override (session
  overrides win over the mapping).
* Renamed ports become explicit column renames on the edge between steps.
* A step fed from several upstream steps of the same pipeline (common: a target fed by an
  Expression and by the Lookup after it) is fed from the furthest-downstream one, which carries
  all the columns.
* Expressions are translated exactly (see ir/etl_expressions.py); variable ports are inlined in
  evaluation order; anything stateful or without an exact equivalent becomes a review item.
* Router sends a row to every matching group; a connected Lookup returns NULLs when nothing
  matches; Joiner master/detail outer joins map to LEFT/RIGHT/FULL joins.
* Update Strategy, Sequence Generator, Mapplets, Normalizer, Stored Procedure, Java, etc. are
  flagged for review - they change how rows are written or generated and are never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from lxml import etree
from sqlglot import exp
from sqlglot.errors import SqlglotError

from accelerator.ir.etl_expressions import Scope, Untranslatable, informatica_kind, translate
from accelerator.ir.expressions import DIALECT_MAP
from accelerator.ir.models import (
    ColumnIR,
    DataFlowGraphIR,
    EdgeIR,
    ExpressionIR,
    NodeIR,
    NodeOperator,
    OpaqueSnippetIR,
    OrchestrationTaskIR,
    PackageIR,
    TaskDependencyIR,
)
from accelerator.plugins.interfaces import Parser, PluginManifest

DB_DIALECTS = {
    "microsoft sql server": "tsql",
    "sql server": "tsql",
    "oracle": "oracle",
    "teradata": "teradata",
    "postgresql": "postgres",
}

# Transformations that change how rows are generated or written; always reviewed.
REVIEW_TYPES = {
    "update strategy": (
        "INFA_UPDATE_STRATEGY",
        "Update Strategy decides insert/update/delete per row; the target load (e.g. a MERGE) "
        "must be designed and approved.",
    ),
    "sequence": (
        "INFA_SEQUENCE_GENERATOR",
        "Sequence Generator creates surrogate keys; use an identity column or sequence on the "
        "target and confirm key continuity.",
    ),
    "sequence generator": (
        "INFA_SEQUENCE_GENERATOR",
        "Sequence Generator creates surrogate keys; use an identity column or sequence on the "
        "target and confirm key continuity.",
    ),
    "mapplet": ("INFA_MAPPLET", "Mapplet logic is defined elsewhere; expand it for review."),
    "normalizer": ("INFA_NORMALIZER", "Normalizer pivots columns into rows."),
    "stored procedure": ("INFA_STORED_PROCEDURE", "Calls a database stored procedure."),
    "java": ("INFA_JAVA", "Custom Java code."),
    "rank": ("INFA_RANK", "Rank keeps top/bottom rows per group."),
    "transaction control": ("INFA_TRANSACTION_CONTROL", "Controls commits per row."),
    "sql": ("INFA_SQL_TRANSFORMATION", "Runs SQL per row."),
    "xml parser": ("INFA_XML", "XML parsing."),
    "xml generator": ("INFA_XML", "XML generation."),
    "http": ("INFA_HTTP", "Calls a web service."),
}


def _is_param(text: str | None) -> bool:
    return bool(text) and bool(re.search(r"\$\$?\w", text or ""))


@dataclass
class _Port:
    name: str
    datatype: str
    porttype: str
    expression: str = ""
    expressiontype: str = ""
    group: str = ""
    ref_field: str = ""
    default: str = ""
    scale: str = "0"

    @property
    def kind(self) -> str:
        return informatica_kind(self.datatype, self.scale)

    def has(self, word: str) -> bool:
        return word in re.split(r"[/ ]+", self.porttype.upper())


@dataclass
class _Conn:
    from_inst: str
    from_field: str
    to_inst: str
    to_field: str


@dataclass
class _Built:
    node: NodeIR
    # port name -> (branch, column name in the node's output relation)
    out_ports: dict[str, tuple[str, str]] = field(default_factory=dict)
    # input port groups: group key -> set of port names (for multi-input steps)
    in_groups: dict[str, list[str]] = field(default_factory=dict)
    strict_inputs: bool = False
    is_generator: bool = False  # produces rows/values without inputs (e.g. Sequence Generator)


def _attrs(el: etree._Element | None) -> dict[str, str]:
    if el is None:
        return {}
    return {
        (a.attrib.get("NAME") or "").strip().lower(): a.attrib.get("VALUE") or ""
        for a in el.xpath("./TABLEATTRIBUTE | ./ATTRIBUTE")
    }


def _ports(el: etree._Element | None) -> list[_Port]:
    if el is None:
        return []
    out = []
    for f in el.xpath("./TRANSFORMFIELD | ./SOURCEFIELD | ./TARGETFIELD"):
        a = f.attrib
        out.append(
            _Port(
                name=a.get("NAME", ""),
                datatype=a.get("DATATYPE", ""),
                porttype=a.get("PORTTYPE", "INPUT/OUTPUT"),
                expression=a.get("EXPRESSION", ""),
                expressiontype=a.get("EXPRESSIONTYPE", ""),
                group=a.get("GROUP", ""),
                ref_field=a.get("REF_FIELD", ""),
                default=a.get("DEFAULTVALUE", ""),
                scale=a.get("SCALE", "0"),
            )
        )
    return out


def _expr_ir(
    eid: str,
    raw: str,
    scope: Scope,
    target: str | None = None,
    context: str = "value",
    aggregates: bool = False,
) -> ExpressionIR:
    try:
        sql, _ = translate(raw, "informatica", scope, context=context, allow_aggregates=aggregates)
        return ExpressionIR(
            id=eid,
            target_column=target,
            original_expr=raw,
            source_dialect="informatica",
            neutral_sql=sql,
        )
    except Untranslatable as exc:
        return ExpressionIR(
            id=eid,
            target_column=target,
            original_expr=raw,
            source_dialect="informatica",
            is_opaque=True,
            opaque_reason=str(exc),
        )


class _MappingReader:
    def __init__(
        self,
        artifact: str,
        folder: _Folder,
        mapping: etree._Element,
        flow_id: str,
        overrides: dict[str, dict[str, str]],
    ) -> None:
        self.artifact = artifact
        self.f = folder
        self.m = mapping
        self.flow_id = flow_id
        self.overrides = {k.lower(): v for k, v in overrides.items()}
        self.instances = {i.attrib.get("NAME", ""): i for i in mapping.xpath("./INSTANCE")}
        self.local_defs = {t.attrib.get("NAME", ""): t for t in mapping.xpath("./TRANSFORMATION")}
        self.conns = [
            _Conn(
                c.attrib.get("FROMINSTANCE", ""),
                c.attrib.get("FROMFIELD", ""),
                c.attrib.get("TOINSTANCE", ""),
                c.attrib.get("TOFIELD", ""),
            )
            for c in mapping.xpath("./CONNECTOR")
        ]
        self.built: dict[str, _Built] = {}
        self.opaque: list[OpaqueSnippetIR] = []

    # -- helpers -------------------------------------------------------------------------------
    def itype(self, name: str) -> str:
        inst = self.instances[name]
        return (inst.attrib.get("TRANSFORMATION_TYPE") or inst.attrib.get("TYPE") or "").lower()

    def tdef(self, name: str) -> etree._Element | None:
        inst = self.instances[name]
        tname = inst.attrib.get("TRANSFORMATION_NAME") or name
        kind = (inst.attrib.get("TYPE") or "").upper()
        if kind == "SOURCE":
            return self.f.source(tname)
        if kind == "TARGET":
            return self.f.target(tname)
        found = self.local_defs.get(tname)
        return found if found is not None else self.f.reusable.get(tname)

    def attrs(self, name: str) -> dict[str, str]:
        merged = _attrs(self.tdef(name))
        merged.update({k.lower(): v for k, v in self.overrides.get(name.lower(), {}).items()})
        return merged

    def location(self, name: str) -> str:
        return f"{self.artifact}/{self.flow_id}/{name}"

    def snippet(self, name: str) -> str:
        el = self.tdef(name)
        return etree.tostring(el if el is not None else self.instances[name], encoding="unicode")[
            :2000
        ]

    def review(self, name: str, ttype: str, code: str, detail: str) -> NodeIR:
        op = OpaqueSnippetIR(
            node_id=name,
            component_name=name,
            component_type=ttype,
            reason_code=code,
            reason_detail=detail,
            original_snippet=self.snippet(name),
            location=self.location(name),
        )
        self.opaque.append(op)
        return NodeIR(
            id=name,
            name=name,
            operator=NodeOperator.OPAQUE,
            source_component_type=ttype,
            opaque=op,
        )

    def incoming(self, name: str) -> list[_Conn]:
        return [c for c in self.conns if c.to_inst == name]

    def scope_of(self, ports: list[_Port], word: str = "INPUT") -> Scope:
        return Scope.of({p.name: p.kind for p in ports if p.has(word)})

    # -- per transformation --------------------------------------------------------------------
    def build_source_qualifier(self, name: str) -> _Built:
        ports = [p for p in _ports(self.tdef(name)) if p.has("OUTPUT")]
        attrs = self.attrs(name)
        assoc = [
            a.attrib.get("NAME", "")
            for a in self.instances[name].xpath("./ASSOCIATED_SOURCE_INSTANCE")
        ]
        feeders = {c.from_inst for c in self.incoming(name)}
        src_insts = [s for s in dict.fromkeys(assoc + sorted(feeders)) if s in self.instances]
        src_defs = [self.tdef(s) for s in src_insts]
        if not src_defs or any(d is None for d in src_defs):
            return _Built(
                self.review(
                    name,
                    "Source Qualifier",
                    "INFA_SOURCE_UNKNOWN",
                    "The source definition read by this qualifier is not in the export.",
                )
            )
        dbtypes = {(d.attrib.get("DATABASETYPE") or "").lower() for d in src_defs}  # type: ignore[union-attr]
        if "flat file" in dbtypes:
            return _Built(
                self.review(
                    name,
                    "Source Qualifier",
                    "INFA_FLAT_FILE_SOURCE",
                    "Reads a flat file; land the file in a table first.",
                )
            )
        dialect = next((DB_DIALECTS[t] for t in dbtypes if t in DB_DIALECTS), None)
        query = attrs.get("sql query", "").strip()
        sql_bits = [query, attrs.get("user defined join", ""), attrs.get("source filter", "")]
        if any(_is_param(b) for b in sql_bits):
            return _Built(
                self.review(
                    name,
                    "Source Qualifier",
                    "INFA_PARAMETER_IN_SQL",
                    "The source SQL uses mapping/session parameters ($$...); "
                    "their run-time values must be designed.",
                )
            )
        read = DIALECT_MAP.get(dialect or "tsql", "tsql")
        if query:
            try:
                tree = sqlglot.parse_one(query, read=read)
            except (SqlglotError, ValueError):
                tree = None
            if not isinstance(tree, exp.Select) or len(tree.expressions) != len(ports):
                return _Built(
                    self.review(
                        name,
                        "Source Qualifier",
                        "INFA_SQL_OVERRIDE_COLUMNS",
                        "The SQL override's columns could not be matched to the qualifier's ports "
                        "(they are matched by position).",
                    )
                )
            tree.set(
                "expressions",
                [
                    exp.alias_(e.unalias(), p.name)
                    for e, p in zip(tree.expressions, ports, strict=True)
                ],
            )
            sql = tree.sql(dialect=read)
        else:
            if len(src_defs) > 1 and not attrs.get("user defined join", "").strip():
                return _Built(
                    self.review(
                        name,
                        "Source Qualifier",
                        "INFA_SQ_JOIN_UNKNOWN",
                        "Joins several sources without a user-defined join; the join Informatica "
                        "derives from key relationships must be confirmed.",
                    )
                )
            field_of: dict[str, tuple[str, str]] = {}
            for c in self.incoming(name):
                if c.from_inst in src_insts:
                    field_of[c.to_field.lower()] = (c.from_inst, c.from_field)
            select = []
            for p in ports:
                src = field_of.get(p.name.lower())
                if src is None:
                    return _Built(
                        self.review(
                            name,
                            "Source Qualifier",
                            "INFA_SQ_PORT_UNWIRED",
                            f"Port {p.name} is not connected to a source column.",
                        )
                    )
                alias = self.instances[src[0]].attrib.get("TRANSFORMATION_NAME") or src[0]
                select.append(f"{alias}.{src[1]} AS {p.name}")
            tables = []
            for inst, d in zip(src_insts, src_defs, strict=True):
                owner = d.attrib.get("OWNERNAME") or ""  # type: ignore[union-attr]
                tname = d.attrib.get("NAME") or inst  # type: ignore[union-attr]
                alias = self.instances[inst].attrib.get("TRANSFORMATION_NAME") or inst
                tables.append(f"{owner + '.' if owner else ''}{tname} {alias}")
            where = [
                w.strip()
                for w in (attrs.get("user defined join", ""), attrs.get("source filter", ""))
                if w.strip()
            ]
            distinct = "DISTINCT " if attrs.get("select distinct", "").upper() == "YES" else ""
            sql = f"SELECT {distinct}{', '.join(select)} FROM {', '.join(tables)}"
            if where:
                sql += " WHERE " + " AND ".join(f"({w})" for w in where)
        node = NodeIR(
            id=name,
            name=name,
            operator=NodeOperator.SOURCE,
            source_component_type="Source Qualifier",
            table_ref=None,
            sql_query=sql,
            output_columns=[ColumnIR(name=p.name, data_type=p.datatype) for p in ports],
            metadata={
                "sql_dialect": dialect or "",
                "query_columns_aligned": True,
                "source_tables": [
                    ".".join(x for x in (d.attrib.get("OWNERNAME"), d.attrib.get("NAME")) if x)
                    for d in src_defs
                ],
            },  # type: ignore[union-attr]
        )
        return _Built(node, {p.name.lower(): ("", p.name) for p in ports})

    def build_expression(self, name: str) -> _Built:
        ports = _ports(self.tdef(name))
        scope = self.scope_of(ports)
        variables = [p for p in ports if p.has("VARIABLE")]
        scope.pending_variables = {v.name for v in variables}
        for v in variables:  # evaluated in order, before output ports
            scope.pending_variables.discard(v.name)
            pending_self = scope.pending_variables | {v.name}
            vscope = Scope(
                columns=scope.columns,
                variables=dict(scope.variables),
                pending_variables=pending_self,
            )
            try:
                scope.variables[v.name] = translate(v.expression, "informatica", vscope)
            except Untranslatable as exc:
                scope.variables[v.name] = exc
        exprs = []
        for p in ports:
            if not p.has("OUTPUT") or p.has("INPUT"):
                continue
            exprs.append(_expr_ir(f"{name}:{p.name}", p.expression or "NULL", scope, p.name))
        node = NodeIR(
            id=name,
            name=name,
            operator=NodeOperator.EXPRESSION,
            source_component_type="Expression",
            expressions=exprs,
            output_columns=[
                ColumnIR(name=p.name, data_type=p.datatype) for p in ports if p.has("OUTPUT")
            ],
        )
        return _Built(node, {p.name.lower(): ("", p.name) for p in ports if p.has("OUTPUT")})

    def build_filter(self, name: str) -> _Built:
        ports = _ports(self.tdef(name))
        cond = self.attrs(name).get("filter condition", "").strip() or "TRUE"
        node = NodeIR(
            id=name,
            name=name,
            operator=NodeOperator.FILTER,
            source_component_type="Filter",
            condition=_expr_ir(f"{name}:filter", cond, self.scope_of(ports), context="condition"),
        )
        return _Built(node, {p.name.lower(): ("", p.name) for p in ports})

    def build_router(self, name: str) -> _Built:
        t = self.tdef(name)
        ports = _ports(t)
        groups = t.xpath("./GROUP") if t is not None else []
        in_group = next(
            (
                g.attrib.get("NAME", "")
                for g in groups
                if (g.attrib.get("TYPE") or "").upper() == "INPUT"
            ),
            "INPUT",
        )
        inputs = [p for p in ports if p.group == in_group or (not p.group and p.has("INPUT"))]
        scope = Scope.of({p.name: p.kind for p in inputs})
        routes, exprs, out_ports = [], [], {}
        default_group = ""
        for g in sorted(groups, key=lambda g: int(g.attrib.get("ORDER", "0") or 0)):
            gname, gtype = g.attrib.get("NAME", ""), (g.attrib.get("TYPE") or "").upper()
            if gtype == "OUTPUT/DEFAULT":
                default_group = gname
            elif gtype == "OUTPUT":
                e = _expr_ir(
                    f"{name}:{gname}",
                    g.attrib.get("EXPRESSION") or "TRUE",
                    scope,
                    context="condition",
                )
                exprs.append(e)
                routes.append({"name": gname, "condition": e.neutral_sql})
        in_names = [p.name for p in inputs]
        for p in ports:
            if p.group in ("", in_group):
                continue
            ref = p.ref_field or next(
                (n for n in in_names if re.fullmatch(re.escape(n) + r"\d*", p.name, re.IGNORECASE)),
                "",
            )
            branch = "__default__" if p.group == default_group else p.group
            out_ports[p.name.lower()] = (branch, ref or p.name)
        node = NodeIR(
            id=name,
            name=name,
            operator=NodeOperator.ROUTER,
            source_component_type="Router",
            expressions=exprs,
            routes=routes,
            metadata={"route_semantics": "all"},
        )
        return _Built(node, out_ports)

    def build_lookup(self, name: str) -> _Built | None:
        ports = _ports(self.tdef(name))
        attrs = self.attrs(name)
        if not self.incoming(name):
            return None  # unconnected lookup: only callable from expressions (flagged there)
        if attrs.get("lookup sql override", "").strip():
            return _Built(
                self.review(
                    name,
                    "Lookup Procedure",
                    "INFA_LOOKUP_SQL_OVERRIDE",
                    "Lookup uses a SQL override; its result set must be reviewed.",
                )
            )
        policy = attrs.get("lookup policy on multiple match", "Use Any Value").strip().lower()
        if policy == "report error":
            return _Built(
                self.review(
                    name,
                    "Lookup Procedure",
                    "INFA_LOOKUP_MULTIPLE_MATCH",
                    "Lookup is set to fail on multiple matches.",
                )
            )
        lookup_ports = {p.name.lower(): p for p in ports if p.has("LOOKUP")}
        input_ports = {p.name.lower(): p for p in ports if p.has("INPUT")}
        keys = []
        for part in re.split(
            r"\s+AND\s+", attrs.get("lookup condition", "").strip(), flags=re.IGNORECASE
        ):
            m = re.fullmatch(r"\s*(\w+)\s*(=|<>|!=|<=|>=|<|>)\s*(\w+)\s*", part)
            if not m:
                keys = []
                break
            a, op, b = m.groups()
            if op != "=":
                return _Built(
                    self.review(
                        name,
                        "Lookup Procedure",
                        "INFA_LOOKUP_NON_EQUAL",
                        f"Lookup condition uses '{op}'.",
                    )
                )
            if a.lower() in lookup_ports and b.lower() in input_ports:
                keys.append(
                    {"left": input_ports[b.lower()].name, "right": lookup_ports[a.lower()].name}
                )
            elif b.lower() in lookup_ports and a.lower() in input_ports:
                keys.append(
                    {"left": input_ports[a.lower()].name, "right": lookup_ports[b.lower()].name}
                )
            else:
                keys = []
                break
        defaults = [
            p.name
            for p in lookup_ports.values()
            if p.has("OUTPUT") and p.default and not p.default.upper().startswith("ERROR")
        ]
        if defaults:
            return _Built(
                self.review(
                    name,
                    "Lookup Procedure",
                    "INFA_LOOKUP_DEFAULT_VALUE",
                    f"Ports {defaults} return a default value when nothing matches.",
                )
            )
        source_filter = attrs.get("lookup source filter", "").strip()
        if _is_param(source_filter):
            return _Built(
                self.review(
                    name,
                    "Lookup Procedure",
                    "INFA_PARAMETER_IN_SQL",
                    "Lookup source filter uses parameters ($$...).",
                )
            )
        table = attrs.get("lookup table name", "").strip()
        src = self.f.source(table)
        dialect = (
            DB_DIALECTS.get((src.attrib.get("DATABASETYPE") or "").lower(), "")
            if (src is not None)
            else ""
        )
        copies = [
            {"reference": p.name, "alias": p.name} for p in lookup_ports.values() if p.has("OUTPUT")
        ]
        node = NodeIR(
            id=name,
            name=name,
            operator=NodeOperator.LOOKUP,
            source_component_type="Lookup Procedure",
            table_ref=table,
            join_keys=keys,
            metadata={
                "copy_columns": copies,
                "on_no_match": "keep",
                "reference_filter": source_filter,
                "sql_dialect": dialect,
                "return_all_matches": policy in ("use all values", "return all values"),
            },
        )
        outs = {p.name.lower(): ("", p.name) for p in ports if p.has("OUTPUT")}
        return _Built(node, outs)

    def build_joiner(self, name: str) -> _Built:
        ports = _ports(self.tdef(name))
        attrs = self.attrs(name)
        master = {p.name.lower(): p.name for p in ports if p.has("MASTER")}
        detail = {p.name.lower(): p.name for p in ports if not p.has("MASTER")}
        keys = []
        for part in re.split(
            r"\s+AND\s+", attrs.get("join condition", "").strip(), flags=re.IGNORECASE
        ):
            m = re.fullmatch(r"\s*(\w+)\s*=\s*(\w+)\s*", part)
            if not m:
                keys = []
                break
            a, b = (x.lower() for x in m.groups())
            if a in master and b in detail:
                keys.append({"left": detail[b], "right": master[a]})
            elif b in master and a in detail:
                keys.append({"left": detail[a], "right": master[b]})
            else:
                keys = []
                break
        jt = {
            "normal join": "INNER",
            "master outer join": "LEFT",
            "detail outer join": "RIGHT",
            "full outer join": "FULL",
        }.get(attrs.get("join type", "Normal Join").lower())
        node = NodeIR(
            id=name,
            name=name,
            operator=NodeOperator.JOIN,
            source_component_type="Joiner",
            join_type=jt,
            join_keys=keys if jt else [],
        )
        built = _Built(node, {p.name.lower(): ("", p.name) for p in ports}, strict_inputs=True)
        built.in_groups = {"detail": list(detail.values()), "master": list(master.values())}
        return built

    def build_aggregator(self, name: str) -> _Built:
        ports = _ports(self.tdef(name))
        scope = self.scope_of(ports)
        downstream = {c.from_field.lower() for c in self.conns if c.from_inst == name}
        groups = [p.name for p in ports if p.expressiontype.upper() == "GROUPBY"]
        aggs, problems = [], []
        for p in ports:
            if not p.has("OUTPUT") or p.name in groups:
                continue
            if p.has("INPUT") or not p.expression or p.expression == p.name:
                if p.name.lower() in downstream:
                    problems.append(p.name)
                continue
            e = _expr_ir(f"{name}:{p.name}", p.expression, scope, p.name, aggregates=True)
            if e.is_opaque:
                return _Built(
                    self.review(
                        name,
                        "Aggregator",
                        "INFA_AGGREGATE_EXPRESSION",
                        f"{p.name}: {e.opaque_reason}",
                    )
                )
            tree = sqlglot.parse_one(e.neutral_sql, read="postgres")
            if not list(tree.find_all(exp.AggFunc)):
                problems.append(p.name)
                continue
            aggs.append({"alias": p.name, "sql": e.neutral_sql})
        if problems:
            return _Built(
                self.review(
                    name,
                    "Aggregator",
                    "INFA_AGG_NON_GROUPED_PORT",
                    f"Ports {problems} are neither grouped nor aggregated; Informatica returns the "
                    "last row's value, which has no set-based equivalent.",
                )
            )
        node = NodeIR(
            id=name,
            name=name,
            operator=NodeOperator.AGGREGATE,
            source_component_type="Aggregator",
            group_by_columns=groups,
            metadata={"aggregations": aggs},
        )
        outs = {g.lower(): ("", g) for g in groups} | {
            a["alias"].lower(): ("", a["alias"]) for a in aggs
        }
        return _Built(node, outs, strict_inputs=True)

    def build_sorter(self, name: str) -> _Built:
        ports = _ports(self.tdef(name))
        distinct = self.attrs(name).get("distinct", "").upper() == "YES"
        node = NodeIR(
            id=name,
            name=name,
            operator=NodeOperator.DEDUPLICATE if distinct else NodeOperator.SORT,
            source_component_type="Sorter",
            dedup_keys=[p.name for p in ports] if distinct else [],
        )
        return _Built(node, {p.name.lower(): ("", p.name) for p in ports}, strict_inputs=distinct)

    def build_union(self, name: str) -> _Built:
        t = self.tdef(name)
        ports = _ports(t)
        groups = t.xpath("./GROUP") if t is not None else []
        out_group = next(
            (
                g.attrib.get("NAME", "")
                for g in groups
                if (g.attrib.get("TYPE") or "").upper() == "OUTPUT"
            ),
            "OUTPUT",
        )
        outs = [p for p in ports if p.group == out_group]
        in_groups: dict[str, list[str]] = {}
        to_output: dict[str, str] = {}
        for g in sorted(groups, key=lambda g: int(g.attrib.get("ORDER", "0") or 0)):
            gname = g.attrib.get("NAME", "")
            if gname == out_group:
                continue
            members = [p for p in ports if p.group == gname]
            in_groups[gname] = [p.name for p in members]
            for i, p in enumerate(members):
                target = p.ref_field or (outs[i].name if i < len(outs) else "")
                to_output[p.name.lower()] = target
        node = NodeIR(
            id=name, name=name, operator=NodeOperator.UNION, source_component_type="Union"
        )
        built = _Built(node, {p.name.lower(): ("", p.name) for p in outs}, strict_inputs=True)
        built.in_groups = in_groups
        built.node.metadata["to_output"] = to_output
        return built

    def build_target(self, name: str) -> _Built:
        tdef = self.tdef(name)
        tname = (
            tdef.attrib.get("NAME")
            if tdef is not None
            else (self.instances[name].attrib.get("TRANSFORMATION_NAME") or name)
        )
        attrs = self.overrides.get(name.lower(), {})
        node = NodeIR(
            id=name,
            name=name,
            operator=NodeOperator.TARGET,
            source_component_type="Target Definition",
            table_ref=tname,
            metadata={
                "truncate_before_load": str(attrs.get("Truncate target table option", "")).upper()
                in ("YES", "1")
            },
        )
        return _Built(node)

    # -- assembly ------------------------------------------------------------------------------
    def build_node(self, name: str) -> _Built | None:
        t = self.itype(name)
        tdef = self.tdef(name)
        template = (tdef.attrib.get("TEMPLATENAME") or "").lower() if tdef is not None else ""
        if t in ("source definition", "source"):
            return None  # folded into its Source Qualifier
        if t in ("source qualifier", "application source qualifier"):
            return self.build_source_qualifier(name)
        if t == "target definition" or (self.instances[name].attrib.get("TYPE") or "") == "TARGET":
            return self.build_target(name)
        handlers = {
            "expression": self.build_expression,
            "filter": self.build_filter,
            "router": self.build_router,
            "lookup procedure": self.build_lookup,
            "lookup": self.build_lookup,
            "joiner": self.build_joiner,
            "aggregator": self.build_aggregator,
            "sorter": self.build_sorter,
            "union": self.build_union,
        }
        if t == "custom transformation" and "union" in template:
            return self.build_union(name)
        if t in handlers:
            return handlers[t](name)
        code, detail = REVIEW_TYPES.get(
            t, ("INFA_UNSUPPORTED_TRANSFORMATION", f"Transformation type '{t}' needs review.")
        )
        built = _Built(self.review(name, t or "Unknown", code, detail))
        built.is_generator = not self.incoming(name)
        return built

    def ancestors(self) -> dict[str, set[str]]:
        ups: dict[str, set[str]] = {n: set() for n in self.instances}
        for c in self.conns:
            if c.to_inst in ups:
                ups[c.to_inst].add(c.from_inst)
        memo: dict[str, set[str]] = {}

        def walk(n: str, seen: frozenset[str] = frozenset()) -> set[str]:
            if n in memo:
                return memo[n]
            out: set[str] = set()
            for u in ups.get(n, set()):
                if u not in seen:
                    out |= {u} | walk(u, seen | {n})
            memo[n] = out
            return out

        return {n: walk(n) for n in self.instances}

    def read(self) -> DataFlowGraphIR:
        for name in self.instances:
            b = self.build_node(name)
            if b is not None:
                self.built[name] = b
        anc = self.ancestors()
        edges: list[EdgeIR] = []
        for name, b in list(self.built.items()):
            conns = [c for c in self.incoming(name) if c.from_inst in self.built]
            if not conns:
                continue
            gens = {c.from_inst for c in conns if self.built[c.from_inst].is_generator}
            conns = [c for c in conns if c.from_inst not in gens]
            if b.in_groups:
                group_of = {p.lower(): g for g, ps in b.in_groups.items() for p in ps}
                grouped: dict[str, list[_Conn]] = {}
                for c in conns:
                    grouped.setdefault(group_of.get(c.to_field.lower(), ""), []).append(c)
                order = list(b.in_groups)
                batches = [grouped[g] for g in order if g in grouped]
            else:
                batches = [conns] if conns else []
            for batch in batches:
                edge = self.edge_for(name, b, batch, anc)
                if edge is None:
                    self.built[name] = _Built(
                        self.review(
                            name,
                            b.node.source_component_type,
                            "INFA_MULTIPLE_PIPELINES",
                            "Columns arrive from steps that are not on one pipeline; the row "
                            "alignment must be reviewed.",
                        )
                    )
                    edges = [e for e in edges if e.target_node_id != name]
                    break
                edges.append(edge)
        for b in self.built.values():
            b.node.metadata.pop("to_output", None)
        nodes = [b.node for b in self.built.values()]
        return DataFlowGraphIR(id=self.flow_id, name=self.flow_id, nodes=nodes, edges=edges)

    def edge_for(
        self, name: str, b: _Built, conns: list[_Conn], anc: dict[str, set[str]]
    ) -> EdgeIR | None:
        ups = {c.from_inst for c in conns}
        down = next((d for d in ups if ups - {d} <= anc.get(d, set())), None)
        if down is None:
            return None
        src_b = self.built[down]
        branch = "default"
        lineage = []
        to_output = b.node.metadata.get("to_output") or {}
        for c in conns:
            if c.from_inst == down:
                br, col = src_b.out_ports.get(c.from_field.lower(), ("", c.from_field))
                branch = br or branch
            else:  # an upstream column carried through the passive steps in between
                col = c.from_field
            to = to_output.get(c.to_field.lower(), c.to_field) if to_output else c.to_field
            lineage.append({"from": col, "to": to})
        if b.node.operator == NodeOperator.TARGET:
            b.node.metadata["column_mapping"] = [
                {"source": m["from"], "target": m["to"]} for m in lineage
            ]
        return EdgeIR(
            id=f"{down}_to_{name}_{len(conns)}",
            source_node_id=down,
            target_node_id=name,
            branch_name=branch,
            column_lineage=lineage,
            projection="strict" if b.strict_inputs else "keep",
        )


class _Folder:
    def __init__(self, folders: list[etree._Element]) -> None:
        self.sources: dict[str, etree._Element] = {}
        self.targets: dict[str, etree._Element] = {}
        self.reusable: dict[str, etree._Element] = {}
        self.shortcuts: dict[str, str] = {}
        for f in folders:
            for s in f.xpath("./SOURCE"):
                self.sources[s.attrib.get("NAME", "").lower()] = s
            for t in f.xpath("./TARGET"):
                self.targets[t.attrib.get("NAME", "").lower()] = t
            for t in f.xpath("./TRANSFORMATION"):
                self.reusable[t.attrib.get("NAME", "")] = t
            for sc in f.xpath("./SHORTCUT"):
                self.shortcuts[sc.attrib.get("NAME", "").lower()] = sc.attrib.get(
                    "REFOBJECTNAME", ""
                ).lower()

    def _find(self, pool: dict[str, etree._Element], name: str) -> etree._Element | None:
        key = name.split(".")[-1].lower()
        found = pool.get(key)
        return found if found is not None else pool.get(self.shortcuts.get(key, ""))

    def source(self, name: str) -> etree._Element | None:
        return self._find(self.sources, name)

    def target(self, name: str) -> etree._Element | None:
        return self._find(self.targets, name)


TASK_REVIEW = {
    "command": ("INFA_COMMAND_TASK", "Runs operating-system commands."),
    "worklet": ("INFA_WORKLET", "Worklet contents must be expanded."),
    "decision": ("INFA_DECISION_TASK", "Branches the workflow on a condition."),
    "assignment": ("INFA_ASSIGNMENT_TASK", "Sets workflow variables."),
    "timer": ("INFA_TIMER_TASK", "Waits for a time."),
    "event wait": ("INFA_EVENT_TASK", "Waits for a file or event."),
    "event raise": ("INFA_EVENT_TASK", "Raises an event."),
    "email": ("INFA_EMAIL_TASK", "Sends an email notification."),
    "control": ("INFA_CONTROL_TASK", "Stops or fails the workflow."),
}


class InformaticaParser(Parser):
    """Parse Informatica PowerCenter XML exports (mappings, sessions, workflows) into Neutral IR."""

    manifest = PluginManifest(
        plugin_type="Parser",
        name="informatica_parser",
        version="1.0.0",
        supported_versions=["PowerCenter 9.x", "PowerCenter 10.x"],
        capabilities={
            "mappings": True,
            "workflows": True,
            "expressions": True,
            "session_overrides": True,
        },
        description="Parses Informatica PowerCenter XML into Neutral IR.",
    )

    def parse_artifact(self, artifact_name: str, content_bytes: bytes) -> PackageIR:
        xml = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        root = etree.fromstring(content_bytes, parser=xml)
        folders = root.xpath(".//FOLDER") or [root]
        folder = _Folder(folders)
        mappings = {m.attrib.get("NAME", ""): m for f in folders for m in f.xpath("./MAPPING")}
        workflows = [w for f in folders for w in f.xpath("./WORKFLOW")]
        sessions = {
            s.attrib.get("NAME", ""): s
            for f in folders
            for s in f.xpath("./SESSION | ./WORKFLOW/SESSION | ./WORKLET/SESSION")
        }

        def overrides_of(sess: etree._Element) -> dict[str, dict[str, str]]:
            out: dict[str, dict[str, str]] = {}
            for st in sess.xpath("./SESSTRANSFORMATIONINST"):
                attrs = {
                    a.attrib.get("NAME", ""): a.attrib.get("VALUE", "")
                    for a in st.xpath("./ATTRIBUTE")
                    if a.attrib.get("VALUE")
                }
                if attrs:
                    out[st.attrib.get("SINSTANCENAME", "")] = attrs
            return out

        flows: dict[str, DataFlowGraphIR] = {}
        opaque: list[OpaqueSnippetIR] = []
        flow_for_session: dict[str, str] = {}

        def read(mname: str, flow_id: str, ov: dict[str, dict[str, str]]) -> None:
            if flow_id in flows or mname not in mappings:
                return
            r = _MappingReader(artifact_name, folder, mappings[mname], flow_id, ov)
            flows[flow_id] = r.read()
            opaque.extend(r.opaque)

        for sname, sess in sessions.items():
            mname = sess.attrib.get("MAPPINGNAME", "")
            ov = overrides_of(sess)
            # Only overrides that change SQL/rows need their own flow.
            flow_id = (
                f"{mname}@{sname}"
                if any(
                    k.lower()
                    in (
                        "sql query",
                        "source filter",
                        "user defined join",
                        "lookup sql override",
                        "lookup source filter",
                    )
                    for a in ov.values()
                    for k in a
                )
                else mname
            )
            read(mname, flow_id, ov)
            flow_for_session[sname] = flow_id
        for mname in mappings:
            if mname not in {f.split("@")[0] for f in flows}:
                read(mname, mname, {})

        tasks: list[OrchestrationTaskIR] = []
        deps: list[TaskDependencyIR] = []
        for wf in workflows:
            skip = set()
            for ti in wf.xpath("./TASKINSTANCE"):
                tname = ti.attrib.get("NAME", "")
                ttype = (ti.attrib.get("TASKTYPE") or "").lower()
                if ttype == "start":
                    skip.add(tname)
                    continue
                if ttype == "session":
                    ref = ti.attrib.get("TASKNAME", "")
                    tasks.append(
                        OrchestrationTaskIR(
                            id=tname,
                            name=tname,
                            task_type="data_flow",
                            data_flow_id=flow_for_session.get(ref),
                        )
                    )
                    continue
                code, detail = TASK_REVIEW.get(
                    ttype, ("INFA_UNSUPPORTED_TASK", f"Task type '{ttype}' needs review.")
                )
                op = OpaqueSnippetIR(
                    node_id=tname,
                    component_name=tname,
                    component_type=ttype or "task",
                    reason_code=code,
                    reason_detail=detail,
                    original_snippet=etree.tostring(ti, encoding="unicode")[:2000],
                    location=f"{artifact_name}/{wf.attrib.get('NAME', '')}/{tname}",
                )
                opaque.append(op)
                tasks.append(
                    OrchestrationTaskIR(id=tname, name=tname, task_type="opaque", opaque=op)
                )
            for link in wf.xpath("./WORKFLOWLINK"):
                up, down = link.attrib.get("FROMTASK", ""), link.attrib.get("TOTASK", "")
                if not up or not down or up in skip:
                    continue
                cond = (link.attrib.get("CONDITION") or "").strip()
                simple = re.fullmatch(r"\$\w+\.Status\s*=\s*SUCCEEDED", cond, re.IGNORECASE)
                deps.append(
                    TaskDependencyIR(
                        upstream_task_id=up,
                        downstream_task_id=down,
                        condition_type="success" if (not cond or simple) else "expression",
                        condition_expr=None if (not cond or simple) else cond,
                    )
                )

        pkg_name = (
            workflows[0].attrib.get("NAME")
            if workflows
            else next(iter(mappings), artifact_name.removesuffix(".xml"))
        )
        return PackageIR(
            id=pkg_name or artifact_name,
            name=pkg_name or artifact_name,
            source_tool="informatica",
            source_artifact_name=artifact_name,
            data_flows=list(flows.values()),
            tasks=tasks,
            task_dependencies=deps,
            opaque_items=opaque,
        )

    def run_golden_check(self) -> tuple[bool, str]:
        sample = b"""<?xml version="1.0"?>
<POWERMART><REPOSITORY><FOLDER NAME="FIN">
  <SOURCE NAME="ORD" DATABASETYPE="Microsoft SQL Server" OWNERNAME="dbo">
    <SOURCEFIELD NAME="ID" DATATYPE="int"/><SOURCEFIELD NAME="AMT" DATATYPE="decimal" SCALE="2"/>
  </SOURCE>
  <TARGET NAME="TGT_ORD"><TARGETFIELD NAME="ID" DATATYPE="integer"/>
    <TARGETFIELD NAME="NET" DATATYPE="decimal"/></TARGET>
  <MAPPING NAME="m_ord">
    <TRANSFORMATION NAME="SQ_ORD" TYPE="Source Qualifier">
      <TRANSFORMFIELD NAME="ID" DATATYPE="integer" PORTTYPE="INPUT/OUTPUT"/>
      <TRANSFORMFIELD NAME="AMT" DATATYPE="decimal" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="EXP_CALC" TYPE="Expression">
      <TRANSFORMFIELD NAME="ID" DATATYPE="integer" PORTTYPE="INPUT/OUTPUT" EXPRESSION="ID"/>
      <TRANSFORMFIELD NAME="AMT" DATATYPE="decimal" SCALE="2" PORTTYPE="INPUT"/>
      <TRANSFORMFIELD NAME="NET" DATATYPE="decimal" SCALE="2" PORTTYPE="OUTPUT"
        EXPRESSION="IIF(ISNULL(AMT), 0, AMT)"/>
    </TRANSFORMATION>
    <INSTANCE NAME="ORD" TRANSFORMATION_NAME="ORD" TYPE="SOURCE" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_ORD" TRANSFORMATION_NAME="SQ_ORD" TYPE="TRANSFORMATION" TRANSFORMATION_TYPE="Source Qualifier">
      <ASSOCIATED_SOURCE_INSTANCE NAME="ORD"/></INSTANCE>
    <INSTANCE NAME="EXP_CALC" TRANSFORMATION_NAME="EXP_CALC" TYPE="TRANSFORMATION" TRANSFORMATION_TYPE="Expression"/>
    <INSTANCE NAME="TGT_ORD" TRANSFORMATION_NAME="TGT_ORD" TYPE="TARGET" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMINSTANCE="ORD" FROMFIELD="ID" TOINSTANCE="SQ_ORD" TOFIELD="ID"/>
    <CONNECTOR FROMINSTANCE="ORD" FROMFIELD="AMT" TOINSTANCE="SQ_ORD" TOFIELD="AMT"/>
    <CONNECTOR FROMINSTANCE="SQ_ORD" FROMFIELD="ID" TOINSTANCE="EXP_CALC" TOFIELD="ID"/>
    <CONNECTOR FROMINSTANCE="SQ_ORD" FROMFIELD="AMT" TOINSTANCE="EXP_CALC" TOFIELD="AMT"/>
    <CONNECTOR FROMINSTANCE="EXP_CALC" FROMFIELD="ID" TOINSTANCE="TGT_ORD" TOFIELD="ID"/>
    <CONNECTOR FROMINSTANCE="EXP_CALC" FROMFIELD="NET" TOINSTANCE="TGT_ORD" TOFIELD="NET"/>
  </MAPPING>
</FOLDER></REPOSITORY></POWERMART>"""
        pkg = self.parse_artifact("golden_infa.xml", sample)
        df = pkg.data_flows[0] if pkg.data_flows else None
        ok = (
            df is not None
            and len(df.nodes) == 3
            and len(df.edges) == 2
            and not pkg.opaque_items
            and df.nodes[1].expressions[0].neutral_sql
            == "CASE WHEN (AMT IS NULL) THEN 0 ELSE AMT END"
        )
        return ok, "InformaticaParser golden check passed" if ok else "Failed golden check"


__all__ = ["InformaticaParser"]
