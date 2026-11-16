import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/backup'
const columns = [
  "策略编号",
  "目标服务",
  "备份周期",
  "保留份数",
  "存储介质",
  "加密开关",
  "恢复演练",
  "过期清理"
]
const actions = [
  "提交校验",
  "启用备份",
  "停用备份"
]
const statuses = [
  "草稿",
  "待校验",
  "已启用",
  "已停用"
]
const stats = [
  {
    "label": "备份策略总数",
    "value": 0
  },
  {
    "label": "已启用策略",
    "value": 0
  },
  {
    "label": "待校验策略",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type ReviewCheck = {
  项目: string
  结论: string
  说明: string
}

type ReviewData = {
  策略编号: string
  备份周期: string | number | null
  保留份数: number
  可恢复份数: number
  快照列表: Row[]
  清单列表: Row[]
  演练记录: Row[]
  契约: Row | null
  复核项: ReviewCheck[]
  复核结论: string
}

type ActionReply = {
  ok: boolean
  message: string
}

export default function BackupView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const [review, setReview] = useState<ReviewData | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [notice, setNotice] = useState('')
  const [batch, setBatch] = useState(() => new Date().toISOString().slice(0, 10))
  const [content, setContent] = useState('')
  const [drillBatch, setDrillBatch] = useState('')
  const [checked, setChecked] = useState<string[]>([])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('备份策略列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据备份列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const loadReview = useCallback(async (entryId: number) => {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${entryId}/review`)
      if (!response.ok) {
        throw new Error('备份复核数据读取失败')
      }
      const payload = (await response.json()) as ReviewData
      setReview(payload)
      setSelectedId(entryId)
      setChecked([])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '备份复核数据读取失败')
    }
  }, [])

  async function postAction(path: string, values: Record<string, unknown>, fallback: string) {
    setNotice('')
    setErrorMessage('')
    try {
      const response = await request(path, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      const payload = (await response.json()) as ActionReply
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || fallback)
      }
      setNotice(payload.message)
      return true
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : fallback)
      return false
    }
  }

  async function runAction(action: string, row: Row) {
    const ok = await postAction(`${ENDPOINT}/${row.id}/actions`, { action }, '数据备份操作失败')
    if (ok) {
      await reload()
    }
  }

  async function generateSnapshot() {
    if (selectedId === null) return
    const ok = await postAction(
      `${ENDPOINT}/${selectedId}/snapshots`,
      { 备份批次: batch, 快照内容: content },
      '数据快照生成失败',
    )
    if (ok) {
      setContent('')
      await loadReview(selectedId)
    }
  }

  async function runDrill() {
    if (selectedId === null || checked.length === 0) return
    const ok = await postAction(
      `${ENDPOINT}/${selectedId}/drills`,
      { 快照编号列表: checked, 演练批次: drillBatch },
      '恢复演练提交失败',
    )
    if (ok) {
      setChecked([])
      await loadReview(selectedId)
    }
  }

  async function runCleanup() {
    if (selectedId === null) return
    const ok = await postAction(`${ENDPOINT}/${selectedId}/cleanup`, {}, '过期清理执行失败')
    if (ok) {
      await loadReview(selectedId)
    }
  }

  function toggleCheck(code: string) {
    setChecked((prev) => (prev.includes(code) ? prev.filter((item) => item !== code) : [...prev, code]))
  }

  return (
    <section className="page" data-module="backup">
      <header className="page-head">
        <div>
          <h2>数据备份管理</h2>
          <p className="page-desc">维护数据备份策略的输入、执行约束与结果记录，为数据备份提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('备份策略登记入口尚未接入审批流')}>
            登记备份策略
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据备份清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void loadReview(Number(row.id))}>
                  复核
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无数据备份数据，可先登记备份策略</td>
            </tr>
          )}
        </tbody>
      </table>

      {review && selectedId !== null && (
        <section className="review-panel">
          <header className="page-head">
            <div>
              <h3>备份复核：{review.策略编号}（周期 {review.备份周期 ?? '—'}，保留 {review.保留份数} 份，当前可恢复 {review.可恢复份数} 份）</h3>
              <p className="page-desc">
                复核结论：{review.复核结论}
                {review.契约 ? `；备份策略契约恢复点 ${review.契约['当前恢复点']}（数据源与数据查询模块读取同一恢复点）` : '；备份策略契约尚未同步'}
              </p>
            </div>
            <div className="page-actions">
              <button className="btn" type="button" onClick={() => void runCleanup()}>过期清理</button>
              <button className="btn ghost" type="button" onClick={() => { setReview(null); setSelectedId(null) }}>收起</button>
            </div>
          </header>

          <table className="data-table">
            <thead>
              <tr><th>复核项</th><th>结论</th><th>说明</th></tr>
            </thead>
            <tbody>
              {review.复核项.map((item) => (
                <tr key={item.项目}>
                  <td>{item.项目}</td>
                  <td>{item.结论}</td>
                  <td>{item.说明}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void generateSnapshot() }}>
            <label className="filter-item">
              <span>备份批次</span>
              <input value={batch} onChange={(event) => setBatch(event.target.value)} placeholder="如 2026-11-16" />
            </label>
            <label className="filter-item">
              <span>快照内容</span>
              <input value={content} onChange={(event) => setContent(event.target.value)} placeholder="用于完整性校验的数据摘要" />
            </label>
            <button className="btn primary" type="submit">生成数据快照</button>
          </form>

          <h4>数据快照（批量多选后可提交恢复演练）</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>选择</th><th>快照编号</th><th>备份批次</th><th>状态</th><th>可恢复</th><th>校验和</th><th>生成时间</th><th>清理时间</th>
              </tr>
            </thead>
            <tbody>
              {review.快照列表.map((row) => {
                const code = String(row['快照编号'] ?? '')
                const cleaned = row['状态'] === '已清理'
                return (
                  <tr key={code}>
                    <td>
                      <input
                        type="checkbox"
                        disabled={cleaned}
                        checked={checked.includes(code)}
                        onChange={() => toggleCheck(code)}
                      />
                    </td>
                    <td>{code}</td>
                    <td>{row['备份批次'] ?? '—'}</td>
                    <td>{row['状态'] ?? '—'}</td>
                    <td>{row['可恢复'] ?? '—'}</td>
                    <td>{String(row['校验和'] ?? '').slice(0, 16)}…</td>
                    <td>{row['生成时间'] ?? '—'}</td>
                    <td>{row['清理时间'] || '—'}</td>
                  </tr>
                )
              })}
              {review.快照列表.length === 0 && (
                <tr><td className="empty-state" colSpan={8}>暂无数据快照，可先生成数据快照</td></tr>
              )}
            </tbody>
          </table>

          <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void runDrill() }}>
            <label className="filter-item">
              <span>演练批次（留空自动生成）</span>
              <input value={drillBatch} onChange={(event) => setDrillBatch(event.target.value)} placeholder="如 REHEARSE-2026-11-16" />
            </label>
            <button className="btn" type="submit" disabled={checked.length === 0}>
              批量恢复演练（已选 {checked.length} 份）
            </button>
          </form>

          <h4>备份清单</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>清单编号</th><th>备份批次</th><th>快照份数</th><th>可恢复快照</th><th>恢复点</th><th>清理快照</th><th>仲裁说明</th><th>写入时间</th>
              </tr>
            </thead>
            <tbody>
              {review.清单列表.map((row) => (
                <tr key={String(row['清单编号'])}>
                  <td>{row['清单编号'] ?? '—'}</td>
                  <td>{row['备份批次'] ?? '—'}</td>
                  <td>{row['快照份数'] ?? '—'}</td>
                  <td>{row['快照编号列表'] ?? '—'}</td>
                  <td>{row['恢复点'] ?? '—'}</td>
                  <td>{row['清理快照'] || '—'}</td>
                  <td>{row['仲裁说明'] || '—'}</td>
                  <td>{row['写入时间'] ?? '—'}</td>
                </tr>
              ))}
              {review.清单列表.length === 0 && (
                <tr><td className="empty-state" colSpan={8}>暂无备份清单</td></tr>
              )}
            </tbody>
          </table>

          <h4>恢复演练记录</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>演练编号</th><th>快照编号</th><th>演练批次</th><th>演练结果</th><th>恢复点</th><th>演练时间</th><th>备注</th>
              </tr>
            </thead>
            <tbody>
              {review.演练记录.map((row) => (
                <tr key={String(row['演练编号'])}>
                  <td>{row['演练编号'] ?? '—'}</td>
                  <td>{row['快照编号'] ?? '—'}</td>
                  <td>{row['演练批次'] ?? '—'}</td>
                  <td>{row['演练结果'] ?? '—'}</td>
                  <td>{row['恢复点'] ?? '—'}</td>
                  <td>{row['演练时间'] ?? '—'}</td>
                  <td>{row['备注'] ?? '—'}</td>
                </tr>
              ))}
              {review.演练记录.length === 0 && (
                <tr><td className="empty-state" colSpan={7}>暂无恢复演练记录</td></tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条数据备份记录</span>
        {notice && <span>{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
