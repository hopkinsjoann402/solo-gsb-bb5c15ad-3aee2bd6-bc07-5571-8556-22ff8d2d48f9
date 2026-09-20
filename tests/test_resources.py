"""解压资源预算校验（resource_limits）测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _call(entries, **extra):
    body = {"entries": entries}
    body.update(extra)
    return client.post("/api/v1/preflight", json=body)


def _by_index(resp):
    return {e["index"]: e for e in resp["entries"]}


# ---------------------------------------------------------------------------
# 兼容性与汇总
# ---------------------------------------------------------------------------


def test_no_resource_limits_stays_compatible_and_reports_totals():
    entries = [
        {"path": "a.txt", "type": "file", "compressed_bytes": 10,
         "uncompressed_bytes": 20},
        {"path": "b.txt", "type": "file", "compressed_bytes": 5,
         "uncompressed_bytes": 7},
        {"path": "d", "type": "directory"},
        {"path": "l", "type": "symlink", "link_target": "x"},
        # 未声明大小的文件不参与求和。
        {"path": "c.txt", "type": "file"},
    ]
    resp = _call(entries).json()
    assert "repair_plan" not in resp
    by = _by_index(resp)
    assert by[1]["compressed_bytes"] == 10
    assert by[1]["uncompressed_bytes"] == 20
    # 目录/符号链接/未声明字段均回显 null。
    assert by[3]["compressed_bytes"] is None
    assert by[3]["uncompressed_bytes"] is None
    assert by[4]["compressed_bytes"] is None
    assert by[5]["compressed_bytes"] is None
    summary = resp["summary"]
    assert summary["total_compressed_bytes"] == 15
    assert summary["total_uncompressed_bytes"] == 27
    assert summary["resource_limits_exceeded"] is False


def test_empty_resource_limits_object_enables_nothing():
    r = _call(
        [{"path": "a", "type": "file"}],
        resource_limits={},
    )
    assert r.status_code == 200
    assert r.json()["summary"]["resource_limits_exceeded"] is False


def test_totals_count_only_files_with_declared_fields():
    entries = [
        {"path": "a", "type": "file", "uncompressed_bytes": 40},
        {"path": "b", "type": "file", "compressed_bytes": 3},
        {"path": "d", "type": "directory"},
        {"path": "l", "type": "symlink", "link_target": "x"},
    ]
    summary = _call(entries).json()["summary"]
    assert summary["total_uncompressed_bytes"] == 40
    assert summary["total_compressed_bytes"] == 3


# ---------------------------------------------------------------------------
# 单文件解压大小限制
# ---------------------------------------------------------------------------


def test_max_uncompressed_per_file_violation_and_boundary():
    entries = [
        {"path": "over", "type": "file", "uncompressed_bytes": 31},
        {"path": "equal", "type": "file", "uncompressed_bytes": 30},
        {"path": "under", "type": "file", "uncompressed_bytes": 29},
    ]
    resp = _call(
        entries,
        resource_limits={"max_uncompressed_bytes_per_file": 30},
    ).json()
    by = _by_index(resp)
    over_codes = [i["code"] for i in by[1]["issues"]]
    assert over_codes == ["UNCOMPRESSED_SIZE_EXCEEDED"]
    assert by[1]["issues"][0]["severity"] == "error"
    assert by[1]["safe"] is False
    assert by[2]["safe"] is True
    assert by[3]["safe"] is True
    assert resp["summary"]["unsafe"] == 1
    assert resp["summary"]["resource_limits_exceeded"] is True


def test_zero_is_a_valid_per_file_limit():
    r = _call(
        [{"path": "a", "type": "file", "uncompressed_bytes": 0}],
        resource_limits={"max_uncompressed_bytes_per_file": 0},
    )
    resp = r.json()
    assert resp["entries"][0]["safe"] is True
    assert resp["summary"]["resource_limits_exceeded"] is False


# ---------------------------------------------------------------------------
# 最大压缩比（整数交叉乘法）
# ---------------------------------------------------------------------------


def test_compression_ratio_uses_integer_cross_multiplication():
    # 上限 10:1：31 > 10*3=30 超限；30 == 10*3 恰好通过。
    entries = [
        {"path": "over", "type": "file", "compressed_bytes": 3,
         "uncompressed_bytes": 31},
        {"path": "equal", "type": "file", "compressed_bytes": 3,
         "uncompressed_bytes": 30},
    ]
    resp = _call(
        entries, resource_limits={"max_compression_ratio": 10}
    ).json()
    by = _by_index(resp)
    assert [i["code"] for i in by[1]["issues"]] == ["COMPRESSION_RATIO_EXCEEDED"]
    assert by[1]["safe"] is False
    assert by[2]["safe"] is True


def test_ratio_never_uses_floating_point_division():
    # 100/3 ≈ 33.33；上限 33:1 时 100 > 33*3=99 超限。
    # 若误用浮点四舍五入可能得到 33.33>33（仍超限）；再验 99 == 33*3 通过。
    entries = [
        {"path": "a", "type": "file", "compressed_bytes": 3,
         "uncompressed_bytes": 100},
        {"path": "b", "type": "file", "compressed_bytes": 3,
         "uncompressed_bytes": 99},
    ]
    resp = _call(
        entries, resource_limits={"max_compression_ratio": 33}
    ).json()
    by = _by_index(resp)
    assert "COMPRESSION_RATIO_EXCEEDED" in [i["code"] for i in by[1]["issues"]]
    assert by[2]["safe"] is True


def test_ratio_zero_compressed_positive_uncompressed_exceeds():
    entries = [
        {"path": "bomb", "type": "file", "compressed_bytes": 0,
         "uncompressed_bytes": 1},
    ]
    resp = _call(
        entries, resource_limits={"max_compression_ratio": 100}
    ).json()
    assert [i["code"] for i in resp["entries"][0]["issues"]] == [
        "COMPRESSION_RATIO_EXCEEDED"
    ]
    assert resp["summary"]["resource_limits_exceeded"] is True


def test_ratio_both_zero_passes():
    entries = [
        {"path": "empty", "type": "file", "compressed_bytes": 0,
         "uncompressed_bytes": 0},
    ]
    resp = _call(
        entries, resource_limits={"max_compression_ratio": 1}
    ).json()
    assert resp["entries"][0]["safe"] is True
    assert resp["entries"][0]["issues"] == []
    assert resp["summary"]["resource_limits_exceeded"] is False


def test_file_can_violate_size_and_ratio_simultaneously():
    entries = [
        {"path": "a", "type": "file", "compressed_bytes": 1,
         "uncompressed_bytes": 100},
    ]
    resp = _call(
        entries,
        resource_limits={
            "max_uncompressed_bytes_per_file": 50,
            "max_compression_ratio": 10,
        },
    ).json()
    codes = [i["code"] for i in resp["entries"][0]["issues"]]
    assert codes == ["UNCOMPRESSED_SIZE_EXCEEDED", "COMPRESSION_RATIO_EXCEEDED"]
    assert resp["entries"][0]["safe"] is False
    assert resp["summary"]["resource_limits_exceeded"] is True


# ---------------------------------------------------------------------------
# 整批总量
# ---------------------------------------------------------------------------


def test_total_uncompressed_limit_batch_only_flag():
    entries = [
        {"path": "a", "type": "file", "uncompressed_bytes": 60},
        {"path": "b", "type": "file", "uncompressed_bytes": 50},
    ]
    resp = _call(
        entries, resource_limits={"max_uncompressed_bytes_total": 100}
    ).json()
    summary = resp["summary"]
    assert summary["total_uncompressed_bytes"] == 110
    assert summary["resource_limits_exceeded"] is True
    # 整批总量是批次级状态：单个文件均未超单文件上限，不追加问题、不标 unsafe。
    assert all(e["issues"] == [] for e in resp["entries"])
    assert summary["unsafe"] == 0


def test_total_boundary_and_under_limit():
    resp = _call(
        [{"path": "a", "type": "file", "uncompressed_bytes": 100}],
        resource_limits={"max_uncompressed_bytes_total": 100},
    ).json()
    assert resp["summary"]["resource_limits_exceeded"] is False

    resp = _call(
        [{"path": "a", "type": "file", "uncompressed_bytes": 0}],
        resource_limits={"max_uncompressed_bytes_total": 0},
    ).json()
    assert resp["summary"]["resource_limits_exceeded"] is False


# ---------------------------------------------------------------------------
# 与既有分析/repair 的交互
# ---------------------------------------------------------------------------


def test_violating_files_skip_collision_and_repair():
    entries = [
        {"path": "a.txt", "type": "file", "compressed_bytes": 1,
         "uncompressed_bytes": 100},
        {"path": "A.TXT", "type": "file", "compressed_bytes": 1,
         "uncompressed_bytes": 5},
    ]
    resp = _call(
        entries,
        repair=True,
        resource_limits={"max_uncompressed_bytes_per_file": 50},
    ).json()
    # 超限文件 unsafe 后退出碰撞分组。
    assert resp["summary"]["collisions"] == 0
    plan = {e["index"]: e for e in resp["repair_plan"]["entries"]}
    assert plan[1]["skipped"] is True
    assert plan[1]["reasons"] == ["UNSAFE"]
    assert plan[1]["repaired_path"] is None
    assert plan[2]["skipped"] is False


def test_violating_file_exits_hierarchy_conflicts():
    # 超限文件 a/b 本会阻挡后代 a/b/c；unsafe 后不再计入层级冲突。
    entries = [
        {"path": "a/b", "type": "file", "compressed_bytes": 1,
         "uncompressed_bytes": 99},
        {"path": "a/b/c.txt", "type": "file", "compressed_bytes": 1,
         "uncompressed_bytes": 1},
    ]
    resp = _call(
        entries,
        resource_limits={"max_uncompressed_bytes_per_file": 50},
    ).json()
    assert resp["summary"]["hierarchy_conflicts"] == 0
    assert resp["summary"]["resource_limits_exceeded"] is True


def test_repair_false_response_has_no_repair_plan_with_limits():
    r = _call(
        [{"path": "a", "type": "file", "uncompressed_bytes": 1}],
        resource_limits={"max_uncompressed_bytes_per_file": 10},
    )
    assert "repair_plan" not in r.json()


# ---------------------------------------------------------------------------
# 422：目录/符号链接携带大小
# ---------------------------------------------------------------------------


def test_non_file_entries_with_sizes_are_422():
    for entry_type, extra in [
        ("directory", {}),
        ("symlink", {"link_target": "x"}),
    ]:
        for field in ("compressed_bytes", "uncompressed_bytes"):
            r = _call(
                [{"path": "x", "type": entry_type, field: 1, **extra}]
            )
            assert r.status_code == 422, (entry_type, field, r.text)
            loc = r.json()["detail"][0]["loc"]
            assert "body" in loc


# ---------------------------------------------------------------------------
# 422：启用限制但文件缺少所需大小
# ---------------------------------------------------------------------------


def test_enabled_per_file_limit_requires_uncompressed_bytes():
    r = _call(
        [{"path": "a", "type": "file"}],
        resource_limits={"max_uncompressed_bytes_per_file": 10},
    )
    assert r.status_code == 422
    assert "uncompressed_bytes" in r.json()["detail"][0]["msg"]


def test_enabled_total_limit_requires_uncompressed_bytes():
    r = _call(
        [{"path": "a", "type": "file"}],
        resource_limits={"max_uncompressed_bytes_total": 10},
    )
    assert r.status_code == 422
    assert "uncompressed_bytes" in r.json()["detail"][0]["msg"]


def test_enabled_ratio_requires_both_sizes():
    # 缺 compressed_bytes。
    r = _call(
        [{"path": "a", "type": "file", "uncompressed_bytes": 10}],
        resource_limits={"max_compression_ratio": 10},
    )
    assert r.status_code == 422
    assert "compressed_bytes" in r.json()["detail"][0]["msg"]
    # 缺 uncompressed_bytes。
    r = _call(
        [{"path": "a", "type": "file", "compressed_bytes": 1}],
        resource_limits={"max_compression_ratio": 10},
    )
    assert r.status_code == 422
    assert "uncompressed_bytes" in r.json()["detail"][0]["msg"]


def test_missing_size_message_identifies_entry_index():
    r = _call(
        [
            {"path": "ok", "type": "file", "uncompressed_bytes": 1},
            {"path": "bad", "type": "file"},
        ],
        resource_limits={"max_uncompressed_bytes_per_file": 10},
    )
    assert r.status_code == 422
    assert "#2" in r.json()["detail"][0]["msg"]


def test_non_file_entries_need_not_declare_sizes_when_limits_enabled():
    r = _call(
        [
            {"path": "d", "type": "directory"},
            {"path": "l", "type": "symlink", "link_target": "x"},
            {"path": "f", "type": "file", "uncompressed_bytes": 1},
        ],
        resource_limits={"max_uncompressed_bytes_per_file": 10},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 422：非法取值
# ---------------------------------------------------------------------------


def test_negative_or_wrong_typed_sizes_are_422():
    for field in ("compressed_bytes", "uncompressed_bytes"):
        r = _call([{"path": "a", "type": "file", field: -1}])
        assert r.status_code == 422
        r = _call([{"path": "a", "type": "file", field: "abc"}])
        assert r.status_code == 422


def test_invalid_limit_values_are_422():
    base_entries = [{"path": "a", "type": "file",
                     "compressed_bytes": 1, "uncompressed_bytes": 1}]
    for limits in [
        {"max_uncompressed_bytes_per_file": -1},
        {"max_uncompressed_bytes_total": -1},
        {"max_compression_ratio": 0},
        {"max_compression_ratio": -5},
    ]:
        r = _call(base_entries, resource_limits=limits)
        assert r.status_code == 422, limits
