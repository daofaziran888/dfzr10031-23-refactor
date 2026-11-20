"""取数服务接口：参数绑定清单、跨服务覆盖、报表快照与模板定义更正。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.schemas import EntryPayload
from app.services.fetch_service import fetch_service

router = APIRouter(prefix="/api/fetch", tags=["取数服务"])


DEFAULT_COMPARE_POLICIES = ["all", "defer"]


def _policies(values: list[str] | None) -> list[str]:
    """默认比较“返回全组”与“暂缓确认”，也可以显式指定其它策略组合。"""

    chosen = [item for item in (values or []) if item]
    if chosen:
        return chosen
    return list(DEFAULT_COMPARE_POLICIES)


@router.get("/templates")
def list_templates() -> dict[str, Any]:
    """列出取数模板及其参数定义、数据范围、空值含义和历史版本。"""
    items = fetch_service.list_templates()
    return {"items": items, "total": len(items)}


@router.get("/templates/{module}/{template_id}")
def get_template(module: str, template_id: int) -> dict[str, Any]:
    """读取单个取数模板的当前定义。"""
    return fetch_service.get_template(module, template_id)


@router.put("/templates/{module}/{template_id}/definition")
def update_definition(module: str, template_id: int, payload: EntryPayload) -> dict[str, Any]:
    """更正模板参数定义：版本递增并重算覆盖过该模板的报表。"""
    return fetch_service.update_definition(module, template_id, payload.values)


@router.post("/parameter-bindings")
def parameter_bindings(payload: EntryPayload) -> dict[str, Any]:
    """按模板参数定义生成绑定清单，保留输入值、业务类型与空值形态。"""
    values = payload.values
    module = str(values.get("module") or "")
    template_id = values.get("template_id")
    params = values.get("params") or {}
    confirmations = values.get("confirmations") or {}
    return fetch_service.manifest(
        module,
        template_id,
        params,
        confirmations=confirmations,
    )


@router.post("/coverage")
def coverage(payload: EntryPayload) -> dict[str, Any]:
    """执行取数：只使用查询实际引用的条件，返回商品分组与空值记录数。"""
    values = payload.values
    templates = values.get("templates") or []
    policy = str(values.get("policy") or "confirm")
    confirmations = values.get("confirmations") or {}
    return fetch_service.execute(templates, policy=policy, confirmations=confirmations)


@router.post("/compare")
def compare(payload: EntryPayload) -> dict[str, Any]:
    """并排比较跨服务策略：默认“返回全组”和“暂缓确认”。"""
    values = payload.values
    templates = values.get("templates") or []
    policies = _policies(values.get("policies"))
    confirmations = values.get("confirmations") or {}
    return fetch_service.compare(templates, policies=policies, confirmations=confirmations)


@router.post("/data_service/{template_id}/coverage")
def data_service_coverage(
    template_id: int,
    request: Request,
    payload: EntryPayload | None = None,
) -> dict[str, Any]:
    """数据 API 发布视角：抽取查询实际使用的来源条件并计算覆盖。"""
    values = (payload.values if payload else {}) or {}
    query_confirmations = {
        key: value
        for key, value in request.query_params.items()
        if key.startswith("confirm.")
    }
    confirmations = dict(values.get("confirmations") or {})
    confirmations.update(
        {key.removeprefix("confirm."): value for key, value in query_confirmations.items()}
    )
    return fetch_service.data_service_coverage(
        template_id,
        values.get("params") or {},
        confirmations=confirmations,
        policy=str(values.get("policy") or "local"),
    )


@router.post("/reports")
def create_report(payload: EntryPayload) -> dict[str, Any]:
    """创建报表：冻结当时的参数、模板定义版本和覆盖结果。"""
    values = payload.values
    report = fetch_service.create_report(
        name=values.get("name"),
        templates=values.get("templates") or [],
        policy=str(values.get("policy") or "confirm"),
        confirmations=values.get("confirmations") or {},
        remark=values.get("remark") or payload.remark,
    )
    return fetch_service.get_report(report["id"])


@router.get("/reports")
def list_reports() -> dict[str, Any]:
    items = fetch_service.list_reports()
    return {"items": items, "total": len(items)}


@router.get("/reports/{report_id}")
def get_report(report_id: int) -> dict[str, Any]:
    """回查报表：交付时的覆盖结果与当前覆盖结果分别返回。"""
    return fetch_service.get_report(report_id)


@router.post("/reports/{report_id}/deliver")
def deliver_report(report_id: int) -> dict[str, Any]:
    """交付报表；含未确认空值含义的报表会被拦下并给出待复核模板。"""
    return fetch_service.get_report(fetch_service.deliver_report(report_id)["id"])


@router.post("/reports/{report_id}/recalculate")
def recalculate_report(report_id: int) -> dict[str, Any]:
    """按当时冻结的参数重算，并与交付时的覆盖结果对照。"""
    return fetch_service.recalculate_report(report_id)
