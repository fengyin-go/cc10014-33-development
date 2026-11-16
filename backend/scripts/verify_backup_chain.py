"""数据备份链路复测脚本：按验收标准逐项复核，全部通过才退出 0。

复测口径：
1. 处理链路：待备份数据快照 + 备份策略 → 校验数据快照 → 同时写入备份清单与恢复
   演练记录 → 同步备份策略契约；
2. 验收标准：快照完整性、恢复演练记录、保留份数可复核，可从备份清单与数据快照复核；
3. 口径冲突仲裁：完整性校验 > 恢复演练 > 过期清理；保留份数到期与恢复演练同时
   发生时，清单仍需保留可恢复的数据快照；
4. 并发重复提交只生效一次；
5. BAK-0002 周期 daily、保留 3 份：生成第四份前先完成旧快照演练，清理后仍保留
   三个可恢复点。

运行：cd backend && .venv/bin/python scripts/verify_backup_chain.py
"""
from __future__ import annotations

import hashlib
import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services.backup import BackupService, read_recovery_point  # noqa: E402
from app.services.datasource import DatasourceService  # noqa: E402
from app.services.query import QueryService  # noqa: E402
from app.store import store  # noqa: E402

svc = BackupService()
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "✓" if condition else "✗"
    line = f"{mark} {name}" + (f" —— {detail}" if detail and not condition else "")
    print(line)
    if not condition:
        FAILURES.append(name)


def strategy(code: str) -> dict:
    return next(row for row in store.rows("backup") if row["策略编号"] == code)


def snapshots(code: str) -> list[dict]:
    return [row for row in store.rows("backup_snapshot") if row["策略编号"] == code]


def recoverable(code: str) -> list[dict]:
    return [row for row in snapshots(code) if row["状态"] == "校验通过" and row["可恢复"] == "是"]


def manifests(code: str) -> list[dict]:
    return [row for row in store.rows("backup_manifest") if row["策略编号"] == code]


def drills(code: str) -> list[dict]:
    return [row for row in store.rows("backup_drill") if row["策略编号"] == code]


def table_counts() -> dict[str, int]:
    return {
        name: len(store.rows(name))
        for name in ("backup_snapshot", "backup_manifest", "backup_drill", "backup_contract")
    }


BAK1 = strategy("BAK-0001")  # 草稿
BAK2 = strategy("BAK-0002")  # 已启用，daily，保留 3 份
BAK3 = strategy("BAK-0003")  # 已启用，daily，保留 2 份

print("== 1. 链路前置：仅已启用策略可生成快照，完整性校验失败整体不落地 ==")
manifest, message = svc.generate_snapshot(BAK1["id"], {"备份批次": "2026-11-13", "快照内容": "草稿策略数据"})
check("草稿状态策略被拒绝生成快照", manifest is None, message)

before = table_counts()
manifest, message = svc.generate_snapshot(BAK2["id"], {"备份批次": "2026-11-13", "快照内容": ""})
check("空快照内容被完整性校验拦下", manifest is None, message)
check("校验失败后快照/清单/演练/契约均未写入（同时保存，缺一不留）", table_counts() == before, str(table_counts()))

print("== 2. BAK-0002（daily，保留 3 份）依次生成三份快照 ==")
for batch in ("2026-11-13", "2026-11-14", "2026-11-15"):
    manifest, message = svc.generate_snapshot(BAK2["id"], {"备份批次": batch, "快照内容": f"订单服务全量数据-{batch}"})
    check(f"批次 {batch} 生成成功", manifest is not None, message)
check("当前可恢复快照为 3 份", len(recoverable("BAK-0002")) == 3, f"实际 {len(recoverable('BAK-0002'))}")
check("已写入 3 份备份清单", len(manifests("BAK-0002")) == 3)
contract = read_recovery_point("BAK-0002")
check("契约恢复点指向最新批次", bool(contract) and contract["当前恢复点"] == "BAK-0002@2026-11-15", str(contract))

print("== 3. 并发重复提交只生效一次 ==")
results: list[tuple[dict | None, str]] = []


def submit(batch: str) -> None:
    results.append(svc.generate_snapshot(BAK2["id"], {"备份批次": batch, "快照内容": f"订单服务全量数据-{batch}"}))


threads = [threading.Thread(target=submit, args=("2026-11-15",)) for _ in range(2)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
check("重复提交已存在批次两次均返回成功", all(item[0] is not None for item in results))
check("已存在批次不产生新清单", len(manifests("BAK-0002")) == 3, f"实际 {len(manifests('BAK-0002'))}")
check("两次返回的是同一份清单", len({item[0]["清单编号"] for item in results if item[0]}) == 1)

print("== 4. 生成第四份：先完成旧快照演练，清理后仍保留三个可恢复点 ==")
results.clear()
threads = [threading.Thread(target=submit, args=("2026-11-16",)) for _ in range(2)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
batch4_manifests = [row for row in manifests("BAK-0002") if row["备份批次"] == "2026-11-16"]
batch4_snapshots = [row for row in snapshots("BAK-0002") if row["备份批次"] == "2026-11-16"]
check("并发提交第四份只生成一份清单", len(batch4_manifests) == 1, f"实际 {len(batch4_manifests)}")
check("并发提交第四份只生成一份快照", len(batch4_snapshots) == 1, f"实际 {len(batch4_snapshots)}")

snap1 = next(row for row in snapshots("BAK-0002") if row["备份批次"] == "2026-11-13")
check("最旧快照已过期清理", snap1["状态"] == "已清理" and snap1["可恢复"] == "否")
auto_drills = [row for row in drills("BAK-0002") if row["快照编号"] == snap1["快照编号"] and row["演练结果"] == "成功"]
check("清理前已先完成旧快照演练并逐条记录", len(auto_drills) >= 1, f"演练记录 {len(auto_drills)} 条")
kept = recoverable("BAK-0002")
check("清理后仍保留三个可恢复点", len(kept) == 3, f"实际 {len(kept)}")
check(
    "保留的是最近三个批次",
    [row["备份批次"] for row in kept] == ["2026-11-14", "2026-11-15", "2026-11-16"],
    str([row["备份批次"] for row in kept]),
)
latest = manifests("BAK-0002")[-1]
check("最新清单覆盖 3 个可恢复点", latest["快照份数"] == 3, latest["快照编号列表"])
contract = read_recovery_point("BAK-0002")
check("契约恢复点同步到第四份", bool(contract) and contract["当前恢复点"] == "BAK-0002@2026-11-16", str(contract))

print("== 5. 恢复点按批量多选整组校验、逐条记录演练结果 ==")
codes = [row["快照编号"] for row in kept[:2]]
summary, message = svc.run_drill(BAK2["id"], {"快照编号列表": codes, "演练批次": "REHEARSE-2026-11-16"})
check("批量多选演练成功", summary is not None and summary["成功"] == 2 and summary["失败"] == 0, message)
check("逐条生成 2 条演练记录", len([row for row in drills("BAK-0002") if row["演练批次"] == "REHEARSE-2026-11-16"]) == 2)
drill_count = len(drills("BAK-0002"))
summary, message = svc.run_drill(BAK2["id"], {"快照编号列表": codes, "演练批次": "REHEARSE-2026-11-16"})
check("同批次重复演练提交被忽略", summary is not None and len(drills("BAK-0002")) == drill_count, message)
summary, message = svc.run_drill(BAK2["id"], {"快照编号列表": ["SNAP-9999"], "演练批次": "REHEARSE-X"})
check("整组校验拦截不存在的快照", summary is None, message)

print("== 6. 复核：快照完整性、恢复演练记录、保留份数均可复核 ==")
review, _ = svc.review(BAK2["id"])
check("BAK-0002 复核结论通过", bool(review) and review["复核结论"] == "通过", str(review and review["复核项"]))
check("复核项覆盖完整性/演练/保留份数", bool(review) and {item["项目"] for item in review["复核项"]} >= {"快照完整性", "恢复演练记录", "保留份数"})
check("保留份数口径为 3", bool(review) and review["保留份数"] == 3 and review["可恢复份数"] == 3)

print("== 7. 数据源与数据查询模块读取同一恢复点 ==")
point_ds = DatasourceService().recovery_point("BAK-0002")
point_qy = QueryService().recovery_point("BAK-0002")
check(
    "两个模块读到同一恢复点",
    bool(point_ds) and bool(point_qy) and point_ds["当前恢复点"] == point_qy["当前恢复点"] == "BAK-0002@2026-11-16",
    f"datasource={point_ds} query={point_qy}",
)

print("== 8. 仲裁：保留到期与恢复演练冲突时，清单仍保留可恢复快照 ==")
svc.generate_snapshot(BAK3["id"], {"备份批次": "2026-11-15", "快照内容": "影像数据-2026-11-15"})
svc.generate_snapshot(BAK3["id"], {"备份批次": "2026-11-16", "快照内容": "影像数据-2026-11-16"})
check("BAK-0003 已有 2 个可恢复点", len(recoverable("BAK-0003")) == 2)
old = next(row for row in snapshots("BAK-0003") if row["备份批次"] == "2026-11-15")
origin_checksum = old["校验和"]
old["校验和"] = hashlib.sha256(b"tampered").hexdigest()  # 模拟旧快照数据损坏
manifest, message = svc.generate_snapshot(BAK3["id"], {"备份批次": "2026-11-17", "快照内容": "影像数据-2026-11-17"})
check("损坏快照演练未通过时仍生成新快照", manifest is not None, message)
check("演练未通过的旧快照清理让行、继续保留", old["状态"] == "校验通过" and old["可恢复"] == "是", message)
check("可恢复点暂时超出保留边界（3 > 2）", len(recoverable("BAK-0003")) == 3, f"实际 {len(recoverable('BAK-0003'))}")
failed = [row for row in drills("BAK-0003") if row["快照编号"] == old["快照编号"] and row["演练结果"] == "失败"]
check("失败演练已逐条记录", len(failed) >= 1)
review3, _ = svc.review(BAK3["id"])
retention_check = next(item for item in review3["复核项"] if item["项目"] == "保留份数")
check("复核说明清理让行原因", retention_check["结论"] == "通过" and "让行" in retention_check["说明"], retention_check["说明"])

old["校验和"] = origin_checksum  # 修复数据后重新清理
summary, message = svc.cleanup_expired(BAK3["id"])
check("修复后过期清理完成", summary is not None and summary["清理份数"] == 1, message)
check("清理后回到保留边界 2 份", len(recoverable("BAK-0003")) == 2, f"实际 {len(recoverable('BAK-0003'))}")
check("被清理快照留有成功演练记录", any(
    row["快照编号"] == old["快照编号"] and row["演练结果"] == "成功" for row in drills("BAK-0003")
))
review3, _ = svc.review(BAK3["id"])
check("BAK-0003 复核结论通过", bool(review3) and review3["复核结论"] == "通过", str(review3 and review3["复核项"]))

print()
if FAILURES:
    print(f"复测未通过：{len(FAILURES)} 项 —— {'、'.join(FAILURES)}")
    sys.exit(1)
print("全部复测项通过：快照完整性、恢复演练记录、保留份数均可从备份清单与数据快照复核。")
