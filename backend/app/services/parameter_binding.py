"""取数服务参数绑定契约。

OLAP 查询列表和数据 API 发布列表原先各自实现了同一件事：把查询串里的条件
转成对内存行的过滤。本模块把这件事收敛成一份可复用的绑定清单：

* 每个输入值都保留原始值、业务类型和“存在形态”（未填写 / 空字符串 / 数值零 / 有值）；
* 空字符串的解释由模板自己记录（``empty_means = all | empty``），清单里带上作用范围；
* 同一变量绑定到不同数据范围时会产生 ``PARAM_SCOPE_CONFLICT``，不能当成同一个请求去重；
* 清单同时给出请求指纹，指纹包含范围，因此不同范围的同名变量不会被混为一次请求。

列表接口的历史语义保持不变：``keyword`` / ``status`` 只在空字符串或未传时
不参与过滤（纯空白仍按历史参与），扩展字段 ``strip()`` 后为空则不参与；字符串
``"0"`` 仍然是有效过滤值。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

UNSET = object()

EMPTY_MEANINGS = {"all", "empty", "unset", "reject", "unconfirmed"}
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class Presence(str, Enum):
    """一个输入值在参数绑定中的存在形态。"""

    MISSING = "missing"
    EMPTY = "empty_string"
    ZERO = "zero"
    VALUE = "value"


class BusinessType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    UNKNOWN = "unknown"


class ParameterContractError(ValueError):
    """参数契约错误：路由层据此给出稳定的错误码。"""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "message": self.message}


# ---------------------------------------------------------------------------
# 值分类
# ---------------------------------------------------------------------------


def normalize_business_type(raw_type: Any, value: Any = UNSET) -> BusinessType:
    if isinstance(raw_type, BusinessType):
        return raw_type
    text = str(raw_type or "").strip().lower()
    aliases = {
        "str": BusinessType.STRING,
        "text": BusinessType.STRING,
        "字符串": BusinessType.STRING,
        "int": BusinessType.NUMBER,
        "float": BusinessType.NUMBER,
        "decimal": BusinessType.NUMBER,
        "数字": BusinessType.NUMBER,
        "数值": BusinessType.NUMBER,
        "bool": BusinessType.BOOLEAN,
        "布尔": BusinessType.BOOLEAN,
    }
    if text in aliases:
        return aliases[text]
    try:
        return BusinessType(text)
    except ValueError:
        pass
    if isinstance(value, bool):
        return BusinessType.BOOLEAN
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return BusinessType.NUMBER
    if isinstance(value, str):
        return BusinessType.STRING
    return BusinessType.UNKNOWN


def _is_blank(raw: Any) -> bool:
    return isinstance(raw, str) and raw.strip() == ""


def _numeric_zero(raw: Any, business_type: BusinessType) -> bool:
    if isinstance(raw, bool) or business_type is BusinessType.BOOLEAN:
        return False
    if isinstance(raw, (int, float, Decimal)) and not isinstance(raw, bool):
        return raw == 0
    if isinstance(raw, str):
        if business_type is BusinessType.STRING:
            return False
        text = raw.strip()
        if not text:
            return False
        try:
            return Decimal(text) == 0
        except (InvalidOperation, ValueError):
            return False
    return False


def classify_presence(raw: Any = UNSET, business_type: Any = None) -> Presence:
    """区分未填写、空字符串、数值零和普通值。

    ``None`` 和 ``UNSET`` 都表示未填写（JSON 里的 null 不携带业务值）；空字符串
    （含纯空白）单独归为 ``EMPTY``；只有数值零或数字型参数里的 ``"0"`` 归为
    ``ZERO``。字符串型参数的 ``"0"`` 是一个真实的字符串值，仍然归类为 ``VALUE``。
    """

    if raw is UNSET or raw is None:
        return Presence.MISSING
    if _is_blank(raw):
        return Presence.EMPTY
    btype = normalize_business_type(business_type, raw)
    if _numeric_zero(raw, btype):
        return Presence.ZERO
    return Presence.VALUE


def coerce_value(raw: Any, business_type: Any) -> Any:
    """按业务类型保留/还原输入值，类型无法转换时保留原始值。"""

    if raw is UNSET or raw is None:
        return None
    btype = normalize_business_type(business_type, raw)
    if btype is BusinessType.NUMBER:
        if isinstance(raw, bool):
            return raw
        try:
            number = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError):
            return raw
        if number == number.to_integral_value():
            return int(number)
        return float(number)
    if btype is BusinessType.BOOLEAN:
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in {"true", "1", "yes", "y", "是"}:
            return True
        if text in {"false", "0", "no", "n", "否"}:
            return False
        return raw
    if btype is BusinessType.STRING:
        return raw if isinstance(raw, str) else str(raw)
    return raw


def value_fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def request_fingerprint(parts: Iterable[Mapping[str, Any]]) -> str:
    """生成请求指纹；范围（scope）是指纹的一部分，范围不同即不同请求。"""

    canonical = [
        {
            "scope": str(part.get("scope") or ""),
            "variable": str(part.get("variable") or ""),
            "field": str(part.get("source_field") or ""),
            "presence": str(part.get("presence") or ""),
            "value": part.get("normalized_value"),
        }
        for part in parts
    ]
    canonical.sort(key=lambda item: (item["scope"], item["variable"], item["field"]))
    return value_fingerprint(canonical)


# ---------------------------------------------------------------------------
# 定义与绑定
# ---------------------------------------------------------------------------


@dataclass
class Binding:
    """一个变量在某个模板、某个范围里的绑定结果。"""

    variable: str
    source_field: str
    business_type: BusinessType
    input_value: Any
    presence: Presence
    scope: str
    operator: str
    empty_means: str
    active: bool
    normalized_value: Any
    used_by_query: bool
    ambiguous: bool = False
    annotation: str = ""
    default_value: Any = UNSET

    def _parts(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "variable": self.variable,
            "source_field": self.source_field,
            "presence": self.presence.value,
            "normalized_value": self.normalized_value,
        }

    def fingerprint(self) -> str:
        return request_fingerprint([self._parts()])

    def to_dict(self) -> dict[str, Any]:
        return {
            "variable": self.variable,
            "source_field": self.source_field,
            "business_type": self.business_type.value,
            "input_value": self.input_value,
            "presence": self.presence.value,
            "scope": self.scope,
            "operator": self.operator,
            "empty_means": self.empty_means,
            "active": self.active,
            "normalized_value": self.normalized_value,
            "used_by_query": self.used_by_query,
            "ambiguous": self.ambiguous,
            "annotation": self.annotation,
            "request_fingerprint": self.fingerprint(),
        }


@dataclass
class TemplateDefinition:
    """模板里记录的参数定义（可随更正递增版本）。"""

    module: str
    template_id: int
    code: str
    name: str
    scope: str
    query_text: str
    conditions: list[dict[str, Any]] = field(default_factory=list)
    version: int = 1
    note: str = ""
    last_params: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    def normalized_conditions(self) -> list[dict[str, Any]]:
        placeholders = set(PLACEHOLDER_RE.findall(self.query_text or ""))
        normalized: list[dict[str, Any]] = []
        for raw in self.conditions:
            item = dict(raw)
            variable = str(item.get("variable") or item.get("name") or item.get("参数") or "").strip()
            if not variable:
                continue
            source_field = str(
                item.get("field") or item.get("source_field") or item.get("来源字段") or variable
            )
            raw_type = item.get("type") or item.get("business_type") or item.get("类型") or "string"
            used = item.get("used")
            if used is None:
                used = (not placeholders) or variable in placeholders
            item.update(
                {
                    "variable": variable,
                    "field": source_field,
                    "type": str(raw_type),
                    "used": bool(used),
                }
            )
            normalized.append(item)
        return normalized

    def to_dict(self, *, include_history: bool = False) -> dict[str, Any]:
        payload = {
            "module": self.module,
            "template_id": self.template_id,
            "template_code": self.code,
            "template_name": self.name,
            "scope": self.scope,
            "query_text": self.query_text,
            "conditions": self.normalized_conditions(),
            "version": self.version,
            "note": self.note,
            "last_params": dict(self.last_params),
        }
        if include_history:
            payload["history"] = list(self.history)
        return payload


def bind_definition(
    definition: TemplateDefinition,
    params: Mapping[str, Any] | None = None,
    *,
    confirmations: Mapping[str, str] | None = None,
) -> list[Binding]:
    """把一次请求的参数绑定到模板定义上，保留原始值与业务类型。"""

    params = dict(params or {})
    confirmations = dict(confirmations or {})
    bindings: list[Binding] = []
    for condition in definition.normalized_conditions():
        variable = str(condition["variable"])
        scope = str(condition.get("scope") or definition.scope or "")
        raw = params.get(variable, UNSET)
        btype = normalize_business_type(condition.get("type") or condition.get("business_type"), raw)
        presence = classify_presence(raw, btype)
        declared_meaning = str(
            condition.get("empty_means") or condition.get("空值含义") or ""
        ).strip()
        if declared_meaning not in EMPTY_MEANINGS:
            declared_meaning = "unconfirmed"
        confirmation = confirmations.get(
            f"{definition.module}:{definition.template_id}:{variable}",
            confirmations.get(f"{definition.scope}:{variable}", confirmations.get(variable)),
        )
        meaning = str(confirmation or declared_meaning).strip()
        if meaning not in EMPTY_MEANINGS:
            meaning = "unconfirmed"

        operator = str(condition.get("operator") or "eq")
        active = False
        ambiguous = presence is Presence.EMPTY and meaning == "unconfirmed"
        annotation = ""
        normalized = coerce_value(raw, btype)

        if presence is Presence.MISSING:
            operator = "unset"
            annotation = f"未填写 {variable}，不参与过滤"
        elif presence is Presence.EMPTY:
            if meaning == "empty":
                operator = "is_empty"
                active = True
                annotation = (
                    f"空字符串按模板 {definition.code} 的局部含义解释为“字段为空”"
                    f"（范围：{scope or '未标注'}）"
                )
            elif meaning == "all":
                operator = "all"
                annotation = (
                    f"空字符串按模板 {definition.code} 的局部含义解释为“不筛选”"
                    f"（范围：{scope or '未标注'}）"
                )
            elif meaning == "unset":
                operator = "unset"
                annotation = (
                    f"空字符串按模板 {definition.code} 视同未填写，不参与过滤"
                    f"（范围：{scope or '未标注'}）"
                )
            elif meaning == "reject":
                raise ParameterContractError(
                    "PARAM_EMPTY_REJECTED",
                    f"参数 {variable} 不接受空字符串，请填写明确值",
                )
            else:
                operator = "all"
                ambiguous = True
                annotation = (
                    f"空字符串在模板 {definition.code} 中未记录含义，按历史不筛选处理并待复核"
                    f"（范围：{scope or '未标注'}）"
                )
        elif presence is Presence.ZERO:
            active = True
            operator = "eq"
            annotation = f"数值零是有效值，按 0 过滤（范围：{scope or '未标注'}）"
        else:
            active = True
            annotation = f"按 {operator} 过滤（范围：{scope or '未标注'}）"

        bindings.append(
            Binding(
                variable=variable,
                source_field=str(condition.get("field") or variable),
                business_type=btype,
                input_value=None if raw is UNSET else raw,
                presence=presence,
                scope=scope,
                operator=operator,
                empty_means=meaning,
                active=active,
                normalized_value=normalized,
                used_by_query=bool(condition.get("used", True)),
                ambiguous=ambiguous,
                annotation=annotation,
                default_value=condition.get("default", UNSET),
            )
        )
    return bindings


def build_manifest(
    definition: TemplateDefinition,
    params: Mapping[str, Any] | None = None,
    *,
    confirmations: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    bindings = bind_definition(definition, params, confirmations=confirmations)
    actual = [binding for binding in bindings if binding.used_by_query]
    payload = {
        "module": definition.module,
        "template_id": definition.template_id,
        "template_code": definition.code,
        "template_name": definition.name,
        "scope": definition.scope,
        "definition_version": definition.version,
        "query_text": definition.query_text,
        "bindings": [binding.to_dict() for binding in bindings],
        "actual_bindings": [binding.to_dict() for binding in actual],
        "scope_conflicts": detect_scope_conflicts(bindings),
        "ambiguous_bindings": [
            binding.to_dict() for binding in bindings if binding.ambiguous
        ],
        "request_key": request_fingerprint([binding._parts() for binding in bindings]),
    }
    return payload


def detect_scope_conflicts(bindings: Sequence[Binding]) -> list[dict[str, Any]]:
    """同一变量绑定到多个范围时返回冲突；这种请求不能被当成同一次取数。"""

    by_variable: dict[str, set[str]] = {}
    for binding in bindings:
        by_variable.setdefault(binding.variable, set()).add(binding.scope)
    conflicts = []
    for variable, scopes in sorted(by_variable.items()):
        if len(scopes) > 1:
            conflicts.append(
                {
                    "code": "PARAM_SCOPE_CONFLICT",
                    "variable": variable,
                    "scopes": sorted(scopes),
                    "message": (
                        f"变量 {variable} 同时绑定到 {len(scopes)} 个数据范围："
                        f"{'、'.join(sorted(scopes))}；不能复用同一个请求"
                    ),
                }
            )
    return conflicts


def merge_issues(manifests: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """检查多个模板联用时的范围冲突与空值语义歧义。"""

    issues: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for manifest in manifests:
        for binding in manifest.get("bindings", []):
            grouped.setdefault(str(binding.get("variable")), []).append(
                {
                    **binding,
                    "template_module": manifest.get("module"),
                    "template_id": manifest.get("template_id"),
                    "template_code": manifest.get("template_code"),
                }
            )

    for variable, bindings in sorted(grouped.items()):
        scopes = sorted({str(item.get("scope") or "") for item in bindings})
        if len(scopes) > 1:
            issues.append(
                {
                    "code": "PARAM_SCOPE_CONFLICT",
                    "variable": variable,
                    "scopes": scopes,
                    "templates": [
                        {
                            "module": item["template_module"],
                            "id": item["template_id"],
                            "code": item["template_code"],
                            "scope": item.get("scope"),
                        }
                        for item in bindings
                    ],
                    "message": (
                        f"变量 {variable} 在不同模板里绑定到不同数据范围："
                        f"{'、'.join(scopes)}；不能当成同一个请求"
                    ),
                }
            )

        empty_bindings = [
            item
            for item in bindings
            if item.get("presence") == Presence.EMPTY.value
            and item.get("used_by_query", True)
        ]
        meanings = sorted({str(item.get("empty_means") or "") for item in empty_bindings})
        if len(meanings) > 1 or (len(meanings) == 1 and "unconfirmed" in meanings):
            issues.append(
                {
                    "code": "PARAM_EMPTY_AMBIGUOUS",
                    "variable": variable,
                    "meanings": meanings,
                    "templates": [
                        {
                            "module": item["template_module"],
                            "id": item["template_id"],
                            "code": item["template_code"],
                            "scope": item.get("scope"),
                            "empty_means": item.get("empty_means"),
                        }
                        for item in empty_bindings
                    ],
                    "message": (
                        f"变量 {variable} 的空字符串含义不一致："
                        f"{'、'.join(meanings)}；跨服务执行前需要确认沿用局部含义还是统一绑定"
                    ),
                }
            )
    return issues


def collect_list_filters(query_params: Mapping[str, str], reserved: Iterable[str]) -> dict[str, str]:
    """列表扩展字段的统一收集方式，与历史语义完全一致。

    未传、空字符串、纯空白都不参与过滤；``"0"`` 作为字符串是有效值。
    """

    reserved_set = set(reserved)
    extra: dict[str, str] = {}
    for key, value in query_params.items():
        if key in reserved_set:
            continue
        if not list_filter_active(value, strip=True):
            continue
        extra[key] = value
    return extra


def list_filter_active(value: Any, *, strip: bool = False) -> bool:
    """判断一个列表查询值是否参与过滤。

    历史列表把 ``keyword`` / ``status`` 当作普通真假值：只有空字符串和未传不
    过滤，纯空白仍然参与过滤；扩展字段则要求 ``strip()`` 后非空。两种写法都
    保留在这里，新增代码不要再各写一遍。
    """

    if value is None:
        return False
    if strip:
        return bool(str(value).strip())
    return bool(value)


def filter_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    keyword: str | None,
    keyword_field: str,
    status: str | None,
    extra: Mapping[str, str] | None,
) -> list[Mapping[str, Any]]:
    """OLAP 查询与数据 API 列表共用的过滤实现。"""

    result = list(rows)
    if list_filter_active(keyword):
        result = [row for row in result if str(keyword) in str(row.get(keyword_field, ""))]
    if list_filter_active(status):
        result = [row for row in result if row.get("status") == status]
    for field_name, value in (extra or {}).items():
        if not list_filter_active(value, strip=True):
            continue
        result = [row for row in result if str(value) in str(row.get(field_name, ""))]
    return result
