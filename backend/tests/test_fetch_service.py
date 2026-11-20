"""取数服务回归：旧列表兼容、参数绑定、覆盖结果与报表快照。"""
from __future__ import annotations

import pytest

from app.services import fetch_service as fetch_module
from app.services.parameter_binding import (
    BusinessType,
    ParameterContractError,
    Presence,
    classify_presence,
    collect_list_filters,
    merge_issues,
    build_manifest,
    TemplateDefinition,
)


# ---------------------------------------------------------------------------
# 旧列表：过滤语义与错误码
# ---------------------------------------------------------------------------


def test_legacy_list_keeps_unfiltered_semantics(client):
    base = client.get("/api/query_builder").json()
    assert base["total"] == 3
    assert client.get("/api/query_builder?keyword=").json()["total"] == 3

    filtered = client.get("/api/query_builder?keyword=QUER-0002").json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["模板编号"] == "QUER-0002"

    # 纯空白在旧实现里仍然按真假值参与过滤，保持原样。
    whitespace = client.get("/api/query_builder?keyword=%20").json()
    assert whitespace["total"] == 0


def test_legacy_list_extension_fields_and_paging(client):
    assert client.get("/api/query_builder?模板名称=样例2").json()["total"] == 1
    assert client.get("/api/query_builder?模板名称=").json()["total"] == 3
    assert client.get("/api/query_builder?模板名称=%20").json()["total"] == 3
    assert client.get("/api/query_builder?模板名称=样例").json()["total"] == 3
    paged = client.get("/api/data_service?page=2&size=2").json()
    assert paged["total"] == 3
    assert len(paged["items"]) == 1
    assert paged["page"] == 2 and paged["size"] == 2


def test_legacy_list_error_codes(client):
    too_big = client.get("/api/data_service?size=200")
    assert too_big.status_code == 200
    error = client.get("/api/data_service?size=201")
    assert error.status_code == 400
    assert error.json() == {"detail": "每页最多 200 条，请缩小分页范围"}
    missing = client.get("/api/query_builder/999")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "查询模板 999 不存在或已归档"


def test_legacy_list_actions_still_work(client):
    result = client.post(
        "/api/data_service/1/actions", json={"values": {"action": "发布服务"}}
    ).json()
    assert result["ok"] is True
    assert result["entry"]["status"] == "已发布"
    rejected = client.post(
        "/api/data_service/1/actions", json={"values": {"action": "不存在动作"}}
    ).json()
    assert rejected["ok"] is False
    assert rejected["message"] == "动作「不存在动作」不属于数据API发布可执行范围"


def test_collect_list_filters_matches_history():
    assert collect_list_filters({"a": "", "b": " ", "c": "0"}, {"page"}) == {"c": "0"}
    assert collect_list_filters({"page": "1", "keyword": ""}, {"page", "keyword"}) == {}


# ---------------------------------------------------------------------------
# 参数绑定：空串、零、未填写
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, presence",
    [
        ("", Presence.EMPTY),
        ("   ", Presence.EMPTY),
        (0, Presence.ZERO),
        (0.0, Presence.ZERO),
        (None, Presence.MISSING),
        ("abc", Presence.VALUE),
    ],
)
def test_presence_classification(raw, presence):
    assert classify_presence(raw, BusinessType.STRING) is presence


def test_zero_string_depends_on_business_type():
    assert classify_presence("0", BusinessType.STRING) is Presence.VALUE
    assert classify_presence("0", BusinessType.NUMBER) is Presence.ZERO
    assert classify_presence("0", BusinessType.BOOLEAN) is Presence.VALUE


def test_manifest_keeps_input_and_business_type():
    definition = TemplateDefinition(
        module="data_service",
        template_id=7,
        code="DATA-T-007",
        name="测试模板",
        scope="商品分组",
        query_text="SELECT 1 WHERE amount = {amount}",
        conditions=[
            {"variable": "amount", "field": "amount", "type": "number", "empty_means": "all"},
            {"variable": "note", "field": "note", "type": "string", "empty_means": "all"},
        ],
    )
    manifest = build_manifest(definition, {"amount": "0", "note": ""})
    by_name = {item["variable"]: item for item in manifest["bindings"]}
    assert by_name["amount"]["presence"] == "zero"
    assert by_name["amount"]["input_value"] == "0"
    assert by_name["amount"]["normalized_value"] == 0
    assert by_name["amount"]["business_type"] == "number"
    assert by_name["amount"]["scope"] == "商品分组"
    assert by_name["note"]["presence"] == "empty_string"
    assert by_name["note"]["operator"] == "all"


def test_request_fingerprint_distinguishes_inputs():
    definition = TemplateDefinition(
        module="query_builder",
        template_id=8,
        code="QUER-T-008",
        name="测试模板",
        scope="商品分组",
        query_text="SELECT 1 WHERE x = {x}",
        conditions=[{"variable": "x", "field": "x", "type": "number"}],
    )
    keys = {
        build_manifest(definition, params)["request_key"]
        for params in ({}, {"x": ""}, {"x": 0}, {"x": 1})
    }
    assert len(keys) == 4


def test_same_variable_different_scope_is_not_same_request():
    shared = dict(
        module="query_builder",
        code="QUER-T-009",
        name="测试模板",
        query_text="SELECT 1 WHERE x = {x}",
    )
    left = TemplateDefinition(
        template_id=1, scope="商品分组",
        conditions=[{"variable": "x", "field": "x", "type": "string"}], **shared,
    )
    right = TemplateDefinition(
        template_id=2, scope="商品明细",
        conditions=[{"variable": "x", "field": "x", "type": "string"}], **shared,
    )
    man_left = build_manifest(left, {"x": "A"})
    man_right = build_manifest(right, {"x": "A"})
    assert man_left["request_key"] != man_right["request_key"]
    issues = merge_issues([man_left, man_right])
    assert issues[0]["code"] == "PARAM_SCOPE_CONFLICT"
    assert issues[0]["scopes"] == ["商品分组", "商品明细"]

    service = fetch_module.fetch_service
    with pytest.raises(ParameterContractError) as error:
        service.execute(
            [
                {"module": "query_builder", "template_id": 1, "params": {"category": ""}},
                {"module": "query_builder", "template_id": 3, "params": {"amount": 0}},
            ],
            policy="local",
        )
    assert error.value.code == "PARAM_SCOPE_CONFLICT"
    assert error.value.status_code == 409


# ---------------------------------------------------------------------------
# 覆盖计算与含混模板
# ---------------------------------------------------------------------------


def test_data_service_coverage_uses_only_referenced_conditions():
    service = fetch_module.fetch_service
    result = service.data_service_coverage(1, {"category": ""})
    used = [item["variable"] for item in result["used_source_conditions"]]
    assert used == ["category"]
    assert result["ignored_source_conditions"][0]["variable"] == "channel"
    coverage = result["coverage"]
    assert coverage["product_groups"] == ["PG-08"]
    assert coverage["group_count"] == 1
    assert coverage["null_record_count"] == 2
    assert coverage["null_by_field"] == {"category": 2}


def test_two_real_empty_meanings_in_one_template():
    service = fetch_module.fetch_service
    result = service.execute(
        [{"module": "data_service", "template_id": 2, "params": {"category": "", "owner": ""}}],
        policy="local",
    )
    assert result["status"] == "ok"
    assert result["product_groups"] == ["PG-03", "PG-05", "PG-09"]
    assert result["null_by_field"] == {"category": 0, "owner": 3}
    assert result["null_record_count"] == 3


def test_zero_is_a_value_not_empty():
    service = fetch_module.fetch_service
    zero = service.execute(
        [{"module": "query_builder", "template_id": 3, "params": {"amount": 0, "category": "家电"}}],
        policy="local",
    )
    empty = service.execute(
        [{"module": "query_builder", "template_id": 3, "params": {"amount": "", "category": "家电"}}],
        policy="local",
    )
    missing = service.execute(
        [{"module": "query_builder", "template_id": 3, "params": {"category": "家电"}}],
        policy="local",
    )
    assert zero["records"] == 1  # 只有 R-014 同时满足金额 0 与家电
    assert zero["request_key"] != empty["request_key"] != missing["request_key"]
    assert empty["group_count"] == 4  # 空串按“不筛选”，家电全组
    assert empty["records"] == 5
    assert missing["records"] == empty["records"]


def test_ambiguous_template_is_listed_for_review():
    service = fetch_module.fetch_service
    result = service.data_service_coverage(3, {"category": ""})
    codes = [item["template_code"] for item in result["templates_pending_review"]]
    assert "DATA-0003" in codes
    assert result["coverage"]["status"] == "ok_with_ambiguity"
    assert result["coverage"]["ambiguous_variables"][0]["variable"] == "category"


# ---------------------------------------------------------------------------
# 跨服务策略比较
# ---------------------------------------------------------------------------


CROSS = [
    {"module": "query_builder", "template_id": 1, "params": {"category": ""}},
    {"module": "data_service", "template_id": 1, "params": {"category": ""}},
]


def test_compare_all_versus_defer():
    service = fetch_module.fetch_service
    result = service.compare(CROSS)
    all_groups = result["comparisons"]["all"]
    defer = result["comparisons"]["defer"]
    assert all_groups["status"] == "ok"
    assert all_groups["group_count"] == 9
    assert defer["status"] == "pending_confirmation"
    assert defer["records"] == 0
    assert {item["template_code"] for item in defer["pending_templates"]} == {
        "QUER-0001",
        "DATA-0001",
    }


def test_confirm_policy_requires_explicit_binding():
    service = fetch_module.fetch_service
    with pytest.raises(ParameterContractError) as error:
        service.execute(CROSS, policy="confirm")
    assert error.value.code == "PARAM_CONFIRMATION_REQUIRED"
    confirmed = service.execute(
        CROSS, policy="confirm", confirmations={"category": "empty"}
    )
    assert confirmed["status"] == "ok"
    assert confirmed["product_groups"] == ["PG-08"]


def test_local_policy_keeps_each_template_meaning_with_scope():
    service = fetch_module.fetch_service
    local = service.execute(CROSS, policy="local")
    assert local["status"] == "ok_with_ambiguity"
    assert local["product_groups"] == ["PG-08"]
    scopes = {item["scope"] for item in local["actual_conditions"]}
    assert scopes == {"商品分组"}
    assert all("范围" in item["annotation"] for item in local["actual_conditions"])


# ---------------------------------------------------------------------------
# 报表：定义变更、重算与交付回查
# ---------------------------------------------------------------------------


def test_report_freezes_delivery_and_recalculates_after_definition_change():
    service = fetch_module.fetch_service
    report = service.create_report(
        name="商品覆盖报表",
        templates=CROSS,
        policy="local",
    )
    delivered = service.deliver_report(report["id"])
    assert delivered["status"] == "delivered"
    delivered_groups = delivered["delivered"]["coverage"]["product_groups"]

    correction = service.update_definition(
        "data_service",
        1,
        {"conditions": [{"variable": "category", "field": "category", "type": "string", "empty_means": "all"}]},
    )
    assert correction["template"]["version"] == 2
    assert correction["template"]["history"][0]["version"] == 1
    assert correction["recalculated_reports"][0]["report_id"] == report["id"]
    assert correction["recalculated_reports"][0]["changed"] is True

    after = service.get_report(report["id"])
    assert after["delivered"]["coverage"]["product_groups"] == delivered_groups
    assert after["latest_recalculation"]["coverage"]["product_groups"] == ["PG-01", "PG-02", "PG-03", "PG-04", "PG-05", "PG-06", "PG-07", "PG-08", "PG-09"] or after["latest_recalculation"]["changed"] is True
    assert after["definition_versions_at_delivery"]["data_service:1"] == 1

    check = service.recalculate_report(report["id"])
    assert check["changed"] is True
    assert check["delivered_coverage"]["product_groups"] == delivered_groups
    assert check["current_definition_versions"]["data_service:1"] == 2


def test_report_delivery_blocked_until_confirmation():
    service = fetch_module.fetch_service
    report = service.create_report(templates=CROSS, policy="defer")
    with pytest.raises(ParameterContractError) as error:
        service.deliver_report(report["id"])
    assert error.value.code == "PARAM_CONFIRMATION_REQUIRED"

    confirm_report = service.create_report(
        templates=CROSS, policy="confirm", confirmations={"category": "empty"}
    )
    delivered = service.deliver_report(confirm_report["id"])
    assert delivered["status"] == "delivered"


def test_update_definition_rejects_stale_version():
    service = fetch_module.fetch_service
    with pytest.raises(ParameterContractError) as error:
        service.update_definition("query_builder", 1, {"expected_version": 99})
    assert error.value.code == "TEMPLATE_VERSION_CONFLICT"


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------


def test_fetch_http_endpoints(client):
    manifest = client.post(
        "/api/fetch/parameter-bindings",
        json={"values": {"module": "data_service", "template_id": 1, "params": {"category": ""}}},
    ).json()
    assert manifest["bindings"][0]["presence"] == "empty_string"

    compared = client.post(
        "/api/fetch/compare", json={"values": {"templates": CROSS}}
    ).json()
    assert set(compared["comparisons"]) == {"all", "defer"}

    error = client.post(
        "/api/fetch/coverage", json={"values": {"templates": CROSS, "policy": "confirm"}}
    )
    assert error.status_code == 409
    assert error.json()["code"] == "PARAM_CONFIRMATION_REQUIRED"

    created = client.post(
        "/api/fetch/reports", json={"values": {"templates": CROSS, "policy": "local"}}
    ).json()
    delivered = client.post(f"/api/fetch/reports/{created['id']}/deliver").json()
    assert delivered["status"] == "delivered"
    fetched = client.get(f"/api/fetch/reports/{created['id']}").json()
    assert fetched["delivered"]["coverage"]["group_count"] == 1


def test_store_is_reset_between_tests(client):
    # 上一个用例交付的报表不应该泄漏到新用例。
    assert client.get("/api/fetch/reports").json()["total"] == 0


def test_same_variable_across_templates_same_scope_is_one_request():
    """同名变量只有在范围一致时才能靠请求指纹去重。"""

    service = fetch_module.fetch_service
    same_scope = service.execute(
        [
            {"module": "query_builder", "template_id": 1, "params": {"category": "家电"}},
            {"module": "data_service", "template_id": 1, "params": {"category": "家电"}},
        ],
        policy="local",
    )
    assert same_scope["status"] == "ok"
    assert same_scope["product_groups"] == ["PG-01", "PG-04", "PG-07", "PG-09"]
    assert same_scope["request_key"]


def test_invalid_policy_and_unknown_template_codes():
    service = fetch_module.fetch_service
    with pytest.raises(ParameterContractError) as error:
        service.execute([{"module": "query_builder", "template_id": 1}], policy="maybe")
    assert error.value.code == "INVALID_POLICY"
    with pytest.raises(ParameterContractError) as missing:
        service.execute([{"module": "query_builder", "template_id": 99}])
    assert missing.value.code == "TEMPLATE_NOT_FOUND"
    assert missing.value.status_code == 404


def test_same_variable_different_scope_between_templates_is_blocked():
    """同一变量在 OLAP 模板与数据API模板里绑到不同范围时不能当成同一请求。"""

    service = fetch_module.fetch_service
    service.update_definition(
        "data_service",
        1,
        {
            "conditions": [
                {
                    "variable": "category",
                    "field": "category",
                    "type": "string",
                    "scope": "商品明细",
                    "empty_means": "empty",
                }
            ]
        },
    )
    with pytest.raises(ParameterContractError) as error:
        service.execute(
            [
                {"module": "query_builder", "template_id": 1, "params": {"category": ""}},
                {"module": "data_service", "template_id": 1, "params": {"category": ""}},
            ],
            policy="local",
        )
    assert error.value.code == "PARAM_SCOPE_CONFLICT"
    assert "商品明细" in error.value.message


def test_declared_unset_meaning_is_not_ambiguous():
    definition = TemplateDefinition(
        module="query_builder",
        template_id=12,
        code="QUER-T-012",
        name="空串视同未填",
        scope="商品分组",
        query_text="SELECT 1 WHERE x = {x}",
        conditions=[{"variable": "x", "field": "x", "type": "string", "empty_means": "unset"}],
    )
    manifest = build_manifest(definition, {"x": ""})
    binding = manifest["bindings"][0]
    assert binding["operator"] == "unset"
    assert binding["active"] is False
    assert binding["ambiguous"] is False


def test_reject_meaning_raises_stable_code():
    definition = TemplateDefinition(
        module="data_service",
        template_id=13,
        code="DATA-T-013",
        name="空串必须拒绝",
        scope="商品分组",
        query_text="SELECT 1 WHERE x = {x}",
        conditions=[{"variable": "x", "field": "x", "type": "string", "empty_means": "reject"}],
    )
    with pytest.raises(ParameterContractError) as error:
        build_manifest(definition, {"x": ""})
    assert error.value.code == "PARAM_EMPTY_REJECTED"
