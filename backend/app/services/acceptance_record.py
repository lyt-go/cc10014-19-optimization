"""验收记录领域服务：凭可追溯复检依据签署，回执交代依据并指明能否归档。

签署规则
--------
- 验收员在签署审批待办中勾选的复检，必须适用于本次验收（同工序）且能
  追溯到原缺陷；检查人/项目/工序对不上、或正在结论更正的复检不能补缺。
- 原缺陷与对应复检按「原检查编号」配对，未被复检覆盖的项目保持待处理，
  重复勾选同一复检不能抵其他检查项目。
- 签验与复检结论更正互斥：签署临界区持有结论更正闸门，只确认当前仍
  成立的依据。
- 签署回执列明依据复检、覆盖与待处理项目，并给出「可归档/暂不可归档」。
- 退回整改后记录仍可被验收员刷新找到，并附带下一次可选的复检清单。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.reinspect import CORRECTION_GUARD_KEY, ReinspectService
from app.store import store


class AcceptanceRecordService(BaseModuleService):
    MODULE = "acceptance_record"
    ENTITY = "验收报告"
    OPERATION_TERM = "开始验收"
    LIST_FIELDS = ["验收工序", "验收标准", "验收结论", "遗留问题", "整改期限", "验收人", "验收日期", "备注说明"]
    REQUIRED_FIELDS = ["验收工序", "验收标准", "验收结论"]
    STATUS_ORDER = ["待验收", "验收中", "已签署", "待整改"]
    ACTION_RULES = {"开始验收": "验收中", "签署验收": "已签署", "退回整改": "待整改"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self.reinspect_service = ReinspectService()

    # ── 前置校验：筛选并确认复检依据 ──
    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        process = entry.get("验收工序", "")
        if action == "签署验收":
            # 关联排期若已延期，需先取得变更签证
            schedules = store.query_by_field("construction_schedule", "计划工序", process)
            for s in schedules:
                if s.get("status") == "已延期":
                    return False, "关联排期已延期，需先获取变更签证再验收"

            ctx = entry.get("_action_context", {})
            reinspect_ids = list(ctx.get("reinspect_ids") or [])

            # 兼容没有复检编号的旧检查项目：沿用原「检查已通过」口径
            if not reinspect_ids:
                quality_records = store.query_by_field("quality_inspect", "检查工序", process)
                if not quality_records:
                    return False, f"工序「{process}」缺少质量检查记录"
                if not any(r.get("status") == "已通过" for r in quality_records):
                    return False, "关联质量检查未通过，无法签署验收"

            # 与复检结论更正互斥：持有闸门期间，复检结论不会被并发改写
            held = ctx.setdefault("_held_locks", [])
            if not store.acquire_resource(CORRECTION_GUARD_KEY):
                return False, "复检结论正在更正，请等待更正完成后再签署"
            held.append(CORRECTION_GUARD_KEY)
            try:
                evaluation, error = self.reinspect_service.evaluate_basis(entry_id, reinspect_ids)
                if error:
                    return False, error
                ctx["evaluation"] = evaluation
            finally:
                # 失败路径立即释放；成功路径交由 run_action 外层统一释放
                if "evaluation" not in ctx:
                    store.release_resource(CORRECTION_GUARD_KEY)
                    held.remove(CORRECTION_GUARD_KEY)
        return True, ""

    # ── 副作用：回执、归档闸门、退回可追踪 ──
    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        effects: list[dict] = []
        if action == "签署验收":
            ctx = entry.get("_action_context", {})
            evaluation = ctx.get("evaluation") or self.reinspect_service.evaluate_basis(
                entry_id, list(ctx.get("reinspect_ids") or [])
            )[0] or {}
            process = entry.get("验收工序", "")
            evidence = evaluation.get("evidence", [])
            pending_items = evaluation.get("pending_items", [])
            all_covered = bool(evaluation.get("all_covered"))
            legacy = bool(evaluation.get("legacy"))

            # 标记被采用的复检依据，便于事后追溯，并同步检查清单镜像状态
            for item in evidence:
                row = store.find("reinspect", int(item["复检id"]))
                if row is not None:
                    row["已用于验收"] = entry_id
                    self.reinspect_service._sync_quality(row)

            round_no = int(entry.get("签署轮次", 0)) + 1
            entry["签署轮次"] = round_no
            entry["签署依据"] = [
                {
                    "复检编号": item.get("复检编号", ""),
                    "原检查编号": item.get("原检查编号", ""),
                    "检查工序": item.get("检查工序", ""),
                    "检查项目": item.get("检查项目", ""),
                    "检查人": item.get("检查人", ""),
                    "复检结果": item.get("复检结果", ""),
                }
                for item in evidence
            ]
            receipt = self._build_receipt(entry_id, round_no, evaluation, legacy)
            entry["签署回执"] = receipt
            entry["可归档"] = all_covered

            # 1. 联动工序完成
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") == process:
                    ws_row["status"] = "已完成"
                    ws_row["pending"] = False
                    effects.append(dict(ws_row))

            # 2. 联动修缮日志
            for log in store.rows("restoration_log"):
                if log.get("对应工序", "") == process:
                    log["status"] = "已归档"
                    log["pending"] = False
                    effects.append(dict(log))

            # 3. 排期里程碑
            for sched in store.rows("construction_schedule"):
                if sched.get("计划工序", "") == process:
                    sched["里程碑节点"] = f"{process}-验收已签署"
                    effects.append(dict(sched))

            # 4. 归档闸门：所有原缺陷均被复检覆盖才触发档案归档
            if all_covered:
                arch_rows = store.rows("archive_mgmt")
                new_arch = {
                    "id": max((int(r.get("id", 0)) for r in arch_rows), default=0) + 1,
                    "档案名称": f"{process}-验收报告",
                    "档案类别": "验收档案",
                    "形成日期": entry.get("验收日期", ""),
                    "保管期限": "永久",
                    "密级等级": "内部",
                    "归档人": entry.get("验收人", ""),
                    "归档状态": "待整理",
                    "备注说明": f"验收签署#{entry_id}自动触发",
                    "status": "待整理",
                    "pending": True,
                    "abnormal": False,
                }
                arch_rows.append(new_arch)
                effects.append(new_arch)

            # 5. 竣工总结质量评定
            for summary in store.rows("completion_summary"):
                if summary.get("status") in ("编制中", "已提交"):
                    if summary.get("质量评定", "") == "":
                        summary["质量评定"] = f"工序{process}已验收"
                    effects.append(dict(summary))

        elif action == "退回整改":
            process = entry.get("验收工序", "")
            # 被退回记录保持待处理，验收员刷新仍能找到
            entry["pending"] = True
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") == process:
                    ws_row["abnormal"] = True
                    effects.append(dict(ws_row))
            # 附带下一次可选复检与仍待处理项目
            options = self.reinspect_service.signing_options(entry_id)
            entry["退回信息"] = {
                "待处理项目": options.get("pending_items", []),
                "下次可选复检": [
                    {
                        "复检编号": item.get("复检编号", ""),
                        "检查项目": item.get("检查项目", ""),
                        "复检结果": item.get("复检结果", ""),
                    }
                    for item in options.get("options", [])
                ],
            }
            effects.append(dict(entry))
        return effects

    # ── 回执组装 ──
    @staticmethod
    def _build_receipt(
        entry_id: int, round_no: int, evaluation: dict[str, Any], legacy: bool
    ) -> dict[str, Any]:
        evidence = evaluation.get("evidence", [])
        pending_items = evaluation.get("pending_items", [])
        all_covered = bool(evaluation.get("all_covered"))
        basis_text = "、".join(
            f"{item.get('复检编号', '')}（{item.get('检查项目', '')}）" for item in evidence
        ) or ("沿用既有合格检查记录（旧流程无复检编号）" if legacy else "无")
        if all_covered:
            conclusion = "本次验收涉及的原缺陷均已由对应复检覆盖，可据本回执继续办理归档。"
        else:
            names = "、".join(
                f"{p.get('原检查编号', '')}/{p.get('检查项目', '')}" for p in pending_items
            )
            conclusion = (
                f"尚有 {len(pending_items)} 个检查项目未被复检覆盖（{names}），"
                "暂不能办理归档；请完成对应复检并重新签署。"
            )
        return {
            "回执编号": f"RC-{entry_id}-{round_no:02d}",
            "验收工序": evaluation.get("process", ""),
            "依据复检": basis_text,
            "依据明细": evidence,
            "待处理项目": pending_items,
            "可归档": all_covered,
            "回执说明": conclusion,
        }
