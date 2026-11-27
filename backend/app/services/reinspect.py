"""复检领域服务：复检与原缺陷的追溯、签署依据筛选、复检结论更正并发控制。

设计要点
--------
- 复检必须能追溯到一条原缺陷（初次检查的需整改记录），通过
  「原检查编号」关联，复检编号 ``FJ-xxxx`` 在检查清单与签署审批间保持一致。
- 只有「同工序、同检查项目、同检查人」且能追到原缺陷的复检通过记录，
  才能作为该缺陷的签署依据；对不上的通过记录不能补缺。
- 复检结论更正（发起更正 -> 更正中）期间，该复检不得再被选作依据；
  更正完成后再决定能否使用。
- 未被复检覆盖的原缺陷始终保持待处理，直到对应复检通过。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 模块级结论更正并发闸门：同一时刻只允许一条复检处于「更正中」临界区，
# 与验收签署互斥，保证签验与复检结论更正并发时只能确认当前仍成立的依据。
CORRECTION_GUARD_KEY = "reinspect:conclusion_guard"

# 结论更正期间不可作为签署依据的状态
CORRECTING_STATUS = "更正中"
# 可作为签署依据的复检状态
EVIDENCE_STATUS = "已通过"
# 原缺陷（初次检查不合格）状态
DEFECT_STATUS = "需整改"


class ReinspectService(BaseModuleService):
    MODULE = "reinspect"
    ENTITY = "复检记录"
    OPERATION_TERM = "开始复检"
    LIST_FIELDS = [
        "复检编号", "复检类型", "原检查编号", "检查工序", "检查项目", "技术标准",
        "检查人", "检查日期", "复检结果", "对应原缺陷", "备注说明",
    ]
    REQUIRED_FIELDS = ["检查工序", "检查项目", "技术标准", "原检查编号"]
    STATUS_ORDER = ["待复检", "复检中", "已通过", "需整改", "更正中"]
    ACTION_RULES = {
        "开始复检": "复检中",
        "出具复检报告": "已通过",
        "退回整改": "需整改",
        "发起更正": "更正中",
        "完成更正": "已通过",
    }
    NEGATIVE_ACTIONS = ["退回整改"]

    # ── 编号工具 ──
    @staticmethod
    def next_inspect_no() -> str:
        """生成下一条检查编号 QC-xxxx（兼容已有检查项目，旧记录缺号时回填）。"""
        existing = [
            int(str(r.get("检查编号", "")).removeprefix("QC-"))
            for r in store.rows("quality_inspect")
            if str(r.get("检查编号", "")).startswith("QC-")
            and str(r.get("检查编号", "")).removeprefix("QC-").isdigit()
        ]
        return f"QC-{(max(existing, default=0) + 1):04d}"

    def next_reinspect_no(self) -> str:
        """生成下一条复检编号 FJ-xxxx，检查清单与签署审批共用此编号。"""
        existing = [
            int(str(r.get("复检编号", "")).removeprefix("FJ-"))
            for r in store.rows(self.MODULE)
            if str(r.get("复检编号", "")).startswith("FJ-")
            and str(r.get("复检编号", "")).removeprefix("FJ-").isdigit()
        ]
        return f"FJ-{(max(existing, default=0) + 1):04d}"

    @staticmethod
    def ensure_inspect_no(quality_row: dict[str, Any]) -> str:
        """确保检查清单上的记录有检查编号；兼容没有编号的旧检查项目。"""
        no = str(quality_row.get("检查编号", "")).strip()
        if no:
            return no
        no = ReinspectService.next_inspect_no()
        quality_row["检查编号"] = no
        return no

    # ── 登记：复检必须能追溯到原缺陷 ──
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing

        original_no = str(values.get("原检查编号", "")).strip()
        original = self._find_quality_by_no(original_no)
        if original is None:
            return None, [f"原检查编号「{original_no}」不存在，无法追溯复检来源"]
        if str(original.get("status", "")) != DEFECT_STATUS:
            return None, [f"原检查「{original_no}」不是需整改缺陷，不能对其发起复检"]

        # 同工序、同检查项目、同检查人才能对原缺陷发起复检
        mismatch = self._mismatch_reason(original, values)
        if mismatch:
            return None, [mismatch]

        rows = store.rows(self.MODULE)
        entry: dict[str, Any] = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "复检编号": self.next_reinspect_no(),
            "复检类型": str(values.get("复检类型", "缺陷复检")).strip() or "缺陷复检",
            "原检查编号": original_no,
            "检查工序": original.get("检查工序", ""),
            "检查项目": original.get("检查项目", ""),
            "技术标准": original.get("技术标准", ""),
            "检查人": original.get("检查人", ""),
            "检查日期": values.get("检查日期", ""),
            "复检结果": values.get("复检结果", ""),
            "对应原缺陷": self._defect_label(original),
            "备注说明": values.get("备注说明", ""),
            "status": self.STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
            "_更正前结论": "",
        }
        rows.append(entry)

        # 同步镜像到质量检查清单，保证两边复检编号一致、状态随复检流转
        self._mirror_to_quality(entry)
        return entry, []

    # ── 动作：状态流转并同步检查清单 ──
    def run_action(
        self,
        entry_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action, context)
        if entry is not None:
            self._sync_quality(entry)
        return entry, message

    # ── 结论更正：与验收签署互斥的并发临界区 ──
    def _apply_status(self, entry: dict[str, Any], target: str, message: str) -> dict[str, Any]:
        """直接驱动复检状态并同步检查清单（结论更正不走动作幂等窗口）。"""
        entry["status"] = target
        # 仅「已通过」视为办结，其余（含更正中、需整改）保持待处理
        entry["pending"] = target != EVIDENCE_STATUS
        entry["abnormal"] = target in ("需整改", CORRECTING_STATUS)
        self._sync_quality(entry)
        return entry

    def begin_correction(self, entry_id: int, reason: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if not store.acquire_resource(CORRECTION_GUARD_KEY):
            return None, "已有复检正在更正结论，请等待其完成后再发起"
        try:
            if str(entry.get("status", "")) == CORRECTING_STATUS:
                return None, "该复检已在结论更正中，请勿重复发起"
            entry["_更正前结论"] = entry.get("复检结果", "")
            entry["更正原因"] = reason
            updated = self._apply_status(entry, CORRECTING_STATUS, "")
            return updated, "复检结论已进入更正中，更正完成前不能作为签署依据"
        finally:
            store.release_resource(CORRECTION_GUARD_KEY)

    def complete_correction(
        self, entry_id: int, new_result: str
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if str(entry.get("status", "")) != CORRECTING_STATUS:
            return None, "该复检不在更正中，无需完成更正"
        if not str(new_result or "").strip():
            return None, "请填写更正后的复检结论"
        if not store.acquire_resource(CORRECTION_GUARD_KEY):
            return None, "结论更正资源忙，请稍后重试"
        try:
            entry["复检结果"] = new_result.strip()
            updated = self._apply_status(entry, EVIDENCE_STATUS, "")
            return updated, "复检结论更正完成"
        finally:
            store.release_resource(CORRECTION_GUARD_KEY)

    # ── 签署依据候选：从验收记录定位同工序、同检查项目可用于签署的复检 ──
    def signing_options(self, acceptance_id: int) -> dict[str, Any]:
        acceptance = store.find("acceptance_record", acceptance_id)
        if acceptance is None:
            return {"acceptance_id": acceptance_id, "options": [], "defects": [],
                    "pending_items": [], "unavailable": []}
        process = str(acceptance.get("验收工序", ""))

        defects: list[dict[str, Any]] = []
        for q in self._original_defects(process):
            defects.append({
                "原检查编号": self.ensure_inspect_no(q),
                "检查工序": q.get("检查工序", ""),
                "检查项目": q.get("检查项目", ""),
                "检查人": q.get("检查人", ""),
                "缺陷": q.get("不合格项", ""),
                "待处理": not self._is_defect_covered(q),
            })

        options: list[dict[str, Any]] = []
        unavailable: list[dict[str, Any]] = []
        for r in store.rows(self.MODULE):
            if str(r.get("检查工序", "")) != process:
                continue
            reason = self._ineligible_reason(r, process)
            payload = {
                "复检编号": r.get("复检编号", ""),
                "复检id": r.get("id"),
                "原检查编号": r.get("原检查编号", ""),
                "检查工序": r.get("检查工序", ""),
                "检查项目": r.get("检查项目", ""),
                "检查人": r.get("检查人", ""),
                "检查日期": r.get("检查日期", ""),
                "复检结果": r.get("复检结果", ""),
                "status": r.get("status", ""),
            }
            if reason:
                payload["不可用原因"] = reason
                unavailable.append(payload)
            else:
                payload["可覆盖缺陷"] = r.get("对应原缺陷", "")
                options.append(payload)

        pending_items = [d for d in defects if d["待处理"]]
        return {
            "acceptance_id": acceptance_id,
            "验收工序": process,
            "options": options,
            "unavailable": unavailable,
            "defects": defects,
            "pending_items": pending_items,
        }

    def evaluate_basis(
        self, acceptance_id: int, reinspect_ids: list[int]
    ) -> tuple[dict[str, Any] | None, str]:
        """校验验收员勾选的复检依据，返回依据评估结果。

        只确认适用于本次验收（同工序）且能追到原缺陷的依据；
        检查人/项目/工序对不上、或正在更正的通过记录不能补缺。
        """
        acceptance = store.find("acceptance_record", acceptance_id)
        if acceptance is None:
            return None, f"验收报告 {acceptance_id} 不存在"
        process = str(acceptance.get("验收工序", ""))

        if not reinspect_ids:
            # 兼容无复检编号的旧流程：没有勾选时按原有「检查已通过」口径
            return self._legacy_evaluation(process), ""

        seen: set[int] = set()
        evidence: list[dict[str, Any]] = []
        for rid in reinspect_ids:
            if rid in seen:
                return None, f"复检依据重复勾选：复检 {rid} 被重复选择，重复勾选不能抵其他检查项目"
            seen.add(rid)
            row = store.find(self.MODULE, rid)
            if row is None:
                return None, f"复检 {rid} 不存在，不能作为签署依据"
            if str(row.get("检查工序", "")) != process:
                return None, (
                    f"复检「{row.get('复检编号', rid)}」的检查工序「{row.get('检查工序', '')}」"
                    f"与本次验收工序「{process}」不一致，不能用于本次验收"
                )
            reason = self._ineligible_reason(row, process)
            if reason:
                return None, f"复检「{row.get('复检编号', rid)}」{reason}"
            evidence.append({
                "复检id": rid,
                "复检编号": row.get("复检编号", ""),
                "原检查编号": row.get("原检查编号", ""),
                "检查工序": row.get("检查工序", ""),
                "检查项目": row.get("检查项目", ""),
                "检查人": row.get("检查人", ""),
                "复检结果": row.get("复检结果", ""),
                "对应原缺陷": row.get("对应原缺陷", ""),
            })

        defects, pending_items = self._coverage(process, evidence)
        return {
            "process": process,
            "evidence": evidence,
            "defects": defects,
            "pending_items": pending_items,
            "all_covered": not pending_items,
        }, ""

    # ── 内部：缺陷与复检匹配 ──
    def _coverage(
        self, process: str, evidence: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """计算本次依据覆盖了哪些原缺陷，哪些仍待处理。"""
        defects: list[dict[str, Any]] = []
        covered_nos = {str(e.get("原检查编号", "")) for e in evidence}
        for q in self._original_defects(process):
            no = self.ensure_inspect_no(q)
            covered = no in covered_nos
            defects.append({
                "原检查编号": no,
                "检查项目": q.get("检查项目", ""),
                "检查人": q.get("检查人", ""),
                "缺陷": q.get("不合格项", ""),
                "已覆盖": covered,
                "待处理": not covered,
            })
        pending = [d for d in defects if d["待处理"]]
        return defects, pending

    def _original_defects(self, process: str) -> list[dict[str, Any]]:
        """返回某工序的原缺陷（初次检查需整改），排除复检镜像记录。"""
        return [
            q for q in store.rows("quality_inspect")
            if str(q.get("检查工序", "")) == process
            and str(q.get("status", "")) == DEFECT_STATUS
            and not q.get("复检编号")
        ]

    def _is_defect_covered(self, quality_row: dict[str, Any]) -> bool:
        no = self.ensure_inspect_no(quality_row)
        for r in store.rows(self.MODULE):
            if str(r.get("原检查编号", "")) != no:
                continue
            # 不可用原因返回空串表示可作为依据
            if not self._ineligible_reason(r, str(quality_row.get("检查工序", ""))):
                return True
        return False

    def _ineligible_reason(self, reinspect_row: dict[str, Any], process: str) -> str:
        """返回复检不能作为签署依据的原因；可用时返回空串。"""
        status = str(reinspect_row.get("status", ""))
        if status == CORRECTING_STATUS:
            return "复检结论正在更正，更正完成前不能作为签署依据"
        if status != EVIDENCE_STATUS:
            return f"复检状态为「{status}」，只有已通过的复检才能作为依据"
        if str(reinspect_row.get("检查工序", "")) != process:
            return "检查工序与本次验收不一致"
        original = self._find_quality_by_no(str(reinspect_row.get("原检查编号", "")))
        if original is None:
            return "追溯不到对应的原缺陷，不能补缺"
        mismatch = self._mismatch_reason(original, reinspect_row)
        if mismatch:
            return mismatch
        return ""

    @staticmethod
    def _mismatch_reason(
        original: dict[str, Any], candidate: dict[str, Any]
    ) -> str:
        """检查人、项目或工序对不上即返回原因，否则空串。"""
        for label, key in (("检查工序", "检查工序"), ("检查项目", "检查项目"), ("检查人", "检查人")):
            if str(original.get(key, "")) != str(candidate.get(key, "")):
                return (
                    f"{label}对不上：原缺陷为「{original.get(key, '')}」，"
                    f"复检为「{candidate.get(key, '')}」，该通过记录不能补缺"
                )
        return ""

    def _find_quality_by_no(self, inspect_no: str) -> dict[str, Any] | None:
        """按检查编号定位原检查记录；优先返回初次检查（非复检镜像）。"""
        fallback = None
        for q in store.rows("quality_inspect"):
            if str(q.get("检查编号", "")).strip() != inspect_no:
                continue
            if not q.get("复检编号"):
                return q
            if fallback is None:
                fallback = q
        return fallback

    @staticmethod
    def _defect_label(quality_row: dict[str, Any]) -> str:
        return (
            f"{quality_row.get('检查工序', '')}/"
            f"{quality_row.get('检查项目', '')}/"
            f"{quality_row.get('不合格项', '')}"
        )

    def _legacy_evaluation(self, process: str) -> dict[str, Any]:
        """无复检编号的旧检查项目：沿用按工序「检查已通过」的兼容口径。"""
        quality = [
            q for q in store.rows("quality_inspect")
            if str(q.get("检查工序", "")) == process
        ]
        defects = self._original_defects(process)
        passed = any(
            str(q.get("status", "")) == EVIDENCE_STATUS and not q.get("复检编号")
            for q in quality
        )
        pending = [
            {
                "原检查编号": self.ensure_inspect_no(q),
                "检查项目": q.get("检查项目", ""),
                "检查人": q.get("检查人", ""),
                "缺陷": q.get("不合格项", ""),
                "已覆盖": False,
                "待处理": True,
            }
            for q in defects
        ]
        return {
            "process": process,
            "evidence": [],
            "defects": [
                {
                    "原检查编号": self.ensure_inspect_no(q),
                    "检查项目": q.get("检查项目", ""),
                    "检查人": q.get("检查人", ""),
                    "缺陷": q.get("不合格项", ""),
                    "已覆盖": False,
                    "待处理": True,
                }
                for q in defects
            ],
            "pending_items": pending,
            "all_covered": passed and not pending,
            "legacy": True,
        }

    # ── 内部：检查清单镜像，保证复检编号一致 ──
    def _mirror_to_quality(self, entry: dict[str, Any]) -> dict[str, Any]:
        quality_rows = store.rows("quality_inspect")
        mirrored = {
            "id": max((int(r.get("id", 0)) for r in quality_rows), default=0) + 1,
            "检查编号": entry.get("复检编号", ""),
            "复检编号": entry.get("复检编号", ""),
            "复检类型": entry.get("复检类型", ""),
            "原检查编号": entry.get("原检查编号", ""),
            "检查工序": entry.get("检查工序", ""),
            "检查项目": entry.get("检查项目", ""),
            "技术标准": entry.get("技术标准", ""),
            "检查结果": entry.get("复检结果", ""),
            "不合格项": "",
            "检查人": entry.get("检查人", ""),
            "检查日期": entry.get("检查日期", ""),
            "备注说明": entry.get("备注说明", ""),
            "status": entry.get("status", ""),
            "pending": entry.get("pending", True),
            "abnormal": entry.get("abnormal", False),
            "_reinspect_id": entry.get("id"),
        }
        quality_rows.append(mirrored)
        return mirrored

    def _sync_quality(self, entry: dict[str, Any]) -> None:
        """复检状态/结论流转后，同步检查清单中的镜像记录。"""
        for q in store.rows("quality_inspect"):
            if q.get("_reinspect_id") == entry.get("id") or \
                    str(q.get("复检编号", "")) == str(entry.get("复检编号", "")):
                q["status"] = entry.get("status", q.get("status"))
                q["pending"] = entry.get("pending", q.get("pending"))
                q["abnormal"] = entry.get("abnormal", q.get("abnormal"))
                q["检查结果"] = entry.get("复检结果", q.get("检查结果"))
                q["复检编号"] = entry.get("复检编号", q.get("复检编号"))
