"""数据备份接口：维护备份策略，覆盖提交校验、启用备份、停用备份等动作，
以及数据快照生成、批量恢复演练、过期清理、备份清单与备份策略契约的完整链路。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.backup import BackupService

router = APIRouter(prefix="/api/backup", tags=["数据备份"])

service = BackupService()

LIST_FIELDS = ["策略编号", "目标服务", "备份周期", "保留份数", "存储介质", "加密开关", "恢复演练", "过期清理"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按策略编号与状态过滤数据备份列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据备份清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "backup", "total": total, "items": items}


@router.get("/contracts/{strategy_code}", response_model=dict)
def get_contract(strategy_code: str) -> dict:
    """读取备份策略契约：数据源与数据查询模块从这里读取同一恢复点。"""
    contract = service.get_contract(strategy_code)
    if contract is None:
        raise HTTPException(status_code=404, detail=f"策略 {strategy_code} 尚未写入备份策略契约")
    return contract


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条备份策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"备份策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条备份策略，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="备份策略已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条备份策略执行提交校验、启用备份、停用备份；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/snapshots", response_model=ActionResult)
def generate_snapshot(entry_id: int, payload: EntryPayload) -> ActionResult:
    """生成数据快照：校验快照完整性，同时写入备份清单与恢复演练记录，并同步备份策略契约。"""
    manifest, message = service.generate_snapshot(entry_id, payload.values)
    if manifest is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=manifest)


@router.get("/{entry_id}/snapshots", response_model=dict)
def list_snapshots(entry_id: int) -> dict:
    """读取策略下的数据快照列表，供保留份数与完整性复核。"""
    items, message = service.list_snapshots(entry_id)
    if items is None:
        raise HTTPException(status_code=404, detail=message)
    return {"total": len(items), "items": items}


@router.post("/{entry_id}/drills", response_model=ActionResult)
def run_drill(entry_id: int, payload: EntryPayload) -> ActionResult:
    """批量恢复演练：快照批量多选、整组校验、逐条记录演练结果。"""
    summary, message = service.run_drill(entry_id, payload.values)
    if summary is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=summary)


@router.get("/{entry_id}/drills", response_model=dict)
def list_drills(entry_id: int) -> dict:
    """读取策略下的恢复演练记录，证明备份可恢复。"""
    items, message = service.list_drills(entry_id)
    if items is None:
        raise HTTPException(status_code=404, detail=message)
    return {"total": len(items), "items": items}


@router.get("/{entry_id}/manifests", response_model=dict)
def list_manifests(entry_id: int) -> dict:
    """读取策略下的备份清单，复核可恢复点与保留边界。"""
    items, message = service.list_manifests(entry_id)
    if items is None:
        raise HTTPException(status_code=404, detail=message)
    return {"total": len(items), "items": items}


@router.post("/{entry_id}/cleanup", response_model=ActionResult)
def cleanup_expired(entry_id: int) -> ActionResult:
    """过期清理：越过保留边界的旧快照先完成恢复演练再清理，演练未通过则保留。"""
    summary, message = service.cleanup_expired(entry_id)
    if summary is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=summary)


@router.get("/{entry_id}/review", response_model=dict)
def review(entry_id: int) -> dict:
    """复核视图：从备份清单与数据快照复核快照完整性、恢复演练记录与保留份数。"""
    result, message = service.review(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail=message)
    return result
