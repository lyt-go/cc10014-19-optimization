"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class SignActionPayload(BaseModel):
    """验收签署动作：除动作名外携带验收员勾选的复检依据编号。"""
    action: str
    remark: str | None = None
    reinspect_ids: list[int] | None = None


class CorrectionPayload(BaseModel):
    """复检结论更正：发起时记录原因，完成时给出更正后的复检结论。"""
    更正原因: str | None = None
    复检结果: str | None = None
    remark: str | None = None
