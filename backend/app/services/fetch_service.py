"""取数服务：模板参数绑定清单、覆盖结果与报表快照。

这个模块把“取数”需要的三件事放在一起：

1. 模板注册表：OLAP 查询模板和数据 API 模板各自记录参数定义、数据范围和
   历史版本，空字符串这类有歧义的输入值必须由模板自己写明含义。
2. 覆盖计算：只使用查询语句里实际引用到的参数（``used_by_query``），返回
   命中的商品分组、空值记录数，并把含混模板单独列出来待复核。
3. 报表快照：交付时冻结参数、定义版本和覆盖结果；之后模板定义更正只产生
   “按当时参数重算”的结果，不会覆盖已交付的报表。

跨服务执行的策略在 ``POLICIES`` 里显式定义：默认 ``confirm``，发现空值含义
冲突时先要求确认；``local`` 沿用各模板已记录的局部含义并在结果里标注范围，
供已交付报表回查；``all`` 把所有冲突的空字符串当作“不筛选”并返回全组；
``defer`` 暂缓含空值含义待确认的模板，全部模板都被暂缓时不返回任何行。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from app.services.parameter_binding import (
    ParameterContractError,
    TemplateDefinition,
    build_manifest,
    merge_issues,
    request_fingerprint,
)

POLICIES = ("confirm", "local", "all", "defer")
DEFAULT_POLICY = "confirm"

# 覆盖计算用的商品明细事实数据：故意保留 None 和空字符串两种真实空值，
# 让“空值记录数”可以落到具体来源字段上。
FACT_ROWS: list[dict[str, Any]] = [
    {"id": "R-001", "product_group": "PG-01", "category": "家电", "region": "华东", "channel": "线上", "amount": 120, "owner": "张三"},
    {"id": "R-002", "product_group": "PG-01", "category": "家电", "region": "华东", "channel": "线下", "amount": None, "owner": "张三"},
    {"id": "R-003", "product_group": "PG-02", "category": "家居", "region": "华南", "channel": "线上", "amount": 0, "owner": "李四"},
    {"id": "R-004", "product_group": "PG-02", "category": "家居", "region": "华南", "channel": None, "amount": 50, "owner": "李四"},
    {"id": "R-005", "product_group": "PG-03", "category": "数码", "region": "华北", "channel": "线上", "amount": 200, "owner": None},
    {"id": "R-006", "product_group": "PG-03", "category": "数码", "region": "", "channel": "线下", "amount": 0, "owner": "王五"},
    {"id": "R-007", "product_group": "PG-04", "category": "家电", "region": None, "channel": None, "amount": 80, "owner": "王五"},
    {"id": "R-008", "product_group": "PG-05", "category": "家居", "region": "华东", "channel": "线上", "amount": 120, "owner": "李四"},
    {"id": "R-009", "product_group": "PG-05", "category": "家居", "region": "华东", "channel": "", "amount": 0, "owner": None},
    {"id": "R-010", "product_group": "PG-06", "category": "数码", "region": "华南", "channel": "", "amount": 60, "owner": "张三"},
    {"id": "R-011", "product_group": "PG-07", "category": "家电", "region": "华南", "channel": "线下", "amount": None, "owner": "王五"},
    {"id": "R-012", "product_group": "PG-08", "category": None, "region": "华东", "channel": "线上", "amount": 30, "owner": "李四"},
    {"id": "R-013", "product_group": "PG-08", "category": "", "region": "华东", "channel": "抖音", "amount": 30, "owner": "李四"},
    {"id": "R-014", "product_group": "PG-09", "category": "家电", "region": "华北", "channel": "线上", "amount": 0, "owner": None},
]

TEMPLATE_SEEDS: list[dict[str, Any]] = [
    {
        "module": "query_builder",
        "template_id": 1,
        "template_code": "QUER-0001",
        "template_name": "OLAP 历史模板 · 商品类别",
        "scope": "商品分组",
        "query_text": "SELECT product_group FROM fact WHERE category = {category}",
        "conditions": [
            {"variable": "category", "field": "category", "type": "string", "empty_means": "all"},
        ],
    },
    {
        "module": "query_builder",
        "template_id": 2,
        "template_code": "QUER-0002",
        "template_name": "OLAP 历史模板 · 区域",
        "scope": "商品分组",
        "query_text": "SELECT product_group FROM fact WHERE region = {region}",
        "conditions": [
            {"variable": "region", "field": "region", "type": "string", "empty_means": "all"},
        ],
    },
    {
        "module": "query_builder",
        "template_id": 3,
        "template_code": "QUER-0003",
        "template_name": "OLAP 历史模板 · 金额（商品明细范围）",
        "scope": "商品分组",
        "query_text": (
            "SELECT product_group FROM fact WHERE amount = {amount} "
            "AND category = {category}"
        ),
        "conditions": [
            {"variable": "amount", "field": "amount", "type": "number", "empty_means": "all"},
            {"variable": "category", "field": "category", "type": "string", "scope": "商品明细", "empty_means": "all"},
        ],
    },
    {
        "module": "data_service",
        "template_id": 1,
        "template_code": "DATA-0001",
        "template_name": "数据API · 商品空值统计",
        "scope": "商品分组",
        "query_text": "SELECT product_group FROM fact WHERE category = {category}",
        "conditions": [
            {"variable": "category", "field": "category", "type": "string", "empty_means": "empty"},
            # 历史模板里还留着一个查询语句没有引用的条件，取数时不得使用。
            {"variable": "channel", "field": "channel", "type": "string", "used": False, "empty_means": "all"},
        ],
    },
    {
        "module": "data_service",
        "template_id": 2,
        "template_code": "DATA-0002",
        "template_name": "数据API · 双空值语义模板",
        "scope": "商品分组",
        "query_text": (
            "SELECT product_group FROM fact WHERE category = {category} "
            "AND owner = {owner}"
        ),
        "conditions": [
            {"variable": "category", "field": "category", "type": "string", "empty_means": "all"},
            {"variable": "owner", "field": "owner", "type": "string", "empty_means": "empty"},
        ],
    },
    {
        "module": "data_service",
        "template_id": 3,
        "template_code": "DATA-0003",
        "template_name": "数据API · 待补记空值语义",
        "scope": "商品分组",
        "query_text": "SELECT product_group FROM fact WHERE category = {category}",
        "conditions": [
            # 这条历史定义没有记录空值含义：按历史行为先当“不筛选”，并标待复核。
            {"variable": "category", "field": "category", "type": "string"},
        ],
    },
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_null(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if left is None or right is None:
        return left is right
    return str(left) == str(right)


def _matches(row: Mapping[str, Any], binding: Mapping[str, Any]) -> bool:
    operator = binding.get("operator")
    field = str(binding.get("source_field") or "")
    value = row.get(field)
    if operator in {"all", "unset"}:
        return True
    if operator == "is_empty":
        return _is_null(value)
    if operator == "eq":
        return _same_value(value, binding.get("normalized_value"))
    return True


class FetchService:
    """取数模板、覆盖结果与报表快照的内存实现。"""

    def __init__(self) -> None:
        self._templates: dict[tuple[str, int], TemplateDefinition] = {}
        self._template_meta: dict[tuple[str, int], dict[str, Any]] = {}
        self._reports: dict[int, dict[str, Any]] = {}
        self._next_report_id = 1
        for seed in TEMPLATE_SEEDS:
            definition = TemplateDefinition(
                module=seed["module"],
                template_id=int(seed["template_id"]),
                code=seed["template_code"],
                name=seed["template_name"],
                scope=seed["scope"],
                query_text=seed["query_text"],
                conditions=[dict(item) for item in seed["conditions"]],
                version=1,
                note="seed",
            )
            key = (definition.module, definition.template_id)
            self._templates[key] = definition
            self._template_meta[key] = {"created_at": _now(), "updated_at": _now()}

    # ------------------------------------------------------------------
    # 模板
    # ------------------------------------------------------------------

    def list_templates(self) -> list[dict[str, Any]]:
        return [self._template_view(key) for key in sorted(self._templates)]

    def get_template(self, module: str, template_id: int) -> dict[str, Any]:
        key = (module, int(template_id))
        if key not in self._templates:
            raise ParameterContractError(
                "TEMPLATE_NOT_FOUND",
                f"取数模板 {module}:{template_id} 不存在",
                status_code=404,
            )
        return self._template_view(key)

    def _template_view(self, key: tuple[str, int]) -> dict[str, Any]:
        definition = self._templates[key]
        conditions = definition.normalized_conditions()
        empty_meanings: dict[str, list[str]] = {}
        for condition in conditions:
            meaning = str(condition.get("empty_means") or "unconfirmed")
            empty_meanings.setdefault(meaning, []).append(condition["variable"])
        view = definition.to_dict(include_history=True)
        view.update(self._template_meta[key])
        view["list_entry"] = self._list_entry(definition.module, definition.template_id)
        view["empty_meanings"] = sorted(empty_meanings)
        view["variables_by_empty_meaning"] = empty_meanings
        view["used_variables"] = [
            condition["variable"] for condition in conditions if condition.get("used")
        ]
        view["ignored_variables"] = [
            condition["variable"] for condition in conditions if not condition.get("used")
        ]
        view["needs_review"] = any(
            str(condition.get("empty_means") or "unconfirmed") == "unconfirmed"
            for condition in conditions
            if condition.get("used")
        )
        return view

    @staticmethod
    def _list_entry(module: str, template_id: int) -> dict[str, Any] | None:
        """取数模板与列表里同编号的业务记录对齐，便于回查旧列表的原始字段。"""

        from app.store import store

        row = store.find(module, int(template_id))
        if row is None:
            return None
        code_field = "模板编号" if module == "query_builder" else "服务编号"
        return {
            "id": row.get("id"),
            "code": row.get(code_field),
            "status": row.get("status"),
            "legacy_definition_text": row.get("参数定义") or row.get("查询参数"),
            "legacy_scope_text": row.get("数据集范围") or row.get("输出数据集"),
        }

    def update_definition(
        self,
        module: str,
        template_id: int,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """更正模板参数定义：版本递增、保留历史，并重算引用报表。"""

        key = (module, int(template_id))
        definition = self._templates.get(key)
        if definition is None:
            raise ParameterContractError(
                "TEMPLATE_NOT_FOUND",
                f"取数模板 {module}:{template_id} 不存在",
                status_code=404,
            )
        expected = payload.get("expected_version")
        if expected is not None and int(expected) != definition.version:
            raise ParameterContractError(
                "TEMPLATE_VERSION_CONFLICT",
                f"模板 {definition.code} 当前版本是 {definition.version}，"
                f"提交的期望版本是 {expected}，请先刷新",
                status_code=409,
            )
        snapshot = definition.to_dict(include_history=False)
        if "conditions" in payload:
            conditions = payload.get("conditions")
            if not isinstance(conditions, list):
                raise ParameterContractError(
                    "INVALID_DEFINITION",
                    "conditions 必须是参数定义列表",
                )
            definition.conditions = [dict(item) for item in conditions if isinstance(item, dict)]
        if "query_text" in payload:
            definition.query_text = str(payload.get("query_text") or "")
        if "scope" in payload:
            definition.scope = str(payload.get("scope") or "")
        if "note" in payload:
            definition.note = str(payload.get("note") or "")
        definition.version += 1
        definition.history.append(
            {
                "version": snapshot["version"],
                "recorded_at": _now(),
                "definition": snapshot,
            }
        )
        self._template_meta[key]["updated_at"] = _now()

        recalculations = self._recalculate_reports(key)
        return {
            "template": self._template_view(key),
            "recalculated_reports": recalculations,
        }

    def _recalculate_reports(self, changed_key: tuple[str, int]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for report in self._reports.values():
            specs = report.get("templates") or []
            if not any(
                (spec.get("module"), int(spec.get("template_id", 0))) == changed_key
                for spec in specs
            ):
                continue
            before = (report.get("delivered") or {}).get("coverage") or report.get("coverage")
            try:
                current = self.execute(
                    specs,
                    policy=report.get("policy") or DEFAULT_POLICY,
                    confirmations=report.get("confirmations") or {},
                )
            except ParameterContractError as error:
                current = {"status": "blocked", "code": error.code, "message": error.message}
            record = {
                "version": self._templates[changed_key].version,
                "recorded_at": _now(),
                "coverage": current,
                "changed": _coverage_signature(before) != _coverage_signature(current),
            }
            report.setdefault("recalculations", []).append(record)
            results.append({"report_id": report["id"], **record})
        return results

    # ------------------------------------------------------------------
    # 参数绑定与覆盖
    # ------------------------------------------------------------------

    def _definition(self, module: str, template_id: Any) -> TemplateDefinition:
        key = (str(module), int(template_id))
        definition = self._templates.get(key)
        if definition is None:
            raise ParameterContractError(
                "TEMPLATE_NOT_FOUND",
                f"取数模板 {module}:{template_id} 不存在",
                status_code=404,
            )
        return definition

    def manifest(
        self,
        module: str,
        template_id: Any,
        params: Mapping[str, Any] | None = None,
        *,
        confirmations: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        definition = self._definition(module, template_id)
        result = build_manifest(definition, params, confirmations=confirmations)
        result["used_conditions"] = [
            item for item in definition.normalized_conditions() if item.get("used")
        ]
        result["ignored_conditions"] = [
            item for item in definition.normalized_conditions() if not item.get("used")
        ]
        return result

    def preview(
        self,
        module: str,
        template_id: Any,
        params: Mapping[str, Any] | None = None,
        *,
        confirmations: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        manifest = self.manifest(module, template_id, params, confirmations=confirmations)
        definition = self._definition(module, template_id)
        coverage = self.execute(
            [{"module": definition.module, "template_id": definition.template_id, "params": dict(params or {})}],
            policy="local",
            confirmations=confirmations,
        )
        return {"manifest": manifest, "coverage": coverage}

    def execute(
        self,
        templates: Sequence[Mapping[str, Any]],
        *,
        policy: str = DEFAULT_POLICY,
        confirmations: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if policy not in POLICIES:
            raise ParameterContractError(
                "INVALID_POLICY",
                f"未知的跨服务策略「{policy}」，可选：{'、'.join(POLICIES)}",
            )
        specs = [self._normalize_spec(spec) for spec in templates]
        if not specs:
            raise ParameterContractError("INVALID_REQUEST", "至少需要一个取数模板")

        manifests = [
            {
                "spec": spec,
                "manifest": self.manifest(
                    spec["module"],
                    spec["template_id"],
                    spec["params"],
                    confirmations=confirmations,
                ),
            }
            for spec in specs
        ]

        duplicate_variables = {
            key
            for key, value in _variable_key_counts(manifests).items()
            if value > 1
        }
        conflicts = self._scope_conflicts(manifests, duplicate_variables)
        if conflicts:
            _raise_scope_conflict(conflicts)

        ambiguity = self._ambiguity(manifests)
        if policy == "confirm" and ambiguity["pending_templates"]:
            supplied = set((confirmations or {}).keys())
            unresolved = [
                item
                for item in ambiguity["pending_variables"]
                if item["confirmation_key"] not in supplied and item["variable"] not in supplied
            ]
            if unresolved:
                raise ParameterContractError(
                    "PARAM_CONFIRMATION_REQUIRED",
                    "跨服务报表存在空值含义冲突，请先确认绑定再发布："
                    + "；".join(item["message"] for item in unresolved),
                    status_code=409,
                )
            # 确认之后仍然使用各模板已记录的局部含义，但用确认值覆盖歧义项。
            manifests = [
                {
                    "spec": item["spec"],
                    "manifest": self.manifest(
                        item["spec"]["module"],
                        item["spec"]["template_id"],
                        item["spec"]["params"],
                        confirmations=confirmations,
                    ),
                }
                for item in manifests
            ]
            ambiguity = self._ambiguity(manifests)

        skipped: set[tuple[str, int]] = set()
        notes: list[str] = []
        if policy == "defer":
            for template in ambiguity["pending_templates"]:
                skipped.add((template["module"], int(template["template_id"])))
                notes.append(
                    f"暂缓模板 {template['template_code']}：空值含义待确认，先不参与覆盖"
                )
        elif policy == "all":
            for item in ambiguity["pending_variables"]:
                notes.append(
                    f"变量 {item['variable']} 的空字符串统一按“不筛选”处理（返回全组）"
                )
        elif policy == "local":
            for item in ambiguity["pending_variables"]:
                notes.append(
                    f"变量 {item['variable']} 沿用各模板已记录的局部含义，范围"
                    f"：{'、'.join(item['scopes']) or '未标注'}"
                )

        bindings: list[dict[str, Any]] = []
        executed_templates: list[dict[str, Any]] = []
        for item in manifests:
            spec = item["spec"]
            key = (spec["module"], spec["template_id"])
            if key in skipped:
                continue
            manifest = item["manifest"]
            executed_templates.append(
                {
                    "module": spec["module"],
                    "template_id": spec["template_id"],
                    "template_code": manifest["template_code"],
                    "scope": manifest["scope"],
                    "definition_version": manifest["definition_version"],
                }
            )
            for binding in manifest["actual_bindings"]:
                adjusted = dict(binding)
                if (
                    policy == "all"
                    and adjusted["presence"] == "empty_string"
                    and any(
                        adjusted["variable"] == pending["variable"]
                        for pending in ambiguity["pending_variables"]
                    )
                ):
                    adjusted["operator"] = "all"
                    adjusted["active"] = False
                    adjusted["annotation"] = (
                        f"跨服务策略 all：空字符串按“不筛选”处理"
                        f"（范围：{adjusted.get('scope') or '未标注'}）"
                    )
                bindings.append(adjusted)

        if policy == "defer" and skipped and not executed_templates:
            # 所有模板都含待确认的空值含义：先不返回数据，等确认跨服务绑定后再取。
            rows: list[Mapping[str, Any]] = []
        else:
            rows = [row for row in FACT_ROWS if all(_matches(row, binding) for binding in bindings)]
        group_values = sorted({str(row.get("product_group")) for row in rows if row.get("product_group") is not None})
        null_by_field: dict[str, int] = {}
        for binding in bindings:
            field = str(binding.get("source_field") or "")
            if not field or field in null_by_field:
                continue
            null_by_field[field] = sum(1 for row in rows if _is_null(row.get(field)))
        null_record_count = sum(
            1
            for row in rows
            if any(_is_null(row.get(str(binding.get("source_field") or ""))) for binding in bindings)
        )
        status = "ok"
        if policy == "defer" and skipped:
            status = "pending_confirmation"
        elif ambiguity["pending_templates"] and policy == "local":
            status = "ok_with_ambiguity"

        request_parts: list[Mapping[str, Any]] = []
        for item in manifests:
            request_parts.extend(item["manifest"]["bindings"])
        request_key = request_fingerprint(
            [
                {
                    "scope": part.get("scope"),
                    "variable": part.get("variable"),
                    "source_field": part.get("source_field"),
                    "presence": part.get("presence"),
                    "normalized_value": part.get("normalized_value"),
                }
                for part in request_parts
            ]
        )
        return {
            "status": status,
            "policy": policy,
            "records": len(rows),
            "record_ids": [row["id"] for row in rows],
            "product_groups": group_values,
            "group_count": len(group_values),
            "null_record_count": null_record_count,
            "null_by_field": null_by_field,
            "actual_conditions": [
                {
                    "module": item["spec"]["module"],
                    "template_id": item["spec"]["template_id"],
                    "variable": binding["variable"],
                    "source_field": binding["source_field"],
                    "business_type": binding["business_type"],
                    "input_value": binding["input_value"],
                    "presence": binding["presence"],
                    "operator": binding["operator"],
                    "empty_means": binding["empty_means"],
                    "scope": binding.get("scope"),
                    "active": binding["active"],
                    "annotation": binding["annotation"],
                }
                for item in manifests
                if (item["spec"]["module"], item["spec"]["template_id"]) not in skipped
                for binding in item["manifest"]["actual_bindings"]
            ],
            "ignored_conditions": [
                {
                    "module": item["spec"]["module"],
                    "template_id": item["spec"]["template_id"],
                    "variable": condition["variable"],
                    "source_field": condition["field"],
                    "reason": "查询语句未引用该变量",
                }
                for item in manifests
                for condition in item["manifest"]["ignored_conditions"]
            ],
            "executed_templates": executed_templates,
            "pending_templates": ambiguity["pending_templates"],
            "pending_variables": ambiguity["pending_variables"],
            "ambiguous_variables": ambiguity["pending_variables"],
            "bindings": bindings,
            "notes": notes,
            "request_key": request_key,
        }

    def _normalize_spec(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        module = str(spec.get("module") or "").strip()
        if not module:
            raise ParameterContractError("INVALID_REQUEST", "模板缺少 module")
        try:
            template_id = int(spec.get("template_id"))
        except (TypeError, ValueError):
            raise ParameterContractError("INVALID_REQUEST", "模板缺少有效的 template_id") from None
        params = spec.get("params") or {}
        if not isinstance(params, Mapping):
            raise ParameterContractError("INVALID_REQUEST", "params 必须是对象")
        self._definition(module, template_id)
        return {"module": module, "template_id": template_id, "params": dict(params)}

    def _scope_conflicts(
        self,
        manifests: Sequence[Mapping[str, Any]],
        duplicate_variables: set[str],
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        for item in manifests:
            conflicts.extend(item["manifest"].get("scope_conflicts") or [])
        merged = merge_issues([item["manifest"] for item in manifests])
        conflicts.extend(
            issue
            for issue in merged
            if issue["code"] == "PARAM_SCOPE_CONFLICT"
            and str(issue.get("variable")) in duplicate_variables
        )
        # 去掉重复的同一变量/范围组合。
        seen: set[tuple[str, tuple[str, ...]]] = set()
        unique = []
        for item in conflicts:
            key = (str(item.get("variable")), tuple(item.get("scopes") or []))
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    def _ambiguity(self, manifests: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """找出空值含义未记录或跨模板不一致的变量/模板。"""

        pending_variables: list[dict[str, Any]] = []
        pending_templates: dict[tuple[str, int], dict[str, Any]] = {}

        by_variable: dict[str, list[dict[str, Any]]] = {}
        for item in manifests:
            manifest = item["manifest"]
            for binding in manifest["actual_bindings"]:
                if binding["presence"] != "empty_string":
                    continue
                by_variable.setdefault(binding["variable"], []).append(
                    {**binding, "manifest": manifest, "spec": item["spec"]}
                )

        for variable, bindings in sorted(by_variable.items()):
            meanings = sorted({str(binding["empty_means"]) for binding in bindings})
            needs = "unconfirmed" in meanings or len(meanings) > 1
            if not needs:
                continue
            scopes = sorted({str(binding.get("scope") or "") for binding in bindings})
            confirm_key = (
                f"{bindings[0]['manifest']['module']}:"
                f"{bindings[0]['manifest']['template_id']}:{variable}"
            )
            pending_variables.append(
                {
                    "variable": variable,
                    "meanings": meanings,
                    "scopes": scopes,
                    "confirmation_key": confirm_key,
                    "message": (
                        f"变量 {variable} 的空字符串在模板之间被解释为"
                        f"{'、'.join(meanings)}（范围：{'、'.join(scopes) or '未标注'}）"
                    ),
                }
            )
            for binding in bindings:
                manifest = binding["manifest"]
                pending_templates[(manifest["module"], int(manifest["template_id"]))] = {
                    "module": manifest["module"],
                    "template_id": manifest["template_id"],
                    "template_code": manifest["template_code"],
                    "scope": manifest["scope"],
                    "empty_means": binding["empty_means"],
                    "variable": variable,
                }

        return {
            "pending_variables": pending_variables,
            "pending_templates": sorted(
                pending_templates.values(),
                key=lambda item: (item["module"], item["template_id"]),
            ),
        }

    # ------------------------------------------------------------------
    # 数据 API 发布视角
    # ------------------------------------------------------------------

    def data_service_coverage(
        self,
        template_id: Any,
        params: Mapping[str, Any] | None = None,
        *,
        confirmations: Mapping[str, str] | None = None,
        policy: str = "local",
    ) -> dict[str, Any]:
        """数据 API 发布：只抽出查询实际使用的来源条件并计算覆盖。"""

        manifest = self.manifest("data_service", template_id, params, confirmations=confirmations)
        coverage = self.execute(
            [{"module": "data_service", "template_id": int(template_id), "params": dict(params or {})}],
            policy=policy,
            confirmations=confirmations,
        )
        review = self.ambiguous_templates(manifest)
        return {
            "manifest": manifest,
            "used_source_conditions": [
                {
                    "variable": binding["variable"],
                    "source_field": binding["source_field"],
                    "business_type": binding["business_type"],
                    "input_value": binding["input_value"],
                    "presence": binding["presence"],
                    "empty_means": binding["empty_means"],
                    "scope": binding["scope"],
                    "operator": binding["operator"],
                }
                for binding in manifest["actual_bindings"]
            ],
            "ignored_source_conditions": manifest["ignored_conditions"],
            "coverage": coverage,
            "templates_pending_review": review,
        }

    def ambiguous_templates(self, manifest: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """列出空值含义未记录、或与其它模板冲突的模板，供发布前复核。"""

        target_variables: dict[str, set[str]] = {}
        target_scopes: dict[str, set[str]] = {}
        if manifest:
            for binding in manifest.get("actual_bindings", []):
                target_variables.setdefault(binding["variable"], set()).add(binding["empty_means"])
                target_scopes.setdefault(binding["variable"], set()).add(str(binding.get("scope") or ""))
        review: list[dict[str, Any]] = []
        for key in sorted(self._templates):
            definition = self._templates[key]
            reasons: list[str] = []
            for condition in definition.normalized_conditions():
                if not condition.get("used"):
                    continue
                variable = condition["variable"]
                meaning = str(condition.get("empty_means") or "unconfirmed")
                if meaning == "unconfirmed":
                    reasons.append(f"变量 {variable} 未记录空值含义")
                if variable in target_variables:
                    if meaning not in target_variables[variable]:
                        reasons.append(
                            f"变量 {variable} 的空值含义为 {meaning}，与当前请求的"
                            f"{'、'.join(sorted(target_variables[variable]))} 不一致"
                        )
                    if str(condition.get("scope") or definition.scope or "") not in target_scopes.get(variable, set()):
                        reasons.append(
                            f"变量 {variable} 的数据范围与当前请求不同"
                        )
            if reasons:
                review.append(
                    {
                        "module": definition.module,
                        "template_id": definition.template_id,
                        "template_code": definition.code,
                        "template_name": definition.name,
                        "scope": definition.scope,
                        "version": definition.version,
                        "reasons": sorted(set(reasons)),
                    }
                )
        return review

    # ------------------------------------------------------------------
    # 报表
    # ------------------------------------------------------------------

    def create_report(
        self,
        *,
        name: str | None = None,
        templates: Sequence[Mapping[str, Any]],
        policy: str = DEFAULT_POLICY,
        confirmations: Mapping[str, str] | None = None,
        remark: str | None = None,
    ) -> dict[str, Any]:
        specs = [self._normalize_spec(spec) for spec in templates]
        coverage = self.execute(specs, policy=policy, confirmations=confirmations)
        versions = {
            f"{spec['module']}:{spec['template_id']}": self._definition(
                spec["module"], spec["template_id"]
            ).version
            for spec in specs
        }
        definitions = {
            f"{spec['module']}:{spec['template_id']}": self._definition(
                spec["module"], spec["template_id"]
            ).to_dict(include_history=False)
            for spec in specs
        }
        report_id = self._next_report_id
        self._next_report_id += 1
        report = {
            "id": report_id,
            "name": name or f"取数报表-{report_id}",
            "status": "draft" if coverage["status"] == "ok" else coverage["status"],
            "policy": policy,
            "templates": specs,
            "confirmations": dict(confirmations or {}),
            "remark": remark,
            "created_at": _now(),
            "delivered_at": None,
            "definition_versions": versions,
            "definition_snapshots": definitions,
            "coverage": coverage,
            "delivered": None,
            "recalculations": [],
        }
        self._reports[report_id] = report
        return report

    def deliver_report(self, report_id: int) -> dict[str, Any]:
        report = self._reports.get(int(report_id))
        if report is None:
            raise ParameterContractError(
                "REPORT_NOT_FOUND", f"报表 {report_id} 不存在", status_code=404
            )
        coverage = report["coverage"]
        if coverage["status"] == "pending_confirmation":
            raise ParameterContractError(
                "PARAM_CONFIRMATION_REQUIRED",
                "报表含待确认的空值含义，确认跨服务绑定后才能发布："
                + "、".join(
                    item["template_code"] for item in coverage.get("pending_templates", [])
                ),
                status_code=409,
            )
        if report.get("delivered") is not None:
            return report
        report["delivered"] = {
            "delivered_at": _now(),
            "definition_versions": dict(report["definition_versions"]),
            "definition_snapshots": dict(report["definition_snapshots"]),
            "templates": [dict(spec) for spec in report["templates"]],
            "params_at_delivery": [dict(spec["params"]) for spec in report["templates"]],
            "coverage": coverage,
            "policy": report["policy"],
            "scope_annotations": [
                {
                    "variable": item["variable"],
                    "scope": item.get("scope"),
                    "empty_means": item.get("empty_means"),
                    "annotation": item.get("annotation"),
                }
                for item in coverage.get("actual_conditions", [])
                if item.get("presence") == "empty_string"
            ],
            "templates_pending_review": coverage.get("pending_templates", []),
        }
        report["delivered_at"] = report["delivered"]["delivered_at"]
        report["status"] = "delivered"
        return report

    def get_report(self, report_id: int) -> dict[str, Any]:
        report = self._reports.get(int(report_id))
        if report is None:
            raise ParameterContractError(
                "REPORT_NOT_FOUND", f"报表 {report_id} 不存在", status_code=404
            )
        return self._report_view(report)

    def list_reports(self) -> list[dict[str, Any]]:
        return [self._report_view(report) for report in sorted(
            self._reports.values(), key=lambda item: item["id"]
        )]

    def recalculate_report(self, report_id: int) -> dict[str, Any]:
        report = self._reports.get(int(report_id))
        if report is None:
            raise ParameterContractError(
                "REPORT_NOT_FOUND", f"报表 {report_id} 不存在", status_code=404
            )
        specs = report.get("templates") or []
        current = self.execute(
            specs,
            policy=report.get("policy") or DEFAULT_POLICY,
            confirmations=report.get("confirmations") or {},
        )
        delivered = (report.get("delivered") or {}).get("coverage")
        return {
            "report_id": report["id"],
            "delivered_coverage": delivered,
            "current_coverage": current,
            "changed": _coverage_signature(delivered) != _coverage_signature(current),
            "definition_versions_at_delivery": (
                (report.get("delivered") or {}).get("definition_versions")
                or report.get("definition_versions")
            ),
            "current_definition_versions": {
                f"{spec['module']}:{spec['template_id']}": self._definition(
                    spec["module"], spec["template_id"]
                ).version
                for spec in specs
            },
        }

    def _report_view(self, report: Mapping[str, Any]) -> dict[str, Any]:
        delivered = report.get("delivered")
        recalculations = report.get("recalculations") or []
        return {
            "id": report["id"],
            "name": report["name"],
            "status": report["status"],
            "policy": report["policy"],
            "templates": report["templates"],
            "created_at": report["created_at"],
            "delivered_at": report["delivered_at"],
            "coverage": report["coverage"],
            "delivered": delivered,
            "params_at_delivery": (delivered or {}).get("params_at_delivery"),
            "definition_versions_at_delivery": (delivered or {}).get("definition_versions"),
            "recalculations": recalculations,
            "latest_recalculation": recalculations[-1] if recalculations else None,
        }

    def compare(
        self,
        templates: Sequence[Mapping[str, Any]],
        *,
        policies: Iterable[str] = ("all", "defer"),
        confirmations: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """并排比较不同策略下的覆盖结果，默认比较“返回全组”和“暂缓确认”。"""

        specs = [self._normalize_spec(spec) for spec in templates]
        comparisons: dict[str, Any] = {}
        for policy in policies:
            try:
                comparisons[policy] = self.execute(
                    specs, policy=policy, confirmations=confirmations
                )
            except ParameterContractError as error:
                comparisons[policy] = {
                    "status": "blocked",
                    "policy": policy,
                    "code": error.code,
                    "message": error.message,
                }
        return {
            "templates": specs,
            "policies": list(policies),
            "comparisons": comparisons,
        }


def _variable_key_counts(manifests: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """统计同一变量在本次请求里出现的次数。

    同一个变量只在一个模板里出现、但被绑到多个范围时，由该模板自己的
    ``scope_conflicts`` 报告；只有变量跨模板重复出现，才用合并检查判断
    两个模板是否把同名变量绑到了不同范围。
    """

    counts: dict[str, int] = {}
    for item in manifests:
        for binding in item["manifest"].get("bindings", []):
            variable = str(binding.get("variable"))
            counts[variable] = counts.get(variable, 0) + 1
    return counts


def _coverage_signature(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    return (
        tuple(value.get("product_groups") or []),
        value.get("group_count"),
        value.get("records"),
        value.get("null_record_count"),
        tuple(sorted((value.get("null_by_field") or {}).items())),
        value.get("status"),
    )


def _raise_scope_conflict(conflicts: Sequence[Mapping[str, Any]]) -> None:
    # 只保留一个稳定错误码，细节放在 message 里，避免路由层处理多错误结构。
    raise ParameterContractError(
        "PARAM_SCOPE_CONFLICT",
        "；".join(str(item.get("message")) for item in conflicts),
        status_code=409,
    )


fetch_service = FetchService()
