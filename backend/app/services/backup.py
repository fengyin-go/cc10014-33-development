"""数据备份业务规则：策略状态流转、数据快照生成、备份清单、恢复演练与过期清理。

处理链路：待备份的数据快照 + 备份策略 → 校验数据快照（完整性校验）→ 同时写入
备份清单与恢复演练记录 → 同步备份策略契约（数据源与数据查询模块读取同一恢复点）。

口径冲突仲裁优先级：完整性校验 > 恢复演练 > 过期清理。保留份数到期与恢复演练
同时发生时，先完成旧快照演练再清理；演练未通过的快照清理让行，备份清单继续保留
可恢复的数据快照。

并发重复提交按（策略编号, 备份批次）/（策略编号, 演练批次）幂等，只生效一次。
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "backup"
REQUIRED_FIELDS = ["策略编号", "目标服务", "备份周期"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用备份": "已启用", "停用备份": "已停用"}
NEGATIVE_ACTIONS = ["停用备份"]

SNAPSHOT_TABLE = "backup_snapshot"
MANIFEST_TABLE = "backup_manifest"
DRILL_TABLE = "backup_drill"
CONTRACT_TABLE = "backup_contract"

# 口径冲突仲裁优先级（数字小者优先执行）：完整性校验 > 恢复演练 > 过期清理
ARBITRATION_ORDER = {"完整性校验": 0, "恢复演练": 1, "过期清理": 2}

# 链路上所有写操作都在同一把锁里完成：先校验、后落地，任何一步失败都不留半成品。
_CHAIN_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _checksum(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def read_recovery_point(strategy_code: str) -> dict[str, Any] | None:
    """备份策略契约的对外读取口径：数据源与数据查询模块读取同一恢复点。"""
    for row in store.rows(CONTRACT_TABLE):
        if row.get("策略编号") == strategy_code:
            return {
                "策略编号": row["策略编号"],
                "当前恢复点": row["当前恢复点"],
                "当前清单编号": row["当前清单编号"],
                "可恢复份数": row["可恢复份数"],
                "同步时间": row["同步时间"],
            }
    return None


class BackupService:
    # ---- 策略列表、明细、登记与启停流转 ----

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"备份策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据备份可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"备份策略已{action}"

    # ---- 链路内部口径 ----

    @staticmethod
    def _retention_of(strategy: dict[str, Any]) -> int:
        try:
            return int(str(strategy.get("保留份数") or "").strip())
        except ValueError:
            return 0

    @staticmethod
    def _snapshots(strategy_code: str) -> list[dict[str, Any]]:
        return [row for row in store.rows(SNAPSHOT_TABLE) if row.get("策略编号") == strategy_code]

    def _recoverable(self, strategy_code: str) -> list[dict[str, Any]]:
        rows = [
            row for row in self._snapshots(strategy_code)
            if row.get("状态") == "校验通过" and row.get("可恢复") == "是"
        ]
        return sorted(rows, key=lambda row: int(row.get("序号", 0)))

    @staticmethod
    def _manifests(strategy_code: str) -> list[dict[str, Any]]:
        return [row for row in store.rows(MANIFEST_TABLE) if row.get("策略编号") == strategy_code]

    @staticmethod
    def _drills(strategy_code: str) -> list[dict[str, Any]]:
        return [row for row in store.rows(DRILL_TABLE) if row.get("策略编号") == strategy_code]

    def _manifest_by_batch(self, strategy_code: str, batch: str) -> dict[str, Any] | None:
        for row in self._manifests(strategy_code):
            if row.get("备份批次") == batch:
                return row
        return None

    @staticmethod
    def _integrity_ok(snapshot: dict[str, Any]) -> bool:
        content = str(snapshot.get("内容摘要") or "")
        return bool(content) and _checksum(content) == str(snapshot.get("校验和") or "")

    def _drill_group(
        self,
        strategy: dict[str, Any],
        snapshots: list[dict[str, Any]],
        drill_batch: str,
        note: str,
    ) -> list[dict[str, Any]]:
        """整组校验、逐条记录：只构造演练记录，不改动任何已有行，由调用方统一落地。"""
        code = str(strategy.get("策略编号"))
        base = _next_id(store.rows(DRILL_TABLE))
        records: list[dict[str, Any]] = []
        for offset, snapshot in enumerate(snapshots):
            ok = snapshot.get("状态") == "校验通过" and self._integrity_ok(snapshot)
            records.append({
                "id": base + offset,
                "演练编号": f"DRIL-{base + offset:04d}",
                "策略编号": code,
                "快照编号": snapshot.get("快照编号"),
                "演练批次": drill_batch,
                "演练结果": "成功" if ok else "失败",
                "恢复点": f"{code}@{snapshot.get('备份批次')}",
                "演练时间": _now(),
                "备注": note if ok else f"{note}；完整性校验未通过",
                "status": "已记录",
                "pending": False,
                "abnormal": not ok,
            })
        return records

    def _sync_contract(
        self,
        strategy: dict[str, Any],
        manifest: dict[str, Any],
        recoverable_count: int,
    ) -> dict[str, Any]:
        """备份清单写入备份策略契约，让数据源与查询模块读取同一恢复点。"""
        rows = store.rows(CONTRACT_TABLE)
        code = str(strategy.get("策略编号"))
        contract = next((row for row in rows if row.get("策略编号") == code), None)
        if contract is None:
            contract = {
                "id": _next_id(rows),
                "策略编号": code,
                "status": "已同步",
                "pending": False,
                "abnormal": False,
            }
            rows.append(contract)
        contract.update({
            "当前恢复点": manifest["恢复点"],
            "当前清单编号": manifest["清单编号"],
            "可恢复份数": recoverable_count,
            "同步时间": _now(),
        })
        return contract

    # ---- 链路动作：生成数据快照 ----

    def generate_snapshot(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        strategy = store.find(MODULE, entry_id)
        if strategy is None:
            return None, f"备份策略 {entry_id} 不存在或已归档"
        code = str(strategy.get("策略编号"))
        if strategy.get("status") != "已启用":
            return None, f"备份策略 {code} 当前状态为「{strategy.get('status')}」，仅已启用策略可生成数据快照"
        batch = str(values.get("备份批次") or "").strip()
        content = str(values.get("快照内容") or "").strip()
        if not batch:
            return None, "缺少必填字段：备份批次"
        retention = self._retention_of(strategy)
        if retention <= 0:
            return None, f"备份策略 {code} 的保留份数需为正整数，当前无法形成保留边界"
        if not content:
            return None, "缺少必填字段：快照内容（用于生成校验和做完整性校验）"
        with _CHAIN_LOCK:
            # 幂等闸口：并发重复提交只生效一次
            existing = self._manifest_by_batch(code, batch)
            if existing is not None:
                return existing, f"备份批次 {batch} 已生成备份清单 {existing['清单编号']}，并发重复提交只生效一次"
            snapshot_rows = store.rows(SNAPSHOT_TABLE)
            drill_rows = store.rows(DRILL_TABLE)
            manifest_rows = store.rows(MANIFEST_TABLE)
            now = _now()
            # 1) 完整性校验优先：先构造待入册快照并校验，失败则整体不落地
            snap_id = _next_id(snapshot_rows)
            snapshot = {
                "id": snap_id,
                "快照编号": f"SNAP-{snap_id:04d}",
                "策略编号": code,
                "目标服务": strategy.get("目标服务"),
                "备份批次": batch,
                "序号": len(self._snapshots(code)) + 1,
                "内容摘要": content,
                "校验和": _checksum(content),
                "大小KB": max(1, len(content.encode("utf-8")) // 1024),
                "状态": "校验通过",
                "可恢复": "是",
                "生成时间": now,
                "清理时间": "",
                "status": "校验通过",
                "pending": False,
                "abnormal": False,
            }
            if not self._integrity_ok(snapshot):
                return None, "数据快照完整性校验未通过，本次快照、备份清单与恢复演练记录均未写入"
            # 2) 保留边界仲裁：生成新快照将越过保留份数时，先完成旧快照恢复演练，再过期清理
            recoverable = self._recoverable(code)
            drills: list[dict[str, Any]] = []
            cleaned: list[dict[str, Any]] = []
            notes: list[str] = []
            if len(recoverable) >= retention:
                expiring = recoverable[: len(recoverable) - retention + 1]
                drills = self._drill_group(strategy, expiring, f"AUTO-{batch}", "保留边界到期前演练")
                for snap, drill in zip(expiring, drills):
                    if drill["演练结果"] == "成功":
                        cleaned.append(snap)
                    else:
                        notes.append(f"快照 {snap['快照编号']} 恢复演练未通过，过期清理让行，清单继续保留该可恢复点")
            # 3) 同时保存：数据快照 + 备份清单 + 恢复演练记录 + 备份策略契约
            kept = [snap for snap in recoverable if snap not in cleaned] + [snapshot]
            man_id = _next_id(manifest_rows)
            manifest = {
                "id": man_id,
                "清单编号": f"MANI-{man_id:04d}",
                "策略编号": code,
                "备份批次": batch,
                "快照份数": len(kept),
                "快照编号列表": "、".join(str(snap["快照编号"]) for snap in kept),
                "完整性结论": "通过",
                "恢复点": f"{code}@{kept[-1]['备份批次']}",
                "保留份数": retention,
                "清理快照": "、".join(str(snap["快照编号"]) for snap in cleaned),
                "仲裁说明": "；".join(notes),
                "写入时间": now,
                "status": "已生效",
                "pending": False,
                "abnormal": bool(notes),
            }
            for snap in cleaned:
                snap["状态"] = "已清理"
                snap["可恢复"] = "否"
                snap["清理时间"] = now
            snapshot_rows.append(snapshot)
            drill_rows.extend(drills)
            manifest_rows.append(manifest)
            self._sync_contract(strategy, manifest, len(kept))
            message = f"数据快照 {snapshot['快照编号']} 已生成并写入备份清单 {manifest['清单编号']}"
            if cleaned:
                message += f"；已先完成恢复演练并过期清理 {len(cleaned)} 份旧快照"
            if notes:
                message += "；" + "；".join(notes)
            return manifest, message

    # ---- 链路动作：批量恢复演练（批量多选、整组校验、逐条记录）----

    def run_drill(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        strategy = store.find(MODULE, entry_id)
        if strategy is None:
            return None, f"备份策略 {entry_id} 不存在或已归档"
        code = str(strategy.get("策略编号"))
        raw_codes = values.get("快照编号列表")
        if not isinstance(raw_codes, list) or not raw_codes:
            return None, "请先批量多选要演练的数据快照，再提交恢复演练"
        codes = sorted({str(item).strip() for item in raw_codes if str(item).strip()})
        drill_batch = str(values.get("演练批次") or "").strip()
        if not drill_batch:
            digest = hashlib.sha1("、".join(codes).encode("utf-8")).hexdigest()[:8]
            drill_batch = f"MANUAL-{_today()}-{digest}"
        with _CHAIN_LOCK:
            existing = [row for row in self._drills(code) if row.get("演练批次") == drill_batch]
            if existing:
                summary = self._drill_summary(drill_batch, existing)
                return summary, f"演练批次 {drill_batch} 已记录 {len(existing)} 条演练结果，并发重复提交只生效一次"
            snapshots = self._snapshots(code)
            by_code = {str(row.get("快照编号")): row for row in snapshots}
            unknown = [item for item in codes if item not in by_code]
            if unknown:
                return None, f"整组校验未通过：快照 {'、'.join(unknown)} 不存在或不属于策略 {code}，本组演练未记录"
            selected = [by_code[item] for item in codes]
            records = self._drill_group(strategy, selected, drill_batch, "批量多选恢复演练")
            store.rows(DRILL_TABLE).extend(records)
            summary = self._drill_summary(drill_batch, records)
            return summary, f"恢复演练完成：成功 {summary['成功']} 条、失败 {summary['失败']} 条，已逐条记录"

    @staticmethod
    def _drill_summary(drill_batch: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        success = sum(1 for row in records if row.get("演练结果") == "成功")
        return {
            "演练批次": drill_batch,
            "成功": success,
            "失败": len(records) - success,
            "记录": records,
        }

    # ---- 链路动作：过期清理 ----

    def cleanup_expired(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        strategy = store.find(MODULE, entry_id)
        if strategy is None:
            return None, f"备份策略 {entry_id} 不存在或已归档"
        code = str(strategy.get("策略编号"))
        retention = self._retention_of(strategy)
        if retention <= 0:
            return None, f"备份策略 {code} 的保留份数需为正整数，当前无法形成保留边界"
        with _CHAIN_LOCK:
            recoverable = self._recoverable(code)
            excess = len(recoverable) - retention
            if excess <= 0:
                summary = {"清理份数": 0, "保留份数": retention, "可恢复份数": len(recoverable)}
                return summary, f"策略 {code} 可恢复 {len(recoverable)} 份，未越过保留边界 {retention} 份，无需清理"
            expiring = recoverable[:excess]
            manifest_rows = store.rows(MANIFEST_TABLE)
            now = _now()
            # 仲裁：恢复演练优先于过期清理，演练未通过的快照继续保留
            drills = self._drill_group(strategy, expiring, f"CLEAN-{_next_id(manifest_rows):04d}", "过期清理前演练")
            cleaned: list[dict[str, Any]] = []
            notes: list[str] = []
            for snap, drill in zip(expiring, drills):
                if drill["演练结果"] == "成功":
                    cleaned.append(snap)
                else:
                    notes.append(f"快照 {snap['快照编号']} 恢复演练未通过，过期清理让行，清单继续保留该可恢复点")
            for snap in cleaned:
                snap["状态"] = "已清理"
                snap["可恢复"] = "否"
                snap["清理时间"] = now
            store.rows(DRILL_TABLE).extend(drills)
            kept = [snap for snap in recoverable if snap not in cleaned]
            if cleaned:
                man_id = _next_id(manifest_rows)
                manifest = {
                    "id": man_id,
                    "清单编号": f"MANI-{man_id:04d}",
                    "策略编号": code,
                    "备份批次": f"CLEAN-{man_id:04d}",
                    "快照份数": len(kept),
                    "快照编号列表": "、".join(str(snap["快照编号"]) for snap in kept),
                    "完整性结论": "通过",
                    "恢复点": f"{code}@{kept[-1]['备份批次']}",
                    "保留份数": retention,
                    "清理快照": "、".join(str(snap["快照编号"]) for snap in cleaned),
                    "仲裁说明": "；".join(notes),
                    "写入时间": now,
                    "status": "已生效",
                    "pending": False,
                    "abnormal": bool(notes),
                }
                manifest_rows.append(manifest)
                self._sync_contract(strategy, manifest, len(kept))
            summary = {"清理份数": len(cleaned), "保留份数": retention, "可恢复份数": len(kept)}
            message = f"过期清理完成：清理 {len(cleaned)} 份，清单仍保留 {len(kept)} 个可恢复点"
            if notes:
                message += "；" + "；".join(notes)
            return summary, message

    # ---- 链路查询：快照、清单、演练记录、契约与复核 ----

    def list_snapshots(self, entry_id: int) -> tuple[list[dict[str, Any]] | None, str]:
        strategy = store.find(MODULE, entry_id)
        if strategy is None:
            return None, f"备份策略 {entry_id} 不存在或已归档"
        return self._snapshots(str(strategy.get("策略编号"))), "ok"

    def list_manifests(self, entry_id: int) -> tuple[list[dict[str, Any]] | None, str]:
        strategy = store.find(MODULE, entry_id)
        if strategy is None:
            return None, f"备份策略 {entry_id} 不存在或已归档"
        return self._manifests(str(strategy.get("策略编号"))), "ok"

    def list_drills(self, entry_id: int) -> tuple[list[dict[str, Any]] | None, str]:
        strategy = store.find(MODULE, entry_id)
        if strategy is None:
            return None, f"备份策略 {entry_id} 不存在或已归档"
        return self._drills(str(strategy.get("策略编号"))), "ok"

    def get_contract(self, strategy_code: str) -> dict[str, Any] | None:
        return read_recovery_point(strategy_code)

    def review(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """复核视图：从备份清单与数据快照复核快照完整性、恢复演练记录与保留份数。"""
        strategy = store.find(MODULE, entry_id)
        if strategy is None:
            return None, f"备份策略 {entry_id} 不存在或已归档"
        code = str(strategy.get("策略编号"))
        snapshots = self._snapshots(code)
        manifests = self._manifests(code)
        drills = self._drills(code)
        contract = read_recovery_point(code)
        retention = self._retention_of(strategy)
        recoverable = [row for row in self._recoverable(code)]
        checks: list[dict[str, str]] = []

        broken = [str(row["快照编号"]) for row in snapshots if row.get("状态") != "已清理" and not self._integrity_ok(row)]
        checks.append({
            "项目": "快照完整性",
            "结论": "未通过" if broken else "通过",
            "说明": f"快照 {'、'.join(broken)} 校验和不一致" if broken else f"{len(snapshots)} 份快照校验和全部一致",
        })

        cleaned = [row for row in snapshots if row.get("状态") == "已清理"]
        undrilled = [
            str(row["快照编号"]) for row in cleaned
            if not any(d.get("快照编号") == row["快照编号"] and d.get("演练结果") == "成功" for d in drills)
        ]
        checks.append({
            "项目": "恢复演练记录",
            "结论": "未通过" if undrilled else "通过",
            "说明": (
                f"已清理快照 {'、'.join(undrilled)} 缺少成功演练记录，无法证明备份可恢复"
                if undrilled else f"已记录 {len(drills)} 条演练结果，已清理快照均先完成演练"
            ),
        })

        excess = len(recoverable) - retention
        if excess <= 0:
            checks.append({
                "项目": "保留份数",
                "结论": "通过",
                "说明": f"可恢复 {len(recoverable)} 份，保留边界 {retention} 份",
            })
        else:
            retained = recoverable[:excess]
            unjustified = [
                str(row["快照编号"]) for row in retained
                if not any(d.get("快照编号") == row["快照编号"] and d.get("演练结果") == "失败" for d in drills)
            ]
            checks.append({
                "项目": "保留份数",
                "结论": "未通过" if unjustified else "通过",
                "说明": (
                    f"可恢复 {len(recoverable)} 份超出保留边界 {retention} 份，快照 {'、'.join(unjustified)} 缺少演练未通过记录"
                    if unjustified
                    else f"可恢复 {len(recoverable)} 份超出保留边界 {retention} 份：{excess} 份演练未通过，过期清理让行，清单保留可恢复点"
                ),
            })

        if manifests:
            latest = manifests[-1]
            listed = {item for item in str(latest.get("快照编号列表") or "").split("、") if item}
            current = {str(row["快照编号"]) for row in recoverable}
            same = listed == current
            checks.append({
                "项目": "清单与快照一致",
                "结论": "通过" if same else "未通过",
                "说明": (
                    f"最新清单 {latest['清单编号']} 覆盖 {len(listed)} 个可恢复点，与数据快照一致"
                    if same else f"最新清单 {latest['清单编号']} 的可恢复点与数据快照不一致：清单 {sorted(listed)}，快照 {sorted(current)}"
                ),
            })
        else:
            checks.append({
                "项目": "清单与快照一致",
                "结论": "通过" if not recoverable else "未通过",
                "说明": "尚未生成备份清单" if not recoverable else "存在可恢复快照但缺少备份清单",
            })

        if contract and manifests:
            same = contract["当前恢复点"] == manifests[-1]["恢复点"]
            checks.append({
                "项目": "契约恢复点一致",
                "结论": "通过" if same else "未通过",
                "说明": (
                    f"备份策略契约恢复点 {contract['当前恢复点']} 与最新清单一致"
                    if same else f"契约恢复点 {contract['当前恢复点']} 与最新清单 {manifests[-1]['恢复点']} 不一致"
                ),
            })
        else:
            checks.append({
                "项目": "契约恢复点一致",
                "结论": "通过" if not manifests else "未通过",
                "说明": "备份策略契约尚未同步" if not manifests else "已有备份清单但备份策略契约未同步",
            })

        passed = all(item["结论"] == "通过" for item in checks)
        return {
            "策略编号": code,
            "备份周期": strategy.get("备份周期"),
            "保留份数": retention,
            "可恢复份数": len(recoverable),
            "快照列表": snapshots,
            "清单列表": manifests,
            "演练记录": drills,
            "契约": contract,
            "复核项": checks,
            "复核结论": "通过" if passed else "未通过",
        }, "复核完成"
