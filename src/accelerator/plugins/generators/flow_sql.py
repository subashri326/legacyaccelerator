"""Render neutral IR into target SQL, one explicit step at a time.

The one rule here: never invent logic. When the neutral model lacks something the SQL needs
(join columns, an aggregation spec, a column mapping, a known table), raise a residue item for a
person to review instead of guessing. A guess that compiles but returns the wrong rows is worse
than an open review item.

Columns are tracked through every step wherever they are known, so projections are explicit
(no `SELECT *` duplicates when a derived column or lookup adds fields).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from accelerator.config.client_config import ClientConfig
from accelerator.ir.expressions import DIALECT_MAP, ExpressionConverter, ExpressionNotPortable
from accelerator.ir.models import DataFlowGraphIR, NodeIR, NodeOperator, PackageIR, TableIR
from accelerator.plugins.interfaces import GeneratedResidue

if TYPE_CHECKING:
    from accelerator.plugins.generators.base import BaseTargetGenerator

DEFAULT_BRANCH = "__default__"
NO_MATCH_BRANCH = "__no_match__"
_PLACEHOLDER = "L2CREF{}X"


def safe_name(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_").lower() or "unnamed"


@dataclass
class Rel:
    """A relation (CTE) in the generated query and the columns it outputs, if known."""

    name: str
    columns: list[str] | None


@dataclass
class GeneratedModel:
    name: str
    target_ref: str | None
    sql: str  # a single SELECT statement (may start with WITH)


@dataclass
class GeneratedStep:
    name: str
    sql: str  # a non-SELECT statement to run in order (e.g. TRUNCATE, UPDATE)


@dataclass
class RenderResult:
    models: list[GeneratedModel] = field(default_factory=list)
    steps: list[GeneratedStep] = field(default_factory=list)
    residues: list[GeneratedResidue] = field(default_factory=list)


class TableResolver:
    """Finds catalog tables from the references used in legacy code (dbo.orders, [dbo].[orders], orders)."""

    def __init__(self, tables: dict[str, TableIR]) -> None:
        self._by_key: dict[str, TableIR] = {}
        short: dict[str, list[TableIR]] = {}
        for t in tables.values():
            self._by_key[t.fqn.lower()] = t
            self._by_key[f"{t.schema_name}.{t.name}".lower()] = t
            short.setdefault(t.name.lower(), []).append(t)
        for name, matches in short.items():
            if len(matches) == 1:  # bare names only when unambiguous
                self._by_key.setdefault(name, matches[0])

    def find(self, ref: str | None) -> TableIR | None:
        if not ref:
            return None
        clean = ref.replace("[", "").replace("]", "").replace('"', "").strip().lower()
        return self._by_key.get(clean)


class SqlRenderer:
    """Shared target-naming and SQL-rewriting helpers for one generator + client config."""

    def __init__(
        self, gen: BaseTargetGenerator, config: ClientConfig, tables: dict[str, TableIR]
    ) -> None:
        self.gen = gen
        self.cfg = config
        self.resolver = TableResolver(tables)
        self.dialect = DIALECT_MAP.get(gen.platform, gen.platform)
        # Dialect of SQL written for the legacy database (source queries, filters).
        self.source_dialect = {
            "sqlserver": "tsql",
            "oracle": "oracle",
            "teradata": "teradata",
            "postgres": "postgres",
        }.get(config.source.database, "tsql")

    def col(self, name: str) -> str:
        return self.gen.resolve_column_name(name, self.cfg)

    def relation(self, table: TableIR) -> str:
        """How generated code refers to a migrated table on the target."""
        schema = self.gen.resolve_schema(table, self.cfg)
        name = self.gen.resolve_table_name(table.name, self.cfg)
        if self.cfg.target.output == "dbt":
            return f"{{{{ source('{schema.lower()}', '{name.lower()}') }}}}"
        return f"{schema}.{name}"

    def table_columns(self, table: TableIR) -> list[str]:
        return [self.col(c.name) for c in sorted(table.columns, key=lambda c: c.ordinal_position)]

    def expr(self, neutral_sql: str) -> str:
        """Render a neutral expression; raises ExpressionNotPortable if the target can't match it."""
        return ExpressionConverter.render_target(neutral_sql, self.gen.platform, self.col)

    def condition_sql(self, text: str, read: str) -> str:
        """Render a SQL condition written in the source database's dialect (e.g. a source filter).

        Table qualifiers are dropped: the condition is applied to a single relation.
        """
        tree = sqlglot.parse_one(text, read=DIALECT_MAP.get(read, read))
        for col in tree.find_all(exp.Column):
            col.set("table", None)
            col.set("db", None)
            col.set("catalog", None)
        return self.expr(tree.sql(dialect="postgres"))

    def rewrite_sql(self, sql: str | exp.Expression, read: str) -> tuple[str, list[str]]:
        """Render SQL for the target, pointing table references at migrated tables.

        Returns (sql, unknown_table_refs).
        """
        tree = (
            sql
            if isinstance(sql, exp.Expression)
            else sqlglot.parse_one(sql, read=DIALECT_MAP.get(read, read))
        )
        tree = tree.copy()
        replacements: dict[str, str] = {}
        unknown: list[str] = []
        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for tbl in list(tree.find_all(exp.Table)):
            raw = ".".join(p for p in (tbl.catalog, tbl.db, tbl.name) if p)
            if not raw or tbl.name.lower() in cte_names:
                continue
            found = self.resolver.find(raw)
            if found is None:
                unknown.append(raw)
                continue
            ph = _PLACEHOLDER.format(len(replacements))
            replacements[ph] = self.relation(found)
            tbl.set("this", exp.to_identifier(ph))
            tbl.set("db", None)
            tbl.set("catalog", None)
        out = tree.sql(dialect=self.dialect, pretty=True)
        for ph, rel in replacements.items():
            out = re.sub(rf'["`\[]?{ph}["`\]]?', lambda _m, r=rel: r, out, flags=re.IGNORECASE)
        return out, sorted(set(unknown))


def _true(cond: str) -> str:
    # Portable boolean test (T-SQL has no boolean type): NULL counts as false, like SSIS/Informatica.
    return f"(CASE WHEN {cond} THEN 1 ELSE 0 END) = 1"


def _false(cond: str) -> str:
    return f"(CASE WHEN {cond} THEN 1 ELSE 0 END) = 0"


class FlowSqlBuilder:
    """Turns one neutral data-flow graph into one SELECT per target, as chained CTEs."""

    def __init__(
        self,
        renderer: SqlRenderer,
        package: PackageIR,
        flow: DataFlowGraphIR,
        accepted: dict[str, str] | None = None,
    ) -> None:
        self.r = renderer
        self.pkg = package
        self.flow = flow
        self.accepted = accepted or {}
        self.ctes: list[str] = []
        self.cte_names: set[str] = set()
        self.out: dict[str, dict[str, Rel]] = {}
        self.residues: list[GeneratedResidue] = []
        self.nodes = {n.id: n for n in flow.nodes}

    # -- helpers -------------------------------------------------------------------------------
    def _residue(
        self, node: NodeIR, code: str, detail: str, snippet: str = "", key: str | None = None
    ) -> None:
        self.residues.append(
            GeneratedResidue(
                object_key=self.pkg.id,
                component_name=node.name,
                reason_code=code,
                reason_detail=detail,
                original_snippet=snippet
                or (node.opaque.original_snippet if node.opaque else node.name),
                component_key=key or node.id,
            )
        )

    def _add(self, base: str, select_sql: str, columns: list[str] | None) -> Rel:
        name = safe_name(f"s{len(self.ctes) + 1}_{base}")[:60]
        while name in self.cte_names:
            name += "_x"
        self.cte_names.add(name)
        self.ctes.append(f"{name} AS (\n    {select_sql}\n)")
        return Rel(name, columns)

    @staticmethod
    def _cols(rel: Rel, prefix: str = "") -> str:
        if rel.columns is None:
            return f"{prefix}*"
        return ", ".join(f"{prefix}{c}" for c in rel.columns) or f"{prefix}*"

    def _inputs(self, node: NodeIR) -> list[Rel]:
        rels: list[Rel] = []
        for e in self.flow.edges:
            if e.target_node_id != node.id or e.source_node_id not in self.out:
                continue
            branches = self.out[e.source_node_id]
            if len(branches) == 1:
                rel = next(iter(branches.values()))
            else:
                src = self.nodes[e.source_node_id]
                if src.operator == NodeOperator.LOOKUP:
                    is_no_match = "no match" in e.branch_name.lower()
                    rel = branches[NO_MATCH_BRANCH if is_no_match else DEFAULT_BRANCH]
                else:
                    rel = branches.get(e.branch_name, branches[DEFAULT_BRANCH])
            if e.column_lineage and node.operator != NodeOperator.TARGET:
                rel = self._project(node, rel, e.column_lineage, e.projection == "strict")
            rels.append(rel)
        return rels

    def _project(self, node: NodeIR, rel: Rel, lineage: list[dict[str, str]], strict: bool) -> Rel:
        """Apply column renames on an edge (Informatica ports / DataStage link columns)."""
        pairs = list(dict.fromkeys((self.r.col(m["from"]), self.r.col(m["to"])) for m in lineage))
        if rel.columns is not None:
            missing = sorted({f for f, _ in pairs if f not in rel.columns})
            if missing:
                self._residue(
                    node,
                    "COLUMN_NOT_PRODUCED",
                    f"'{node.name}' reads columns the previous step does not produce: {missing}",
                    key=f"{node.id}:inputs",
                )
                pairs = [(f, t) for f, t in pairs if f not in missing]
        if not strict and all(f == t for f, t in pairs):
            return rel
        targets = [t for _, t in pairs]
        parts = [f if f == t else f"{f} AS {t}" for f, t in pairs]
        if strict:
            cols: list[str] | None = list(dict.fromkeys(targets))
        elif rel.columns is None:
            parts = ["*", *(p for p, (f, t) in zip(parts, pairs, strict=True) if f != t)]
            cols = None
        else:
            keep = [c for c in rel.columns if c not in targets]
            parts = keep + parts
            cols = keep + targets
        return self._add(f"{node.name}_in", f"SELECT {', '.join(parts)} FROM {rel.name}", cols)

    def _render(self, node: NodeIR, neutral_sql: str, what: str, key: str | None = None) -> str:
        """Render an expression for the target, or raise a review item and return None-marker."""
        try:
            return self.r.expr(neutral_sql)
        except ExpressionNotPortable as exc:
            self._residue(
                node,
                "EXPRESSION_NOT_PORTABLE",
                f"{what} cannot be expressed exactly on {self.r.gen.platform}: {exc}",
                neutral_sql,
                key=key,
            )
            raise

    def _order(self) -> list[NodeIR]:
        ups: dict[str, list[str]] = {n.id: [] for n in self.flow.nodes}
        for e in self.flow.edges:
            if e.target_node_id in ups:
                ups[e.target_node_id].append(e.source_node_id)
        ordered: list[NodeIR] = []
        seen: set[str] = set()

        def visit(nid: str) -> None:
            if nid in seen or nid not in self.nodes:
                return
            seen.add(nid)
            for u in ups[nid]:
                visit(u)
            ordered.append(self.nodes[nid])

        for n in self.flow.nodes:
            visit(n.id)
        return ordered

    # -- main ----------------------------------------------------------------------------------
    def build(self) -> list[GeneratedModel]:
        models: list[GeneratedModel] = []
        for node in self._order():
            handler = getattr(self, f"_op_{node.operator.value.lower()}", None)
            inputs = self._inputs(node)
            if node.operator == NodeOperator.OPAQUE or handler is None:
                self._opaque(node, inputs)
                continue
            if node.operator == NodeOperator.TARGET:
                model = self._op_target(node, inputs)
                if model:
                    models.append(model)
                continue
            if node.operator != NodeOperator.SOURCE and not inputs:
                self._residue(node, "NO_INPUT", f"'{node.name}' has no connected input.")
                continue
            handler(node, inputs)
        return models

    def _passthrough(self, node: NodeIR, inputs: list[Rel]) -> None:
        if inputs:
            self.out[node.id] = {DEFAULT_BRANCH: inputs[0]}

    def _opaque(self, node: NodeIR, inputs: list[Rel]) -> None:
        if node.id in self.accepted and not self.accepted[node.id].strip():
            self._passthrough(node, inputs)  # reviewer confirmed the component adds nothing
            return
        if node.id in self.accepted:
            body = self.accepted[node.id].strip().rstrip(";")
            if inputs:
                body = body.replace("{input}", inputs[0].name)
            self.out[node.id] = {
                DEFAULT_BRANCH: self._add(node.name, f"-- Reviewed replacement\n    {body}", None)
            }
            return
        detail = (
            node.opaque.reason_detail
            if node.opaque
            else f"'{node.source_component_type}' has no automatic conversion."
        )
        code = node.opaque.reason_code if node.opaque else "UNSUPPORTED_COMPONENT"
        self._residue(node, code, detail)
        self._passthrough(node, inputs)

    def _op_source(self, node: NodeIR, _inputs: list[Rel]) -> None:
        if node.sql_query:
            try:
                sql, unknown = self.r.rewrite_sql(
                    node.sql_query, str(node.metadata.get("sql_dialect") or self.r.source_dialect)
                )
            except (SqlglotError, ValueError) as exc:
                self._residue(node, "SOURCE_QUERY_UNPARSED", str(exc), node.sql_query)
                return
            if unknown:
                self._residue(
                    node,
                    "UNKNOWN_TABLE_REFERENCE",
                    f"Source query reads tables not in the catalog: {unknown}",
                    node.sql_query,
                )
            cols = (
                [self.r.col(c.name) for c in node.output_columns]
                if node.metadata.get("query_columns_aligned")
                else None
            )
            self.out[node.id] = {
                DEFAULT_BRANCH: self._add(node.name, sql.replace("\n", "\n    "), cols)
            }
            return
        table = self.r.resolver.find(node.table_ref)
        if table is None:
            self._residue(
                node,
                "UNKNOWN_SOURCE_TABLE",
                f"Source table '{node.table_ref}' is not in the discovered catalog.",
            )
            return
        cols = self.r.table_columns(table)
        self.out[node.id] = {
            DEFAULT_BRANCH: self._add(
                node.name, f"SELECT {', '.join(cols)} FROM {self.r.relation(table)}", cols
            )
        }

    def _op_filter(self, node: NodeIR, inputs: list[Rel]) -> None:
        cond = node.condition
        if cond is None or cond.is_opaque or not cond.neutral_sql:
            self._residue(
                node,
                "FILTER_CONDITION_UNKNOWN",
                "Filter condition could not be converted.",
                cond.original_expr if cond else "",
            )
            self._passthrough(node, inputs)
            return
        inp = inputs[0]
        try:
            where = _true(self._render(node, cond.neutral_sql, "Filter condition"))
        except ExpressionNotPortable:
            self._passthrough(node, inputs)
            return
        self.out[node.id] = {
            DEFAULT_BRANCH: self._add(
                node.name, f"SELECT {self._cols(inp)} FROM {inp.name} WHERE {where}", inp.columns
            )
        }

    def _op_expression(self, node: NodeIR, inputs: list[Rel]) -> None:
        inp = inputs[0]
        derived: dict[str, str] = {}
        for e in node.expressions:
            if not e.target_column:
                self._residue(
                    node,
                    "EXPRESSION_TARGET_UNKNOWN",
                    "Derivation has no output column name.",
                    e.original_expr,
                    key=f"{node.id}:{e.id}",
                )
                continue
            col = self.r.col(e.target_column)
            if e.is_opaque or not e.neutral_sql:
                self._residue(
                    node,
                    "EXPRESSION_OPAQUE",
                    e.opaque_reason or "Expression could not be converted.",
                    e.original_expr,
                    key=f"{node.id}:{col}",
                )
                derived[col] = "NULL"
                continue
            try:
                derived[col] = self._render(
                    node, e.neutral_sql, f"Column '{col}'", key=f"{node.id}:{col}"
                )
            except ExpressionNotPortable:
                derived[col] = "NULL"
        if inp.columns is None:
            proj = ", ".join(["*", *(f"{sql} AS {c}" for c, sql in derived.items())])
            cols = None
        else:
            proj_parts = [f"{derived[c]} AS {c}" if c in derived else c for c in inp.columns]
            proj_parts += [f"{sql} AS {c}" for c, sql in derived.items() if c not in inp.columns]
            proj = ", ".join(proj_parts)
            cols = inp.columns + [c for c in derived if c not in inp.columns]
        self.out[node.id] = {
            DEFAULT_BRANCH: self._add(node.name, f"SELECT {proj} FROM {inp.name}", cols)
        }

    def _op_router(self, node: NodeIR, inputs: list[Rel]) -> None:
        inp = inputs[0]
        if any(e.is_opaque for e in node.expressions):
            self._residue(
                node, "ROUTE_CONDITION_OPAQUE", "A route condition could not be converted."
            )
            self._passthrough(node, inputs)
            return
        # SSIS Conditional Split sends a row to the FIRST matching output; Informatica Router and
        # DataStage Transformer links send it to EVERY matching output.
        semantics = node.metadata.get("route_semantics")
        first_match = (
            semantics or ("first" if self.pkg.source_tool == "ssis" else "all")
        ) == "first"
        try:
            conds = [
                (r["name"], self._render(node, r["condition"], f"Route '{r['name']}'"))
                for r in node.routes
            ]
        except ExpressionNotPortable:
            self._passthrough(node, inputs)
            return
        branches: dict[str, Rel] = {}
        for i, (name, cond) in enumerate(conds):
            preds = [_true(cond)]
            if first_match:
                preds += [_false(c) for _, c in conds[:i]]
            where = " AND ".join(preds)
            branches[name] = self._add(
                f"{node.name}_{name}",
                f"SELECT {self._cols(inp)} FROM {inp.name} WHERE {where}",
                inp.columns,
            )
        default_where = " AND ".join(_false(c) for _, c in conds) or "1 = 1"
        branches[DEFAULT_BRANCH] = self._add(
            f"{node.name}_default",
            f"SELECT {self._cols(inp)} FROM {inp.name} WHERE {default_where}",
            inp.columns,
        )
        self.out[node.id] = branches

    def _op_lookup(self, node: NodeIR, inputs: list[Rel]) -> None:
        inp = inputs[0]
        ref_table = self.r.resolver.find(node.table_ref)
        if not node.join_keys:
            self._residue(
                node,
                "LOOKUP_KEYS_UNKNOWN",
                "Lookup join columns are not defined in the source package; refusing to guess.",
            )
            self._passthrough(node, inputs)
            return
        if ref_table is None:
            self._residue(
                node,
                "UNKNOWN_LOOKUP_TABLE",
                f"Lookup reference '{node.table_ref or node.metadata.get('reference_query')}' is not a known table.",
            )
            self._passthrough(node, inputs)
            return
        ref_cols = {c.name.lower() for c in ref_table.columns}
        copies = list(node.metadata.get("copy_columns", []))  # type: ignore[arg-type]
        missing = [c["reference"] for c in copies if c["reference"].lower() not in ref_cols]
        missing += [k["right"] for k in node.join_keys if k["right"].lower() not in ref_cols]
        if missing:
            self._residue(
                node,
                "LOOKUP_COLUMN_UNKNOWN",
                f"Lookup refers to columns not in {ref_table.fqn}: {missing}",
            )
            self._passthrough(node, inputs)
            return
        if inp.columns is not None:
            missing_in = [
                k["left"] for k in node.join_keys if self.r.col(k["left"]) not in inp.columns
            ]
            if missing_in:
                self._residue(
                    node,
                    "LOOKUP_COLUMN_UNKNOWN",
                    f"Lookup input columns are not produced by the flow: {missing_in}",
                )
                self._passthrough(node, inputs)
                return
        ref_where = ""
        if node.metadata.get("reference_filter"):
            try:
                ref_where = " WHERE " + _true(
                    self.r.condition_sql(
                        str(node.metadata["reference_filter"]),
                        str(node.metadata.get("sql_dialect") or self.r.source_dialect),
                    )
                )
            except (SqlglotError, ValueError) as exc:
                self._residue(
                    node,
                    "LOOKUP_FILTER_UNCONVERTED",
                    f"Lookup source filter could not be converted: {exc}",
                    str(node.metadata["reference_filter"]),
                )
                self._passthrough(node, inputs)
                return
        rk = [self.r.col(k["right"]) for k in node.join_keys]
        keep = list(dict.fromkeys(rk + [self.r.col(c["reference"]) for c in copies]))
        # Legacy lookups return one reference row per key; dedupe so a join can't multiply rows
        # (unless the lookup is configured to return every match).
        if node.metadata.get("return_all_matches"):
            ref_sql = f"SELECT {', '.join(keep)} FROM {self.r.relation(ref_table)}{ref_where}"
        else:
            ref_sql = (
                f"SELECT {', '.join(keep)} FROM (SELECT {', '.join(keep)}, ROW_NUMBER() OVER "
                f"(PARTITION BY {', '.join(rk)} ORDER BY {', '.join(rk)}) AS l2c_rn "
                f"FROM {self.r.relation(ref_table)}{ref_where}) d WHERE l2c_rn = 1"
            )
        ref = self._add(f"{node.name}_ref", ref_sql, keep)
        on = " AND ".join(
            f"i.{self.r.col(k['left'])} = r.{self.r.col(k['right'])}" for k in node.join_keys
        )
        ignore_failure = (
            str(node.metadata.get("error_row_disposition", "")).lower() == "ignorefailure"
            or node.metadata.get("on_no_match") == "keep"
        )
        join = "LEFT JOIN" if ignore_failure else "INNER JOIN"
        aliases = [self.r.col(c["alias"]) for c in copies]
        copy_proj = [f"r.{self.r.col(c['reference'])} AS {self.r.col(c['alias'])}" for c in copies]
        # A looked-up column replaces an input column of the same name.
        kept_in = None if inp.columns is None else [c for c in inp.columns if c not in aliases]
        in_proj = self._cols(inp, "i.") if kept_in is None else ", ".join(f"i.{c}" for c in kept_in)
        proj = ", ".join(p for p in [in_proj, *copy_proj] if p)
        out_cols = None if kept_in is None else kept_in + aliases
        note = (
            ""
            if ignore_failure or node.metadata.get("no_match_output")
            else (
                "-- Legacy package fails on rows with no lookup match; here they are dropped.\n    "
            )
        )
        branches = {
            DEFAULT_BRANCH: self._add(
                f"{node.name}_match",
                f"{note}SELECT {proj} FROM {inp.name} i {join} {ref.name} r ON {on}",
                out_cols,
            )
        }
        branches[NO_MATCH_BRANCH] = self._add(
            f"{node.name}_nomatch",
            f"SELECT {self._cols(inp, 'i.')} FROM {inp.name} i WHERE NOT EXISTS (SELECT 1 FROM {ref.name} r WHERE {on})",
            inp.columns,
        )
        self.out[node.id] = branches

    def _op_join(self, node: NodeIR, inputs: list[Rel]) -> None:
        if len(inputs) < 2 or not node.join_keys:
            self._residue(
                node,
                "JOIN_SPEC_UNKNOWN",
                "Join inputs or join columns are not defined; refusing to guess.",
            )
            self._passthrough(node, inputs)
            return
        left, right = inputs[0], inputs[1]
        if left.columns is None or right.columns is None:
            self._residue(node, "JOIN_COLUMNS_UNKNOWN", "Columns of the joined inputs are unknown.")
            self._passthrough(node, inputs)
            return
        extra = [c for c in right.columns if c not in left.columns]
        on = " AND ".join(
            f"l.{self.r.col(k['left'])} = r.{self.r.col(k['right'])}" for k in node.join_keys
        )
        jt = (node.join_type or "INNER").upper()
        proj = ", ".join([*(f"l.{c}" for c in left.columns), *(f"r.{c}" for c in extra)])
        self.out[node.id] = {
            DEFAULT_BRANCH: self._add(
                node.name,
                f"SELECT {proj} FROM {left.name} l {jt} JOIN {right.name} r ON {on}",
                left.columns + extra,
            )
        }

    def _op_aggregate(self, node: NodeIR, inputs: list[Rel]) -> None:
        aggs = node.metadata.get("aggregations") or []
        if not aggs:
            self._residue(
                node,
                "AGGREGATE_SPEC_UNKNOWN",
                "Aggregation functions/columns are not defined; refusing to guess.",
            )
            self._passthrough(node, inputs)
            return
        inp = inputs[0]
        groups = [self.r.col(g) for g in node.group_by_columns]
        parts = list(groups)
        for a in aggs:  # type: ignore[union-attr]
            alias = self.r.col(a["alias"])
            if a.get("sql"):  # a full aggregate expression in neutral SQL
                try:
                    body = self._render(
                        node, a["sql"], f"Aggregate '{alias}'", f"{node.id}:{alias}"
                    )
                except ExpressionNotPortable:
                    self._passthrough(node, inputs)
                    return
            else:
                body = f"{a['func'].upper()}({self.r.col(a['column']) if a.get('column') else '*'})"
            parts.append(f"{body} AS {alias}")
        group_sql = f" GROUP BY {', '.join(groups)}" if groups else ""
        cols = groups + [self.r.col(a["alias"]) for a in aggs]  # type: ignore[index]
        self.out[node.id] = {
            DEFAULT_BRANCH: self._add(
                node.name, f"SELECT {', '.join(parts)} FROM {inp.name}{group_sql}", cols
            )
        }

    def _op_sort(self, node: NodeIR, inputs: list[Rel]) -> None:
        self._passthrough(node, inputs)  # row order has no meaning for a table

    def _op_deduplicate(self, node: NodeIR, inputs: list[Rel]) -> None:
        if not node.dedup_keys:
            self._residue(
                node,
                "DEDUP_KEYS_UNKNOWN",
                "Duplicate-removal columns are not defined; refusing to guess.",
            )
            self._passthrough(node, inputs)
            return
        inp = inputs[0]
        keys = ", ".join(self.r.col(k) for k in node.dedup_keys)
        # Which duplicate survives: the legacy sort order when known (else any - all are equal
        # on the keys, and the parser only allows that when no other column is kept).
        order = (
            ", ".join(
                f"{self.r.col(o['column'])} {o.get('direction', 'ASC')}"
                for o in node.metadata.get("order_by") or []  # type: ignore[union-attr]
            )
            or keys
        )
        body = (
            f"SELECT {self._cols(inp)} FROM (SELECT {self._cols(inp)}, ROW_NUMBER() OVER "
            f"(PARTITION BY {keys} ORDER BY {order}) AS l2c_rn FROM {inp.name}) d WHERE l2c_rn = 1"
        )
        self.out[node.id] = {DEFAULT_BRANCH: self._add(node.name, body, inp.columns)}

    def _op_union(self, node: NodeIR, inputs: list[Rel]) -> None:
        if len(inputs) == 1:
            self._passthrough(node, inputs)
            return
        if any(i.columns is None for i in inputs):
            self._residue(
                node,
                "UNION_COLUMNS_UNKNOWN",
                "Columns of the union inputs are unknown; cannot align them safely.",
            )
            self._passthrough(node, inputs)
            return
        cols = inputs[0].columns or []
        if any(set(i.columns or []) != set(cols) for i in inputs):
            self._residue(node, "UNION_COLUMN_MISMATCH", "Union inputs have different columns.")
        parts = [
            f"SELECT {', '.join(c if c in (i.columns or []) else f'NULL AS {c}' for c in cols)} FROM {i.name}"
            for i in inputs
        ]
        self.out[node.id] = {
            DEFAULT_BRANCH: self._add(node.name, "\n    UNION ALL\n    ".join(parts), cols)
        }

    def _used_ctes(self, final: str) -> list[str]:
        """Only the CTEs the final relation depends on (other branches are left out)."""
        bodies = {c.split(" AS (", 1)[0]: c for c in self.ctes}
        needed: set[str] = set()
        stack = [final]
        while stack:
            name = stack.pop()
            if name in needed or name not in bodies:
                continue
            needed.add(name)
            body = bodies[name].split(" AS (", 1)[1]
            stack += [n for n in bodies if re.search(rf"\b{n}\b", body)]
        return [c for c in self.ctes if c.split(" AS (", 1)[0] in needed]

    def _op_target(self, node: NodeIR, inputs: list[Rel]) -> GeneratedModel | None:
        if not inputs:
            self._residue(node, "NO_INPUT", f"Target '{node.name}' has no connected input.")
            return None
        inp = inputs[0]
        table = self.r.resolver.find(node.table_ref)
        mapping = list(node.metadata.get("column_mapping", []))  # type: ignore[arg-type]
        proj: list[str] = []
        if mapping:
            mapped = {self.r.col(m["target"]): self.r.col(m["source"]) for m in mapping}
            for tgt, src in mapped.items():
                if inp.columns is not None and src not in inp.columns:
                    self._residue(
                        node,
                        "TARGET_SOURCE_COLUMN_MISSING",
                        f"Mapped column '{src}' is not produced by the flow.",
                    )
                proj.append(src if src == tgt else f"{src} AS {tgt}")
            if table is not None:
                for c in table.columns:
                    tc = self.r.col(c.name)
                    if tc in mapped:
                        continue
                    if c.nullable:
                        proj.append(f"NULL AS {tc}")
                    else:
                        self._residue(
                            node,
                            "TARGET_COLUMN_UNMAPPED",
                            f"Required target column '{tc}' is not mapped.",
                        )
        elif table is not None:
            if inp.columns is None:
                self._residue(
                    node,
                    "TARGET_MAPPING_UNKNOWN",
                    "Target column mapping is not defined and flow columns are unknown.",
                )
                proj = ["*"]
            else:
                for c in table.columns:
                    tc = self.r.col(c.name)
                    if tc in inp.columns:
                        proj.append(tc)
                    elif c.nullable:
                        proj.append(f"NULL AS {tc}")
                    else:
                        self._residue(
                            node,
                            "TARGET_COLUMN_UNMAPPED",
                            f"Required target column '{tc}' is not produced by the flow or mapped.",
                        )
        else:
            proj = [self._cols(inp)]
        target_name = (node.table_ref or node.name).replace("[", "").replace("]", "").split(".")[-1]
        body = (
            "WITH " + ",\n".join(used) + f"\nSELECT {', '.join(proj)} FROM {inp.name}"
            if (used := self._used_ctes(inp.name))
            else f"SELECT {', '.join(proj)} FROM {inp.name}"
        )
        return GeneratedModel(
            name=f"{safe_name(self.pkg.name)}__{safe_name(target_name)}",
            target_ref=node.table_ref,
            sql=body,
        )


def render_sql_tasks(
    renderer: SqlRenderer,
    package: PackageIR,
    read_dialect: str,
    accepted: dict[str, str] | None = None,
) -> RenderResult:
    """Convert Execute-SQL style tasks: SELECT/INSERT...SELECT/CREATE VIEW become models, the rest steps."""
    res = RenderResult()
    accepted = accepted or {}
    pkg_name = safe_name(package.name)
    for idx, task in enumerate(package.tasks, start=1):
        if task.task_type != "execute_sql" or not task.sql_statement:
            continue
        if task.id in accepted:
            if accepted[
                task.id
            ].strip():  # empty approval = reviewer confirmed the step isn't needed
                res.steps.append(GeneratedStep(f"{pkg_name}__step_{idx}", accepted[task.id]))
            continue
        try:
            stmts = [
                s
                for s in sqlglot.parse(
                    task.sql_statement, read=DIALECT_MAP.get(read_dialect, read_dialect)
                )
                if s
            ]
        except (SqlglotError, ValueError) as exc:
            res.residues.append(
                GeneratedResidue(
                    package.id, task.name, "SQL_PARSE_ERROR", str(exc), task.sql_statement, task.id
                )
            )
            continue
        for s_idx, stmt in enumerate(stmts, start=1):
            suffix = f"{idx}" if len(stmts) == 1 else f"{idx}_{s_idx}"
            try:
                if (
                    isinstance(stmt, exp.Create)
                    and str(stmt.args.get("kind", "")).upper() == "VIEW"
                    and stmt.expression
                ):
                    view = stmt.this.this if isinstance(stmt.this, exp.Schema) else stmt.this
                    sql, unknown = renderer.rewrite_sql(stmt.expression, "neutral")
                    res.models.append(GeneratedModel(safe_name(view.name), None, sql))
                elif isinstance(stmt, exp.Insert) and isinstance(stmt.expression, exp.Query):
                    target = stmt.this.this if isinstance(stmt.this, exp.Schema) else stmt.this
                    select = stmt.expression.copy()
                    if isinstance(stmt.this, exp.Schema) and isinstance(select, exp.Select):
                        names = [c.name for c in stmt.this.expressions]
                        if len(names) == len(select.expressions):
                            select.set(
                                "expressions",
                                [
                                    exp.alias_(e.unalias(), renderer.col(n))
                                    for e, n in zip(select.expressions, names, strict=True)
                                ],
                            )
                    sql, unknown = renderer.rewrite_sql(select, "neutral")
                    res.models.append(
                        GeneratedModel(f"{pkg_name}__{safe_name(target.name)}", target.sql(), sql)
                    )
                elif isinstance(stmt, exp.Query):
                    sql, unknown = renderer.rewrite_sql(stmt, "neutral")
                    res.models.append(GeneratedModel(f"{pkg_name}__query_{suffix}", None, sql))
                else:
                    sql, unknown = renderer.rewrite_sql(
                        stmt, "neutral" if read_dialect == "neutral" else read_dialect
                    )
                    res.steps.append(GeneratedStep(f"{pkg_name}__step_{suffix}", sql))
            except (SqlglotError, ValueError) as exc:
                res.residues.append(
                    GeneratedResidue(
                        package.id, task.name, "SQL_CONVERSION_ERROR", str(exc), stmt.sql(), task.id
                    )
                )
                continue
            if unknown:
                res.residues.append(
                    GeneratedResidue(
                        package.id,
                        task.name,
                        "UNKNOWN_TABLE_REFERENCE",
                        f"Statement uses tables not in the discovered catalog: {unknown}. Map them or confirm they are not needed.",
                        stmt.sql(),
                        task.id,
                    )
                )
    return res
