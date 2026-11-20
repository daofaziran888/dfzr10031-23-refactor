"""演示脚本：比较旧列表服务与新取数报表对“未填 / 空串 / 零”的处理。

用法（在 backend 目录）：
    python3 scripts/verify_fetch_contract.py
需要先安装 requirements.txt。输出是给人看的对照表，并带断言，失败时退出码非零。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import fetch_service as fetch_module  # noqa: E402
from app.services.parameter_binding import ParameterContractError  # noqa: E402
from app.store import store  # noqa: E402


def old_list(params: dict[str, str]) -> dict[str, object]:
    """直接调用旧列表服务，模拟查询串传入。"""

    from app.services.query_builder import QueryBuilderService

    query = {key: value for key, value in params.items() if value is not None}
    items, total = QueryBuilderService().list_entries(
        keyword=query.get("keyword"),
        status=query.get("status"),
        extra={key: value for key, value in query.items() if key not in {"keyword", "status"}}
        if query
        else None,
    )
    return {"total": total, "codes": [row["模板编号"] for row in items]}


def main() -> int:
    service = fetch_module.fetch_service

    print("== 1. 旧列表：未填 / 空串 / 零 ==")
    for label, params in (
        ("未填", {"keyword": None}),
        ("空串", {"keyword": ""}),
        ("零", {"keyword": "0"}),
    ):
        print(f"  keyword={label:<4} -> {old_list(params)}")

    print("\n== 2. 新取数报表：同一模板的三种输入 ==")
    reports = {}
    for label, value in (("未填", None), ("空串", ""), ("零", 0)):
        params = {} if value is None else {"amount": value}
        manifest = service.manifest("query_builder", 3, params)
        coverage = service.execute(
            [{"module": "query_builder", "template_id": 3, "params": params}],
            policy="local",
        )
        amount = next(item for item in manifest["bindings"] if item["variable"] == "amount")
        reports[label] = coverage
        print(
            f"  amount={label:<4} presence={amount['presence']:<12}"
            f" operator={amount['operator']:<6} 分组={coverage['group_count']}"
            f" 行数={coverage['records']} 指纹={coverage['request_key']}"
        )

    assert len({item["request_key"] for item in reports.values()}) == 3, "三种输入必须是不同请求"
    assert reports["未填"]["records"] == reports["空串"]["records"], "未填与空串在历史口径下都不过滤"

    print("\n== 3. 含两种真实空值含义的模板（DATA-0002） ==")
    dual = service.execute(
        [{"module": "data_service", "template_id": 2, "params": {"category": "", "owner": ""}}],
        policy="local",
    )
    for condition in dual["actual_conditions"]:
        print(
            f"  {condition['variable']}: 空串 -> {condition['empty_means']}"
            f"（范围：{condition['scope']}）=> {condition['operator']}"
        )
    print(
        f"  覆盖商品分组 {dual['product_groups']}，空值记录 {dual['null_record_count']}，"
        f"按字段 {dual['null_by_field']}"
    )

    print("\n== 4. 跨服务：OLAP 说“全部”，数据API 说“字段为空” ==")
    cross = [
        {"module": "query_builder", "template_id": 1, "params": {"category": ""}},
        {"module": "data_service", "template_id": 1, "params": {"category": ""}},
    ]
    comparison = service.compare(cross)
    for policy, result in comparison["comparisons"].items():
        print(
            f"  {policy:<7} status={result['status']:<22} 分组数={result['group_count']:<2}"
            f" 行数={result['records']:<2} 待确认={[t['template_code'] for t in result['pending_templates']]}"
        )
    try:
        service.execute(cross, policy="confirm")
    except ParameterContractError as error:
        print(f"  confirm 被拦下：{error.code}")

    print("\n== 5. 报表交付、定义更正与回查 ==")
    report = service.create_report(name="对照报表", templates=cross, policy="local")
    delivered = service.deliver_report(report["id"])
    print(f"  交付覆盖分组：{delivered['delivered']['coverage']['product_groups']}")
    correction = service.update_definition(
        "data_service",
        1,
        {"conditions": [{"variable": "category", "field": "category", "type": "string", "empty_means": "all"}]},
    )
    print(f"  模板版本 -> {correction['template']['version']}，重算报表 {len(correction['recalculated_reports'])} 条")
    check = service.recalculate_report(report["id"])
    print(f"  按当时参数回查交付覆盖：{check['delivered_coverage']['product_groups']}")
    print(f"  当前定义重算覆盖：{check['current_coverage']['product_groups']}")
    assert check["delivered_coverage"]["product_groups"] == delivered["delivered"]["coverage"]["product_groups"]
    assert check["changed"] is True

    store.reset()
    print("\n全部断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
