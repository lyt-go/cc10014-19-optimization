"""复检筛选与验收签署专项测试。

覆盖业务要求：
- 从验收记录定位同工序、同检查项目可签署的复检，编号在检查清单与签署间一致；
- 区分原缺陷与对应复检，未覆盖项目保持待处理，人/项目/工序对不上的通过记录不能补缺；
- 签验与复检结论更正并发互斥，只确认适用且可追溯的依据；
- 回执交代依据并指明能否归档；退回后刷新仍能找到记录和下一次可选复检；
- 两条榫卯复检 + 更正中的承重复检发起签署：重复榫卯不能抵承重；
  承重更正完成可归档；不再满足则复检承重，已生效榫卯依据仍可继续使用。
"""
from __future__ import annotations

import concurrent.futures

import pytest

from app.seed import SEED_ROWS
from app.services.acceptance_record import AcceptanceRecordService
from app.services.reinspect import CORRECTION_GUARD_KEY, ReinspectService
from app.store import store


@pytest.fixture(autouse=True)
def reset_store():
    store._idempotent_log.clear()
    store._resource_locks.clear()
    for name in list(store._tables.keys()):
        if name in SEED_ROWS:
            store._tables[name] = [dict(row) for row in SEED_ROWS[name]]
        else:
            store._tables[name] = []
    yield
    store._resource_locks.clear()


reinspect_service = ReinspectService()
acceptance_service = AcceptanceRecordService()

# 大殿木作待办验收（seed）
ACCEPTANCE_ID = 4
# 复检 id：1/2 榫卯（对应 QC-0004），3 承重（更正中，QC-0005），
# 4 检查人不符，5 检查项目不符，6 检查工序不符
FJ_SUNMAO_1, FJ_SUNMAO_2, FJ_CHENGZHONG = 1, 2, 3
FJ_WRONG_INSPECTOR, FJ_WRONG_ITEM, FJ_WRONG_PROCESS = 4, 5, 6


def test_signing_options_locates_same_process_items_and_marks_unavailable():
    data = reinspect_service.signing_options(ACCEPTANCE_ID)
    assert data["验收工序"] == "大殿木作"
    option_ids = {o["复检id"] for o in data["options"]}
    # 只有两条合格榫卯复检可用于签署
    assert option_ids == {FJ_SUNMAO_1, FJ_SUNMAO_2}
    unavailable = {u["复检id"]: u["不可用原因"] for u in data["unavailable"]}
    assert FJ_CHENGZHONG in unavailable and "更正" in unavailable[FJ_CHENGZHONG]
    assert FJ_WRONG_INSPECTOR in unavailable and "检查人" in unavailable[FJ_WRONG_INSPECTOR]
    assert FJ_WRONG_ITEM in unavailable and "检查项目" in unavailable[FJ_WRONG_ITEM]
    # 工序对不上的记录不会进入同工序候选；直接评估时必须被拒绝
    _, wrong_process_msg = reinspect_service.evaluate_basis(ACCEPTANCE_ID, [FJ_WRONG_PROCESS])
    assert "工序" in wrong_process_msg
    # 两条原缺陷，承重尚未被覆盖
    pending = {d["检查项目"] for d in data["pending_items"]}
    assert pending == {"承重梁挠度"}


def test_reinspect_no_consistent_between_checklist_and_signing():
    options = {o["复检id"]: o["复检编号"] for o in reinspect_service.signing_options(ACCEPTANCE_ID)["options"]}
    for rid, no in options.items():
        quality = next(q for q in store.rows("quality_inspect") if q.get("_reinspect_id") == rid)
        assert quality["复检编号"] == no
        assert quality["检查编号"] == no  # 检查清单与签署审批编号一致


def test_sign_with_two_sunmao_plus_correcting_chengzhong_rejected():
    # 更正中的承重复检不能作为依据
    entry, msg = acceptance_service.run_action(
        ACCEPTANCE_ID, "签署验收",
        {"reinspect_ids": [FJ_SUNMAO_1, FJ_SUNMAO_2, FJ_CHENGZHONG]},
    )
    assert entry is None
    assert "更正" in msg
    # 闸门已释放，未泄漏
    assert CORRECTION_GUARD_KEY not in store._resource_locks
    assert store.find("acceptance_record", ACCEPTANCE_ID)["status"] == "待验收"


def test_duplicate_sunmao_cannot_cover_chengzhong():
    # 只勾选两条榫卯（含重复勾选），不能抵承重缺陷
    entry, msg = acceptance_service.run_action(
        ACCEPTANCE_ID, "签署验收",
        {"reinspect_ids": [FJ_SUNMAO_1, FJ_SUNMAO_1]},
    )
    assert entry is None
    assert "重复勾选" in msg

    # 两条不同榫卯可受理，但承重仍待处理 → 暂不可归档
    entry, msg = acceptance_service.run_action(
        ACCEPTANCE_ID, "签署验收",
        {"reinspect_ids": [FJ_SUNMAO_1, FJ_SUNMAO_2]},
    )
    assert entry is not None, msg
    receipt = entry["签署回执"]
    assert receipt["可归档"] is False
    assert any(p["检查项目"] == "承重梁挠度" for p in receipt["待处理项目"])
    assert "暂不能办理归档" in receipt["回执说明"]
    # 未触发档案归档
    assert not any(a.get("档案名称") == "大殿木作-验收报告" for a in store.rows("archive_mgmt"))


def test_wrong_inspector_item_process_pass_records_cannot_fill_gap():
    for rid, keyword in (
        (FJ_WRONG_INSPECTOR, "检查人"),
        (FJ_WRONG_ITEM, "检查项目"),
        (FJ_WRONG_PROCESS, "工序"),
    ):
        entry, msg = acceptance_service.run_action(
            ACCEPTANCE_ID, "签署验收", {"reinspect_ids": [rid]}
        )
        assert entry is None
        assert keyword in msg


def test_correction_complete_then_sign_covers_all_and_archive_allowed():
    # 完成承重结论更正（更正后仍合格）
    row, msg = reinspect_service.complete_correction(FJ_CHENGZHONG, "复检合格，挠度复测达标")
    assert row is not None and row["status"] == "已通过", msg

    entry, msg = acceptance_service.run_action(
        ACCEPTANCE_ID, "签署验收",
        {"reinspect_ids": [FJ_SUNMAO_1, FJ_SUNMAO_2, FJ_CHENGZHONG]},
    )
    assert entry is not None, msg
    receipt = entry["签署回执"]
    assert receipt["可归档"] is True
    assert receipt["待处理项目"] == []
    assert "可据本回执继续办理归档" in receipt["回执说明"]
    # 触发档案归档
    assert any(a.get("档案名称") == "大殿木作-验收报告" for a in store.rows("archive_mgmt"))
    # 复检被标记为已用于该验收
    assert store.find("reinspect", FJ_CHENGZHONG).get("已用于验收") == ACCEPTANCE_ID


def test_correction_to_unqualified_keeps_pending_and_sunmao_evidence_reusable():
    # 每次签署都会推进验收状态，单独建一条验收记录，避免与其他用例状态串扰
    fresh = acceptance_service.create_entry({
        "验收工序": "大殿木作", "验收标准": "GB50165-木作", "验收结论": "待签署",
    })[0]
    fresh_id = fresh["id"]

    # 承重更正后不再满足（完成更正但结论为不合格 → 退回需整改）
    row, _ = reinspect_service.complete_correction(FJ_CHENGZHONG, "复检不合格，挠度仍超限")
    # complete_correction 默认置已通过；不合格场景应走退回整改
    row2, msg = reinspect_service.run_action(FJ_CHENGZHONG, "退回整改")
    assert row2 is not None and row2["status"] == "需整改", msg

    # 已生效的榫卯依据仍可继续使用，但承重待处理、不可归档
    entry, msg = acceptance_service.run_action(
        fresh_id, "签署验收", {"reinspect_ids": [FJ_SUNMAO_1, FJ_SUNMAO_2]}
    )
    assert entry is not None, msg
    assert entry["可归档"] is False
    assert any(p["检查项目"] == "承重梁挠度" for p in entry["签署回执"]["待处理项目"])

    # 重新发起一条合格承重复检即可补齐
    new_row, missing = reinspect_service.create_entry({
        "原检查编号": "QC-0005",
        "检查工序": "大殿木作",
        "检查项目": "承重梁挠度",
        "技术标准": "GB50165-木作",
        "检查人": "张监理",
        "检查日期": "2026-10-18",
        "复检结果": "整改后复检合格",
    })
    assert missing == []
    new_id = new_row["id"]
    reinspect_service.run_action(new_id, "开始复检")
    reinspect_service.run_action(new_id, "出具复检报告")

    # 新一轮验收（原验收已签署，新建一条）据榫卯 + 新承重复检完成覆盖
    fresh2 = acceptance_service.create_entry({
        "验收工序": "大殿木作", "验收标准": "GB50165-木作", "验收结论": "待签署",
    })[0]
    entry2, msg = acceptance_service.run_action(
        fresh2["id"], "签署验收",
        {"reinspect_ids": [FJ_SUNMAO_1, FJ_SUNMAO_2, new_id]},
    )
    assert entry2 is not None, msg
    assert entry2["签署回执"]["可归档"] is True


def test_return_keeps_record_refreshable_with_next_options():
    # 先退回整改
    entry, msg = acceptance_service.run_action(ACCEPTANCE_ID, "退回整改")
    assert entry is not None and entry["status"] == "待整改", msg
    assert entry["pending"] is True
    info = entry["退回信息"]
    # 下一次可选复检仍含两条榫卯；承重仍在待处理
    option_nos = {o["复检编号"] for o in info["下次可选复检"]}
    assert {"FJ-0001", "FJ-0002"}.issubset(option_nos)
    assert any(p["检查项目"] == "承重梁挠度" for p in info["待处理项目"])

    # 验收员刷新：仍能定位该退回记录及候选
    refreshed = reinspect_service.signing_options(ACCEPTANCE_ID)
    assert refreshed["acceptance_id"] == ACCEPTANCE_ID
    assert {o["复检id"] for o in refreshed["options"]} == {FJ_SUNMAO_1, FJ_SUNMAO_2}


def test_sign_and_correction_are_mutually_exclusive():
    # 确定性不变量：复检进入「更正中」后，任何并发/串行签署都不能确认它
    row, msg = reinspect_service.begin_correction(FJ_SUNMAO_1, "结论复核")
    assert row is not None and row["status"] == "更正中", msg

    sign_msgs: list[tuple[bool, str]] = []

    def sign(acceptance_id: int) -> None:
        entry, message = acceptance_service.run_action(
            acceptance_id, "签署验收", {"reinspect_ids": [FJ_SUNMAO_1, FJ_SUNMAO_2]}
        )
        sign_msgs.append((entry is not None, message))

    # 多个验收单并发签署，勾选依据都含更正中的复检，必须全部被拒
    fresh_ids = [
        acceptance_service.create_entry({
            "验收工序": "大殿木作", "验收标准": "x", "验收结论": "t",
        })[0]["id"]
        for _ in range(4)
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(sign, fresh_ids))

    assert sign_msgs and all(not ok and "更正" in m for ok, m in sign_msgs)
    for aid in fresh_ids:
        assert store.find("acceptance_record", aid)["status"] != "已签署"
    # 临界区结束后闸门释放，系统仍可继续工作
    assert CORRECTION_GUARD_KEY not in store._resource_locks

    # 完成更正后恢复可签署（承重仍未覆盖时受理但不可归档）
    reinspect_service.complete_correction(FJ_SUNMAO_1, "复核合格")
    entry, _ = acceptance_service.run_action(
        fresh_ids[0], "签署验收", {"reinspect_ids": [FJ_SUNMAO_1, FJ_SUNMAO_2]}
    )
    assert entry is not None and entry["status"] == "已签署"


def test_create_reinspect_requires_traceable_defect_and_triple_match():
    # 追溯不到原缺陷
    _, missing = reinspect_service.create_entry({
        "原检查编号": "QC-9999",
        "检查工序": "大殿木作", "检查项目": "榫卯缝隙", "技术标准": "GB50165-木作",
    })
    assert missing  # 返回错误信息

    # 对一条非缺陷（已通过）记录不能发起复检
    row, missing = reinspect_service.create_entry({
        "原检查编号": "FJ-0001",
        "检查工序": "大殿木作", "检查项目": "榫卯缝隙", "技术标准": "GB50165-木作",
    })
    assert row is None and missing

    # 检查项目对不上不能发起
    row, missing = reinspect_service.create_entry({
        "原检查编号": "QC-0004",
        "检查工序": "大殿木作", "检查项目": "承重梁挠度", "技术标准": "GB50165-木作",
        "检查人": "张监理",
    })
    assert row is None and any("检查项目" in m for m in missing)


def test_legacy_quality_flow_still_supported_without_reinspect_no():
    # 旧检查项目（无复检编号）仍走原「检查已通过」兼容口径
    entry = acceptance_service.create_entry({
        "验收工序": "旧工序", "验收标准": "STD", "验收结论": "合格",
    })[0]
    # 无任何质量记录 → 拒绝
    res, msg = acceptance_service.run_action(entry["id"], "签署验收", {"reinspect_ids": []})
    assert res is None and "质量检查记录" in msg
