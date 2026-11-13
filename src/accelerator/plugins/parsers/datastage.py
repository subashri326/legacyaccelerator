"""IBM DataStage reader (.dsx text export and DSExport XML) producing Neutral IR.

A DataStage job is a set of stages joined by links. Each link carries named columns, and stages
other than the Transformer map their output columns from input columns by derivation
(``lnk_in.col``). This reader keeps that wiring:

* Source vs target is decided by the stage's links (only outputs = source, only inputs =
  target), never by its name.
* A database stage feeding a Lookup's reference link becomes the lookup table.
* Each Transformer output link becomes a filter (its constraint; "otherwise" links get rows no
  other link took) followed by its column derivations. Stage variables are inlined in order; a
  variable that reads its own or a later value (row-to-row state) is flagged.
* Lookup "continue" keeps unmatched rows (NULLs); "fail"/"drop" keep matches only.
* Join, Aggregator, Funnel, Sort, Remove Duplicates, Copy and single-condition Filter are
  converted; Modify, Merge, Change Capture, SCD, generators, file stages, shared containers and
  non-insert database writes are flagged for review.

Formats not seen in a client export yet are treated conservatively: anything not understood
becomes a review item rather than a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from lxml import etree
from sqlglot import exp
from sqlglot.errors import SqlglotError

from accelerator.ir.etl_expressions import Scope, Untranslatable, datastage_kind, translate
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

DB_STAGES = {
    "odbcconnectorpx": None,
    "pxodbc": None,
    "msoledbconnectorpx": "tsql",
    "oracleconnectorpx": "oracle",
    "pxoracle": "oracle",
    "teradataconnectorpx": "teradata",
    "pxteradata": "teradata",
    "db2connectorpx": "db2",
    "pxdb2": "db2",
    "jdbcconnectorpx": None,
}
FILE_STAGES = {"pxsequentialfile", "pxdataset", "pxfileset", "pxlookupfileset", "pxcomplexflatfile"}
REVIEW_STAGES = {
    "pxmodify": ("DS_MODIFY_STAGE", "Modify stage changes types/nulls with its own specification."),
    "pxmerge": ("DS_MERGE_STAGE", "Merge stage applies update records to a master."),
    "pxchangecapture": ("DS_CHANGE_CAPTURE", "Change Capture compares before/after data sets."),
    "pxchangeapply": ("DS_CHANGE_APPLY", "Change Apply applies change records."),
    "pxscd": ("DS_SCD_STAGE", "Slowly Changing Dimension stage maintains history rows."),
    "pxsurrogatekeygenerator": ("DS_SURROGATE_KEY", "Generates surrogate keys."),
    "pxcolumngenerator": ("DS_GENERATOR", "Generates column values."),
    "pxrowgenerator": ("DS_GENERATOR", "Generates rows."),
    "pxpivot": ("DS_PIVOT", "Pivot stage."),
    "pxcheckduplicates": ("DS_UNSUPPORTED_STAGE", "Unsupported stage."),
    "pxexternalfilter": ("DS_EXTERNAL", "Runs an external program."),
}
_FUNCS = {"sum": "SUM", "mean": "AVG", "max": "MAX", "min": "MIN"}


def _norm_ole(t: str) -> str:
    """'CCustomStage' (text export) and 'CustomStage' (XML export) -> 'customstage'."""
    t = t.strip()
    if len(t) > 1 and t[0] == "C" and t[1].isupper():
        t = t[1:]
    return t.lower()


@dataclass
class _Rec:
    id: str
    ole: str
    props: dict[str, str] = field(default_factory=dict)
    collections: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    raw: str = ""

    def get(self, key: str, default: str = "") -> str:
        return self.props.get(key.lower(), default)


@dataclass
class _Job:
    name: str
    records: list[_Rec]


# --------------------------------------------------------------------------------------------
# Reading the two export formats into records
# --------------------------------------------------------------------------------------------
_PROP = re.compile(r'^\s*(\w+)\s+"((?:[^"\\]|\\.)*)"\s*$')
_MULTI = "=+=+=+="


def _unescape(v: str) -> str:
    return re.sub(r'\\(["\\])', r"\1", v)


def _read_text_dsx(text: str) -> list[_Job]:
    jobs: list[_Job] = []
    lines = text.splitlines()
    i = 0
    job: _Job | None = None
    rec: _Rec | None = None
    sub: dict[str, str] | None = None
    sub_depth = 0
    last_coll = ""
    rec_lines: list[str] = []
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        i += 1
        if rec is not None:
            rec_lines.append(line)
        m_begin = re.match(r"^BEGIN\s+(\w+)$", s)
        m_end = re.match(r"^END\s+(\w+)$", s)
        if m_begin:
            kind = m_begin.group(1).upper()
            if kind == "DSJOB":
                job = _Job(name="", records=[])
            elif kind == "DSRECORD" and job is not None:
                rec = _Rec(id="", ole="")
                rec_lines = [line]
                last_coll = ""
            elif kind == "DSSUBRECORD" and rec is not None:
                if sub is None:
                    sub = {}
                sub_depth += 1
            continue
        if m_end:
            kind = m_end.group(1).upper()
            if kind == "DSSUBRECORD" and rec is not None and sub is not None:
                sub_depth -= 1
                if sub_depth == 0:
                    rec.collections.setdefault(last_coll or "_", []).append(sub)
                    sub = None
            elif kind == "DSRECORD" and rec is not None and job is not None:
                rec.raw = "\n".join(rec_lines)[:4000]
                job.records.append(rec)
                rec = None
            elif kind == "DSJOB" and job is not None:
                jobs.append(job)
                job = None
            continue
        key, value = None, None
        m = _PROP.match(line)
        if m:
            key, value = m.group(1), _unescape(m.group(2))
        else:
            m2 = re.match(r"^\s*(\w+)\s+=\+=\+=\+=\s*(.*)$", line)
            if m2:
                key, buf = m2.group(1), [m2.group(2)] if m2.group(2) else []
                while i < len(lines) and _MULTI not in lines[i]:
                    buf.append(lines[i])
                    i += 1
                if i < len(lines):
                    tail = lines[i].split(_MULTI)[0]
                    if tail:
                        buf.append(tail)
                    if rec is not None:
                        rec_lines.append(lines[i])
                    i += 1
                value = "\n".join(buf)
        if key is None:
            continue
        if sub is not None and sub_depth == 1:
            sub.setdefault(key.lower(), value or "")
        elif rec is not None and sub is None:
            if key == "Identifier":
                rec.id = value or ""
            elif key == "OLEType":
                rec.ole = _norm_ole(value or "")
            else:
                rec.props.setdefault(key.lower(), value or "")
                if value and re.fullmatch(r"C[A-Z]\w*", value):
                    last_coll = key.lower()
        elif job is not None and rec is None and key == "Identifier":
            job.name = value or ""
    return jobs


def _read_xml_dsx(data: bytes) -> list[_Job]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(data, parser=parser)
    jobs = []
    for j in root.xpath(".//Job"):
        recs = []
        for r in j.xpath("./Record"):
            rec = _Rec(id=r.attrib.get("Identifier", ""), ole=_norm_ole(r.attrib.get("Type", "")))
            for p in r.xpath("./Property"):
                rec.props.setdefault(p.attrib.get("Name", "").lower(), p.text or "")
            for c in r.xpath("./Collection"):
                rows = []
                for sr in c.xpath("./SubRecord"):
                    rows.append(
                        {
                            p.attrib.get("Name", "").lower(): p.text or ""
                            for p in sr.xpath("./Property")
                        }
                    )
                rec.collections[c.attrib.get("Name", "").lower()] = rows
            rec.raw = etree.tostring(r, encoding="unicode")[:4000]
            recs.append(rec)
        jobs.append(_Job(name=j.attrib.get("Identifier", ""), records=recs))
    return jobs


def _connector_props(rec: _Rec) -> dict[str, str]:
    """Settings of a connector stage (XMLProperties) or native stage (plain properties)."""
    out: dict[str, str] = {}
    for p in rec.collections.get("properties", []):
        name, value = p.get("name", ""), p.get("value", "")
        if name.lower() == "xmlproperties" and value.strip():
            body = re.sub(r"^\s*<\?xml[^>]*\?>", "", value.strip())
            try:
                tree = etree.fromstring(
                    body.encode("utf-8"), parser=etree.XMLParser(resolve_entities=False)
                )
            except etree.XMLSyntaxError:
                continue
            for el in tree.iter():
                if isinstance(el.tag, str) and (el.text or "").strip():
                    out.setdefault(el.tag.lower(), el.text.strip())
        elif name:
            out.setdefault(name.lower(), value)
    return out


def _options(rec: _Rec) -> list[tuple[str, str]]:
    """Ordered (name, value) stage options (Join keys, Aggregator reduce/sum, Sort keys...)."""
    out = []
    for p in rec.collections.get("properties", []):
        name = p.get("name", "").lower()
        if name and name != "xmlproperties":
            out.append((name, p.get("value", "")))
    return out


def _first_token(value: str) -> str:
    """Option values may carry sub-options after DataStage's \\(n) separators."""
    return re.split(r"\\\(\w+\)", value)[0].strip().strip('"')


# --------------------------------------------------------------------------------------------
# Building one job's data flow
# --------------------------------------------------------------------------------------------
@dataclass
class _Link:
    pin: _Rec
    name: str
    from_stage: str
    to_stage: str
    columns: list[dict[str, str]]


class _JobReader:
    def __init__(self, artifact: str, job: _Job) -> None:
        self.artifact = artifact
        self.job = job
        self.by_id = {r.id: r for r in job.records}
        self.stages = {
            r.id: r
            for r in job.records
            if r.ole
            in (
                "customstage",
                "transformerstage",
                "containerstage",
                "sharedcontainerstage",
                "stage",
            )
            or (r.ole.endswith("stage") and "pins" not in r.ole)
        }
        self.links: list[_Link] = []
        for r in job.records:
            if r.ole in ("customoutput", "trxoutput", "output") and r.get("partner"):
                owner = r.id.rsplit("P", 1)[0] if "P" in r.id else ""
                target_stage = r.get("partner").split("|")[0]
                if owner in self.stages and target_stage in self.stages:
                    cols = r.collections.get("columns", [])
                    self.links.append(_Link(r, r.get("name"), owner, target_stage, cols))
        self.nodes: list[NodeIR] = []
        self.edges: list[EdgeIR] = []
        self.opaque: list[OpaqueSnippetIR] = []
        # link name -> (node id producing it, branch)
        self.link_source: dict[str, tuple[str, str]] = {}
        self.consumed_reference: set[str] = set()

    # -- helpers -------------------------------------------------------------------------------
    def name(self, sid: str) -> str:
        return self.stages[sid].get("name") or sid

    def stype(self, sid: str) -> str:
        rec = self.stages[sid]
        if rec.ole == "transformerstage":
            return "ctransformerstage"
        return (rec.get("stagetype") or rec.ole).lower()

    def ins(self, sid: str) -> list[_Link]:
        order = [p for p in self.stages[sid].get("inputpins").split("|") if p]
        links = [lk for lk in self.links if lk.to_stage == sid]
        partner_pin = {lk.name: lk.pin.get("partner").split("|")[-1] for lk in links}
        return sorted(
            links,
            key=lambda lk: (
                order.index(partner_pin[lk.name]) if partner_pin[lk.name] in order else 99
            ),
        )

    def outs(self, sid: str) -> list[_Link]:
        order = [p for p in self.stages[sid].get("outputpins").split("|") if p]
        links = [lk for lk in self.links if lk.from_stage == sid]
        return sorted(links, key=lambda lk: order.index(lk.pin.id) if lk.pin.id in order else 99)

    def review(self, sid: str, code: str, detail: str, node_id: str | None = None) -> NodeIR:
        rec = self.stages[sid]
        nid = node_id or self.name(sid)
        op = OpaqueSnippetIR(
            node_id=nid,
            component_name=self.name(sid),
            component_type=rec.get("stagetype") or rec.ole,
            reason_code=code,
            reason_detail=detail,
            original_snippet=rec.raw,
            location=f"{self.artifact}/{self.job.name}/{self.name(sid)}",
        )
        self.opaque.append(op)
        node = NodeIR(
            id=nid,
            name=self.name(sid),
            operator=NodeOperator.OPAQUE,
            source_component_type=op.component_type,
            opaque=op,
        )
        self.nodes.append(node)
        for lk in self.outs(sid):
            self.link_source.setdefault(lk.name, (nid, ""))
        if node_id is None:
            for lk in self.ins(sid):
                self.connect(lk, nid)
        return node

    @staticmethod
    def col_kind(c: dict[str, str]) -> str:
        return datastage_kind(c.get("sqltype"), c.get("scale"))

    def scope_for(self, links: list[_Link]) -> Scope:
        cols: dict[str, str] = {}
        for lk in links:
            for c in lk.columns:
                cols.setdefault(c.get("name", ""), self.col_kind(c))
        return Scope.of(cols, strip_prefixes={lk.name for lk in links})

    def connect(self, lk: _Link, node_id: str, lineage: list[dict[str, str]] | None = None) -> None:
        """Wire an input link into a node: the node sees exactly the link's columns."""
        src = self.link_source.get(lk.name)
        if src is None:
            return
        cols = (
            lineage
            if lineage is not None
            else [{"from": c.get("name", ""), "to": c.get("name", "")} for c in lk.columns]
        )
        self.edges.append(
            EdgeIR(
                id=f"{lk.name}_{node_id}",
                source_node_id=src[0],
                target_node_id=node_id,
                branch_name=src[1] or "default",
                column_lineage=cols,
                projection="strict",
            )
        )

    def derived_lineage(self, out: _Link, inputs: list[_Link]) -> list[dict[str, str]] | None:
        """Output columns of a non-Transformer stage are plain copies of input columns."""
        prefixes = {lk.name.lower() for lk in inputs}
        lineage = []
        for c in out.columns:
            d = (c.get("derivation") or "").strip()
            src = c.get("name", "")
            if d:
                m = re.fullmatch(r"(\w+)\.(\w+)", d)
                if not m or m.group(1).lower() not in prefixes:
                    return None
                src = m.group(2)
            lineage.append({"from": src, "to": c.get("name", "")})
        return lineage

    # -- stages --------------------------------------------------------------------------------
    def db_stage(self, sid: str) -> None:
        props = _connector_props(self.stages[sid])
        stype = self.stype(sid)
        dialect = DB_STAGES.get(stype)
        ins, outs = self.ins(sid), self.outs(sid)
        name = self.name(sid)
        table = props.get("tablename") or props.get("table") or ""
        if outs and not ins:  # a source (or a lookup reference)
            if any(
                self.stype(lk.to_stage) == "pxlookup" and self.ins(lk.to_stage)[0].name != lk.name
                for lk in outs
            ):
                self.consumed_reference.add(sid)
                return  # folded into the Lookup
            if len(outs) != 1:
                self.review(sid, "DS_MULTI_OUTPUT_SOURCE", "Source stage with several outputs.")
                return
            out = outs[0]
            cols = [c.get("name", "") for c in out.columns]
            sql = (props.get("selectstatement") or props.get("query") or "").strip()
            generate = (props.get("generatesql") or "1").strip() not in ("0", "false", "no")
            if re.search(r"#\w[\w.]*#", sql + table):
                self.review(sid, "DS_PARAMETER_IN_SQL", "Source SQL uses job parameters (#...#).")
                return
            if sql and (not generate or not table):
                read = DIALECT_MAP.get(dialect or "tsql", "tsql")
                try:
                    tree = sqlglot.parse_one(sql, read=read)
                except (SqlglotError, ValueError):
                    tree = None
                ok = isinstance(tree, exp.Select) and len(tree.expressions) == len(cols)
                if ok:
                    names = [e.alias_or_name.lower() for e in tree.expressions]  # type: ignore[union-attr]
                    ok = names == [c.lower() for c in cols]
                if not ok:
                    self.review(
                        sid,
                        "DS_SQL_COLUMNS",
                        "The user-defined SQL's columns do not "
                        "match the link's columns by name and order.",
                    )
                    return
            elif table:
                sql = f"SELECT {', '.join(cols)} FROM {table}"
                read = DIALECT_MAP.get(dialect or "tsql", "tsql")
            else:
                self.review(sid, "DS_SOURCE_UNKNOWN", "No table or SQL found for this stage.")
                return
            tables = [
                t.sql(dialect=read) for t in sqlglot.parse_one(sql, read=read).find_all(exp.Table)
            ]
            self.nodes.append(
                NodeIR(
                    id=name,
                    name=name,
                    operator=NodeOperator.SOURCE,
                    source_component_type=stype,
                    sql_query=sql,
                    output_columns=[
                        ColumnIR(name=c.get("name", ""), data_type=c.get("sqltype", ""))
                        for c in out.columns
                    ],
                    metadata={
                        "sql_dialect": dialect or "",
                        "query_columns_aligned": True,
                        "source_tables": tables,
                    },
                )
            )
            self.link_source[out.name] = (name, "")
            return
        if ins and not outs:  # a target
            mode = (props.get("writemode") or "0").strip().lower()
            if mode not in ("0", "insert", "append"):
                self.review(
                    sid,
                    "DS_TARGET_WRITE_MODE",
                    f"Writes with mode '{mode}' (update/"
                    "upsert/delete); the load must be designed and approved.",
                )
                self.connect(ins[0], name)
                return
            if not table:
                self.review(sid, "DS_TARGET_UNKNOWN", "No target table found for this stage.")
                return
            lk = ins[0]
            self.nodes.append(
                NodeIR(
                    id=name,
                    name=name,
                    operator=NodeOperator.TARGET,
                    source_component_type=stype,
                    table_ref=table,
                    metadata={
                        "column_mapping": [
                            {"source": c.get("name", ""), "target": c.get("name", "")}
                            for c in lk.columns
                        ],
                        "truncate_before_load": (props.get("tableaction") or "")
                        in ("3", "truncate"),
                    },
                )
            )
            self.connect(lk, name)
            return
        self.review(sid, "DS_DB_STAGE_SHAPE", "Database stage with both inputs and outputs.")

    def transformer(self, sid: str) -> None:
        rec = self.stages[sid]
        name = self.name(sid)
        ins = self.ins(sid)
        if len(ins) != 1:
            self.review(sid, "DS_TRANSFORMER_INPUTS", "Transformer with several input links.")
            return
        if rec.get("loopcondition") or rec.collections.get("loopvars"):
            self.review(
                sid, "DS_TRANSFORMER_LOOP", "Transformer loops create several rows per input row."
            )
            return
        inp = ins[0]
        scope = self.scope_for([inp])
        svars = rec.collections.get("stagevars", [])
        scope.pending_variables = {v.get("name", "") for v in svars}
        for v in svars:
            vname = v.get("name", "")
            scope.pending_variables.discard(vname)
            vscope = Scope(
                columns=scope.columns,
                variables=dict(scope.variables),
                pending_variables=scope.pending_variables | {vname},
                strip_prefixes=scope.strip_prefixes,
            )
            text = v.get("expression") or v.get("derivation") or ""
            try:
                scope.variables[vname] = translate(text, "datastage", vscope)
            except Untranslatable as exc:
                scope.variables[vname] = exc
        # Every output link is filtered from the same input rows.
        hub = f"{name}__in"
        self.nodes.append(
            NodeIR(
                id=hub,
                name=hub,
                operator=NodeOperator.SORT,
                source_component_type="CTransformerStage",
            )
        )
        self.connect(inp, hub)
        conditions: list[str] = []
        outs = self.outs(sid)
        otherwise = [o for o in outs if o.pin.get("reject") == "1"]
        unconstrained = [
            o for o in outs if o not in otherwise and not (o.pin.get("constraint") or "").strip()
        ]
        for out in [o for o in outs if o not in otherwise] + otherwise:
            upstream = hub
            constraint = (out.pin.get("constraint") or "").strip()
            cond_ir: ExpressionIR | None = None
            if out in otherwise and unconstrained:
                cond_ir = ExpressionIR(
                    id=f"{name}:{out.name}:otherwise",
                    original_expr="otherwise",
                    source_dialect="datastage",
                    is_opaque=True,
                    opaque_reason=f"Otherwise link alongside links with no constraint "
                    f"({[o.name for o in unconstrained]}); which rows it receives depends on "
                    "link order and must be confirmed.",
                )
            elif out in otherwise:
                neutral = " AND ".join(f"(CASE WHEN {c} THEN 1 ELSE 0 END) = 0" for c in conditions)
                cond_ir = ExpressionIR(
                    id=f"{name}:{out.name}:otherwise",
                    original_expr="otherwise",
                    source_dialect="datastage",
                    neutral_sql=neutral or "1 = 0",
                )
            elif constraint:
                try:
                    sql, _ = translate(constraint, "datastage", scope, context="condition")
                    cond_ir = ExpressionIR(
                        id=f"{name}:{out.name}:constraint",
                        original_expr=constraint,
                        source_dialect="datastage",
                        neutral_sql=sql,
                    )
                    conditions.append(sql)
                except Untranslatable as exc:
                    cond_ir = ExpressionIR(
                        id=f"{name}:{out.name}:constraint",
                        original_expr=constraint,
                        source_dialect="datastage",
                        is_opaque=True,
                        opaque_reason=str(exc),
                    )
                    conditions.append("1 = 1")  # otherwise links can't be computed exactly
            if cond_ir is not None:
                fid = f"{name}__{out.name}__filter"
                self.nodes.append(
                    NodeIR(
                        id=fid,
                        name=f"{name}.{out.name} constraint",
                        operator=NodeOperator.FILTER,
                        condition=cond_ir,
                        source_component_type="CTransformerStage",
                    )
                )
                self.edges.append(EdgeIR(id=f"{hub}_{fid}", source_node_id=hub, target_node_id=fid))
                upstream = fid
            exprs = []
            for c in out.columns:
                cname, deriv = c.get("name", ""), (c.get("derivation") or "").strip()
                try:
                    sql, _ = translate(deriv or "SetNull()", "datastage", scope)
                    exprs.append(
                        ExpressionIR(
                            id=f"{name}:{out.name}:{cname}",
                            target_column=cname,
                            original_expr=deriv,
                            source_dialect="datastage",
                            neutral_sql=sql,
                        )
                    )
                except Untranslatable as exc:
                    exprs.append(
                        ExpressionIR(
                            id=f"{name}:{out.name}:{cname}",
                            target_column=cname,
                            original_expr=deriv,
                            source_dialect="datastage",
                            is_opaque=True,
                            opaque_reason=str(exc),
                        )
                    )
            xid = f"{name}__{out.name}"
            self.nodes.append(
                NodeIR(
                    id=xid,
                    name=f"{name}.{out.name}",
                    operator=NodeOperator.EXPRESSION,
                    expressions=exprs,
                    source_component_type="CTransformerStage",
                )
            )
            self.edges.append(
                EdgeIR(id=f"{upstream}_{xid}", source_node_id=upstream, target_node_id=xid)
            )
            self.link_source[out.name] = (xid, "")

    def lookup(self, sid: str) -> None:
        name = self.name(sid)
        ins, outs = self.ins(sid), self.outs(sid)
        if len(ins) != 2 or len(outs) != 1:
            self.review(
                sid, "DS_LOOKUP_SHAPE", "Lookup with other than one reference link and one output."
            )
            return
        primary, ref = ins
        ref_stage = ref.from_stage
        props = _connector_props(self.stages[ref_stage]) if ref_stage in self.stages else {}
        table = props.get("tablename") or props.get("table") or ""
        ref_sql = (props.get("selectstatement") or "").strip()
        generate = (props.get("generatesql") or "1").strip() not in ("0", "false", "no")
        ref_dialect = DB_STAGES.get(self.stype(ref_stage)) if ref_stage in self.stages else None
        ref_filter = ""
        if ref_sql and (not generate or not table):
            # A reference query is accepted when it is a plain filtered read of one table.
            read = DIALECT_MAP.get(ref_dialect or "tsql", "tsql")
            try:
                tree = sqlglot.parse_one(ref_sql, read=read)
            except (SqlglotError, ValueError):
                tree = None
            ref_cols = {c.get("name", "").lower() for c in ref.columns}
            frm = (tree.args.get("from") or tree.args.get("from_")) if tree is not None else None
            simple = (
                isinstance(tree, exp.Select)
                and not any(
                    tree.args.get(k)
                    for k in ("joins", "group", "having", "distinct", "limit", "with")
                )
                and isinstance(frm, exp.From)
                and isinstance(frm.this, exp.Table)
                and all(isinstance(e, exp.Column) for e in tree.expressions)
                and {e.name.lower() for e in tree.expressions} == ref_cols
            )
            if not simple:
                table = ""
            else:
                table = frm.this.sql(dialect=read)  # type: ignore[union-attr]
                where = tree.args.get("where")  # type: ignore[union-attr]
                ref_filter = where.this.sql(dialect=read) if where is not None else ""
        if ref_stage not in self.consumed_reference or not table or re.search(r"#\w", ref_sql):
            self.review(
                sid,
                "DS_LOOKUP_REFERENCE",
                "The reference link is not a plain read of "
                "one database table (file, complex SQL or another stage).",
            )
            return
        prim_cols = {c.get("name", "").lower(): c.get("name", "") for c in primary.columns}
        keys = []
        for c in ref.columns:
            key_expr = (c.get("keyexpression") or c.get("derivation") or "").strip()
            is_key = c.get("keyposition", "0") not in ("", "0") or key_expr
            if not is_key:
                continue
            m = re.fullmatch(r"(?:(\w+)\.)?(\w+)", key_expr) if key_expr else None
            left = (
                m.group(2)
                if m and (m.group(1) or "").lower() in ("", primary.name.lower())
                else (c.get("name", "") if not key_expr else "")
            )
            if left.lower() not in prim_cols:
                self.review(
                    sid,
                    "DS_LOOKUP_KEYS",
                    f"Key {c.get('name')} is not matched to a primary column.",
                )
                return
            keys.append({"left": prim_cols[left.lower()], "right": c.get("name", "")})
        # The failure action can sit on the stage or on the reference input pin.
        in_pin = self.by_id.get(ref.pin.get("partner").split("|")[-1])
        opts = {k: _first_token(v).lower() for k, v in _options(self.stages[sid])}
        if in_pin is not None:
            opts |= {k: v.lower() for k, v in in_pin.props.items()}
            opts |= {k: _first_token(v).lower() for k, v in _options(in_pin)}
        fail = next(
            (
                opts[k]
                for k in ("lookupfail", "ifnotfound", "lookupfailure", "conditionnotmet")
                if k in opts
            ),
            "fail",
        )
        if fail not in ("continue", "drop", "fail"):
            self.review(sid, "DS_LOOKUP_FAILURE_MODE", f"Lookup failure action '{fail}'.")
            return
        out = outs[0]
        copies, lineage = [], []
        for c in out.columns:
            d = (c.get("derivation") or "").strip()
            m = re.fullmatch(r"(\w+)\.(\w+)", d)
            if not m:
                self.review(
                    sid,
                    "DS_LOOKUP_OUTPUT",
                    f"Output column {c.get('name')} is not a plain input column.",
                )
                return
            link, col = m.group(1).lower(), m.group(2)
            if link == ref.name.lower():
                alias = f"{ref.name}__{col}"
                copies.append({"reference": col, "alias": alias})
                lineage.append({"from": alias, "to": c.get("name", "")})
            else:
                lineage.append({"from": col, "to": c.get("name", "")})
        node = NodeIR(
            id=name,
            name=name,
            operator=NodeOperator.LOOKUP,
            source_component_type="PxLookup",
            table_ref=table,
            join_keys=keys,
            metadata={
                "copy_columns": copies,
                "on_no_match": "keep" if fail == "continue" else "",
                "no_match_output": "dropped" if fail == "drop" else "",
                "reference_filter": ref_filter,
                "sql_dialect": ref_dialect or "",
            },
        )
        self.nodes.append(node)
        self.connect(primary, name)
        self._emit_output(sid, name, out, lineage)

    def _emit_output(
        self, sid: str, node_id: str, out: _Link, lineage: list[dict[str, str]]
    ) -> None:
        """A projection step giving the output link its columns."""
        pid = f"{node_id}__{out.name}"
        self.nodes.append(
            NodeIR(
                id=pid,
                name=f"{self.name(sid)}.{out.name}",
                operator=NodeOperator.SORT,
                source_component_type="link",
            )
        )
        self.edges.append(
            EdgeIR(
                id=f"{node_id}_{pid}",
                source_node_id=node_id,
                target_node_id=pid,
                column_lineage=lineage,
                projection="strict",
            )
        )
        self.link_source[out.name] = (pid, "")

    def join(self, sid: str) -> None:
        name = self.name(sid)
        ins, outs = self.ins(sid), self.outs(sid)
        opts = _options(self.stages[sid])
        keys = [_first_token(v) for k, v in opts if k == "key"]
        op = next((_first_token(v).lower() for k, v in opts if k == "operator"), "innerjoin")
        if len(ins) != 2 or len(outs) != 1 or not keys:
            self.review(sid, "DS_JOIN_SHAPE", "Join with other than two inputs, or no keys.")
            return
        if op == "fullouterjoin":
            self.review(
                sid,
                "DS_FULL_OUTER_JOIN",
                "Full outer join produces separate left/right key columns that must be mapped.",
            )
            return
        jt = {"innerjoin": "INNER", "leftouterjoin": "LEFT", "rightouterjoin": "LEFT"}.get(op)
        if jt is None:
            self.review(sid, "DS_JOIN_TYPE", f"Join type '{op}'.")
            return
        left, right = (ins[1], ins[0]) if op == "rightouterjoin" else (ins[0], ins[1])
        lineage = self.derived_lineage(outs[0], ins)
        if lineage is None:
            self.review(sid, "DS_JOIN_OUTPUT", "Output columns are not plain input columns.")
            return
        self.nodes.append(
            NodeIR(
                id=name,
                name=name,
                operator=NodeOperator.JOIN,
                source_component_type="PxJoin",
                join_type=jt,
                join_keys=[{"left": k, "right": k} for k in keys],
            )
        )
        self.connect(left, name)
        self.connect(right, name)
        self._emit_output(sid, name, outs[0], lineage)

    def aggregator(self, sid: str) -> None:
        name = self.name(sid)
        ins, outs = self.ins(sid), self.outs(sid)
        if len(ins) != 1 or len(outs) != 1:
            self.review(sid, "DS_AGGREGATOR_SHAPE", "Aggregator with several inputs or outputs.")
            return
        groups, aggs, current = [], [], ""
        for k, v in _options(self.stages[sid]):
            val = _first_token(v)
            if k == "key":
                groups.append(val)
            elif k == "reduce":
                current = val
            elif k in _FUNCS and current:
                aggs.append({"alias": val, "func": _FUNCS[k], "column": current})
            elif k == "countfield":
                aggs.append({"alias": val, "func": "COUNT", "column": ""})
            elif k in ("method", "selection", "nul_res", "collation_sequence", "ignorenull"):
                continue
            else:
                self.review(
                    sid, "DS_AGGREGATOR_OPTION", f"Aggregator option '{k}' is not converted."
                )
                return
        lineage = self.derived_lineage(outs[0], ins)
        if not aggs or lineage is None:
            self.review(sid, "DS_AGGREGATOR_SPEC", "No aggregations found.")
            return
        self.nodes.append(
            NodeIR(
                id=name,
                name=name,
                operator=NodeOperator.AGGREGATE,
                source_component_type="PxAggregator",
                group_by_columns=groups,
                metadata={"aggregations": aggs},
            )
        )
        self.connect(ins[0], name)
        self._emit_output(sid, name, outs[0], lineage)

    def simple(self, sid: str, operator: NodeOperator, **kw: object) -> None:
        """Funnel / Sort / Remove Duplicates / Copy: inputs by column name, outputs by mapping."""
        name = self.name(sid)
        ins, outs = self.ins(sid), self.outs(sid)
        if not ins or not outs or (operator != NodeOperator.SORT and len(outs) != 1):
            self.review(sid, "DS_STAGE_SHAPE", "Unexpected links for this stage.")
            return
        self.nodes.append(
            NodeIR(
                id=name, name=name, operator=operator, source_component_type=self.stype(sid), **kw
            )
        )  # type: ignore[arg-type]
        out_names = [c.get("name", "") for c in outs[0].columns]
        for lk in ins:
            if operator == NodeOperator.UNION:
                self.connect(lk, name, [{"from": n, "to": n} for n in out_names])
            else:
                self.connect(lk, name)
        for out in outs:
            lineage = self.derived_lineage(out, ins)
            if lineage is None:
                self.review(
                    sid,
                    "DS_STAGE_OUTPUT",
                    "Output columns are not plain input columns.",
                    node_id=f"{name}__{out.name}",
                )
                continue
            self._emit_output(sid, name, out, lineage)

    def sort_keys(self, sid: str) -> list[dict[str, str]]:
        keys = []
        for k, v in _options(self.stages[sid]):
            if k == "key":
                keys.append(
                    {
                        "column": _first_token(v),
                        "direction": "DESC" if re.search(r"desc", v, re.IGNORECASE) else "ASC",
                    }
                )
        return keys

    def filter_stage(self, sid: str) -> None:
        wheres = [v for k, v in _options(self.stages[sid]) if k == "where"]
        if len(wheres) != 1 or len(self.outs(sid)) != 1:
            self.review(sid, "DS_FILTER_SHAPE", "Filter with several conditions or outputs.")
            return
        allowed = (
            exp.Column,
            exp.Literal,
            exp.EQ,
            exp.NEQ,
            exp.GT,
            exp.GTE,
            exp.LT,
            exp.LTE,
            exp.And,
            exp.Or,
            exp.Not,
            exp.Is,
            exp.Null,
            exp.Paren,
            exp.Identifier,
            exp.Boolean,
            exp.In,
            exp.Between,
            exp.Neg,
        )
        try:
            tree = sqlglot.parse_one(_first_token(wheres[0]), read="postgres")
            if not all(isinstance(n, allowed) for n in tree.walk()):
                raise ValueError("unsupported construct")
        except (SqlglotError, ValueError):
            self.review(sid, "DS_FILTER_CONDITION", f"Filter condition: {wheres[0]}")
            return
        name = self.name(sid)
        self.simple(
            sid,
            NodeOperator.FILTER,
            condition=ExpressionIR(
                id=f"{name}:where",
                original_expr=wheres[0],
                source_dialect="datastage",
                neutral_sql=tree.sql(dialect="postgres"),
            ),
        )

    # -- assembly ------------------------------------------------------------------------------
    def order(self) -> list[str]:
        ups = {s: {lk.from_stage for lk in self.links if lk.to_stage == s} for s in self.stages}
        done: list[str] = []

        def visit(s: str, path: frozenset[str] = frozenset()) -> None:
            if s in done or s in path:
                return
            for u in ups[s]:
                visit(u, path | {s})
            done.append(s)

        for s in self.stages:
            visit(s)
        return done

    def read(self) -> DataFlowGraphIR:
        for sid in self.order():
            st = self.stype(sid)
            rec = self.stages[sid]
            if st in DB_STAGES:
                self.db_stage(sid)
            elif st == "ctransformerstage":
                self.transformer(sid)
            elif st == "pxlookup":
                self.lookup(sid)
            elif st == "pxjoin":
                self.join(sid)
            elif st == "pxaggregator":
                self.aggregator(sid)
            elif st == "pxfunnel":
                self.simple(sid, NodeOperator.UNION)
            elif st == "pxcopy":
                self.simple(sid, NodeOperator.SORT)
            elif st == "pxfilter":
                self.filter_stage(sid)
            elif st == "pxsort":
                keys = self.sort_keys(sid)
                flags = {k: _first_token(v).lower() for k, v in _options(rec)}
                unique = flags.get("unique") in ("true", "1") or flags.get("allowdups") in (
                    "false",
                    "0",
                )
                if unique:
                    self.simple(
                        sid,
                        NodeOperator.DEDUPLICATE,
                        dedup_keys=[k["column"] for k in keys],
                        sort_keys=keys,
                    )
                else:
                    self.simple(sid, NodeOperator.SORT, sort_keys=keys)
            elif st == "pxremdup":
                keys = [_first_token(v) for k, v in _options(rec) if k == "key"]
                retain = next(
                    (_first_token(v).lower() for k, v in _options(rec) if k == "retain"), "first"
                )
                upstream = [lk.from_stage for lk in self.ins(sid)]
                sort = (
                    self.sort_keys(upstream[0])
                    if upstream and self.stype(upstream[0]) == "pxsort"
                    else []
                )
                order = [k for k in sort if k["column"].lower() not in {x.lower() for x in keys}]
                out_cols = {c.get("name", "").lower() for lk in self.outs(sid) for c in lk.columns}
                if out_cols - {k.lower() for k in keys} and not order:
                    self.review(
                        sid,
                        "DS_REMDUP_ORDER",
                        "Which duplicate is kept depends on the "
                        "row order, which is not defined by a sort on non-key columns.",
                    )
                    continue
                if retain == "last":
                    order = [
                        {**k, "direction": "ASC" if k["direction"] == "DESC" else "DESC"}
                        for k in order
                    ]
                self.simple(
                    sid, NodeOperator.DEDUPLICATE, dedup_keys=keys, metadata={"order_by": order}
                )
            elif st in FILE_STAGES:
                self.review(
                    sid,
                    "DS_FILE_STAGE",
                    "Reads or writes a file or data set; land it in "
                    "a table (or replace the hand-off) first.",
                )
            elif rec.ole in ("containerstage", "sharedcontainerstage"):
                self.review(sid, "DS_CONTAINER", "Container logic is defined elsewhere.")
            else:
                code, detail = REVIEW_STAGES.get(
                    st, ("DS_UNSUPPORTED_STAGE", f"Stage type '{st}' needs review.")
                )
                self.review(sid, code, detail)
        return DataFlowGraphIR(
            id=self.job.name, name=self.job.name, nodes=self.nodes, edges=self.edges
        )


def _sequence(
    artifact: str, job: _Job
) -> tuple[list[OrchestrationTaskIR], list[TaskDependencyIR], list[OpaqueSnippetIR]]:
    tasks, deps, opaque = [], [], []
    acts = {r.id: r for r in job.records if r.ole.startswith("js") and r.ole.endswith("activity")}
    for r in acts.values():
        nm = r.get("name") or r.id
        if r.ole == "jsjobactivity":
            tasks.append(
                OrchestrationTaskIR(
                    id=nm, name=nm, task_type="data_flow", data_flow_id=r.get("jobname") or None
                )
            )
            continue
        op = OpaqueSnippetIR(
            node_id=nm,
            component_name=nm,
            component_type=r.ole,
            reason_code="DS_SEQUENCE_ACTIVITY",
            reason_detail=f"Sequence activity '{r.ole}' needs review.",
            original_snippet=r.raw,
            location=f"{artifact}/{job.name}/{nm}",
        )
        opaque.append(op)
        tasks.append(OrchestrationTaskIR(id=nm, name=nm, task_type="opaque", opaque=op))
    for r in job.records:
        if r.ole == "jsactivityoutput" and r.get("partner"):
            up = r.id.rsplit("P", 1)[0]
            down = r.get("partner").split("|")[0]
            if up in acts and down in acts:
                kind = (r.get("conditiontype") or "").strip()
                deps.append(
                    TaskDependencyIR(
                        upstream_task_id=acts[up].get("name") or up,
                        downstream_task_id=acts[down].get("name") or down,
                        condition_type="success" if kind in ("", "0", "1", "2") else "expression",
                        condition_expr=r.get("expression") or None,
                    )
                )
    return tasks, deps, opaque


class DataStageParser(Parser):
    """Parse IBM DataStage job exports (.dsx text or DSExport XML) into Neutral IR."""

    manifest = PluginManifest(
        plugin_type="Parser",
        name="datastage_parser",
        version="1.0.0",
        supported_versions=["DataStage 8.5", "DataStage 9.1", "DataStage 11.5", "DataStage 11.7"],
        capabilities={
            "parallel_jobs": True,
            "transformers": True,
            "sequences": True,
            "xml_export": True,
        },
        description="Parses DataStage .dsx/.xml job exports into Neutral IR.",
    )

    def parse_artifact(self, artifact_name: str, content_bytes: bytes) -> PackageIR:
        head = content_bytes[:400].lstrip()
        if head.startswith(b"<"):
            jobs = _read_xml_dsx(content_bytes)
        else:
            jobs = _read_text_dsx(content_bytes.decode("utf-8", errors="replace"))
        flows, tasks, deps, opaque = [], [], [], []
        for job in jobs:
            root = next((r for r in job.records if r.ole == "jobdefn"), None)
            job_type = root.get("jobtype") if root else ""
            if job_type == "2" or any(r.ole == "jsjobactivity" for r in job.records):
                t, d, o = _sequence(artifact_name, job)
                tasks += t
                deps += d
                opaque += o
                continue
            reader = _JobReader(artifact_name, job)
            flows.append(reader.read())
            opaque += reader.opaque
        # Every parallel job runs as a task; jobs a sequence already runs are not repeated.
        scheduled = {t.data_flow_id for t in tasks if t.data_flow_id}
        tasks += [
            OrchestrationTaskIR(id=f.id, name=f.id, task_type="data_flow", data_flow_id=f.id)
            for f in flows
            if f.id not in scheduled
        ]
        name = jobs[0].name if len(jobs) == 1 else artifact_name.rsplit(".", 1)[0]
        return PackageIR(
            id=name or artifact_name,
            name=name or artifact_name,
            source_tool="datastage",
            source_artifact_name=artifact_name,
            data_flows=flows,
            tasks=tasks,
            task_dependencies=deps,
            opaque_items=opaque,
        )

    def run_golden_check(self) -> tuple[bool, str]:
        sample = b"""BEGIN DSJOB
   Identifier "px_golden"
   BEGIN DSRECORD
      Identifier "ROOT"
      OLEType "CJobDefn"
      Name "px_golden"
      JobType "3"
   END DSRECORD
   BEGIN DSRECORD
      Identifier "V0S0"
      OLEType "CCustomStage"
      Name "Src"
      StageType "ODBCConnectorPX"
      OutputPins "V0S0P1"
      Properties "CCustomProperty"
      BEGIN DSSUBRECORD
         Name "XMLProperties"
         Value =+=+=+=
<?xml version='1.0' encoding='UTF-16'?><Properties version='1.1'><Usage><GenerateSQL type='bool'><![CDATA[1]]></GenerateSQL><TableName type='string'><![CDATA[dbo.products]]></TableName></Usage></Properties>
=+=+=+=
      END DSSUBRECORD
   END DSRECORD
   BEGIN DSRECORD
      Identifier "V0S0P1"
      OLEType "CCustomOutput"
      Name "lnk_src"
      Partner "V0S1|V0S1P1"
      Columns "COutputColumn"
      BEGIN DSSUBRECORD
         Name "product_name"
         SqlType "12"
      END DSSUBRECORD
   END DSRECORD
   BEGIN DSRECORD
      Identifier "V0S1"
      OLEType "CTransformerStage"
      Name "Xfm"
      InputPins "V0S1P1"
      OutputPins "V0S1P2"
   END DSRECORD
   BEGIN DSRECORD
      Identifier "V0S1P1"
      OLEType "CTrxInput"
      Name "lnk_src"
      Partner "V0S0|V0S0P1"
   END DSRECORD
   BEGIN DSRECORD
      Identifier "V0S1P2"
      OLEType "CTrxOutput"
      Name "lnk_out"
      Partner "V0S2|V0S2P1"
      Columns "COutputColumn"
      BEGIN DSSUBRECORD
         Name "product_name"
         SqlType "12"
         Derivation "NullToValue(lnk_src.product_name, 'UNKNOWN')"
      END DSSUBRECORD
   END DSRECORD
   BEGIN DSRECORD
      Identifier "V0S2"
      OLEType "CCustomStage"
      Name "Tgt"
      StageType "ODBCConnectorPX"
      InputPins "V0S2P1"
      Properties "CCustomProperty"
      BEGIN DSSUBRECORD
         Name "XMLProperties"
         Value =+=+=+=
<?xml version='1.0' encoding='UTF-16'?><Properties version='1.1'><Usage><WriteMode type='int'><![CDATA[0]]></WriteMode><TableName type='string'><![CDATA[dbo.dim_products]]></TableName></Usage></Properties>
=+=+=+=
      END DSSUBRECORD
   END DSRECORD
   BEGIN DSRECORD
      Identifier "V0S2P1"
      OLEType "CCustomInput"
      Name "lnk_out"
      Partner "V0S1|V0S1P2"
      Columns "COutputColumn"
      BEGIN DSSUBRECORD
         Name "product_name"
         SqlType "12"
      END DSSUBRECORD
   END DSRECORD
END DSJOB"""
        pkg = self.parse_artifact("golden.dsx", sample)
        df = pkg.data_flows[0] if pkg.data_flows else None
        ops = [n.operator for n in df.nodes] if df else []
        ok = (
            df is not None
            and NodeOperator.SOURCE in ops
            and NodeOperator.TARGET in ops
            and not pkg.opaque_items
            and any(
                e.neutral_sql == "COALESCE(product_name, 'UNKNOWN')"
                for n in df.nodes
                for e in n.expressions
            )
        )
        return ok, "DataStageParser golden check passed" if ok else "Failed golden check"
