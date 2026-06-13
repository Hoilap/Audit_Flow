import { createModal } from './modal.js'
import { parseCsv } from './csv.js'
import { escapeHtml } from './dom.js'
import { api } from './api.js'

const REVIEW_CSV_PATH = 'outputs/bank_ledger_match/matches/manual_review_candidates.csv'

/**
 * 人工复核编辑器 — 在 Modal 中渲染 manual_review_candidates.csv，
 * 支持业务人员直接编辑 bank_summary、ledger_summary、llm_reason 字段并保存。
 *
 * 布局: bank_summary 和 ledger_summary 放在更左侧（宽列），llm_reason 紧随其后。
 */

export async function openReviewEditor() {
  let data
  try {
    const resp = await api.readFile(REVIEW_CSV_PATH)
    data = resp.content || ''
  } catch (err) {
    alert(`无法加载人工复核表: ${err.message}`)
    return
  }

  const rows = parseCsv(data)
  if (rows.length < 2) {
    alert('人工复核表为空或格式不正确。')
    return
  }

  const headers = rows[0]
  const records = rows.slice(1)

  // 找到关键列索引
  const colIndex = {}
  headers.forEach((h, i) => { colIndex[h.trim()] = i })

  // 构建一个以 candidate_id 为 key 的编辑状态
  const editState = {}
  records.forEach((row, rowIdx) => {
    const cid = row[colIndex['candidate_id']] || `row_${rowIdx}`
    editState[cid] = {
      bank_summary: row[colIndex['bank_summary']] || '',
      ledger_summary: row[colIndex['ledger_summary']] || '',
      llm_reason: row[colIndex['llm_reason']] || '',
      approve: row[colIndex['approve']] || '1',
      manual_note: row[colIndex['manual_note']] || '',
      status: row[colIndex['status']] || '',
    }
  })

  const modal = createModal({
    title: `人工复核编辑 — ${records.length} 条候选记录`,
    width: '95vw',
    height: '90vh',
  })

  function renderTable() {
    const displayHeaders = [
      'approve', 'status', 'candidate_id', 'match_type',
      'bank_summary', 'ledger_summary', 'llm_reason',
      'review_reason', 'manual_note',
    ]

    // 构建表头
    const thead = `<tr>${displayHeaders.map(h => `<th>${escapeHtml(h)}</th>`).join('')}</tr>`

    // 构建表体
    const tbody = records.map((row, rowIdx) => {
      const cid = row[colIndex['candidate_id']] || `row_${rowIdx}`
      const st = editState[cid] || {}

      return `<tr data-review-row="${escapeHtml(cid)}">
        <td>
          <select class="review-approve" data-cid="${escapeHtml(cid)}">
            <option value="1" ${st.approve === '1' ? 'selected' : ''}>✓ 通过</option>
            <option value="0" ${st.approve === '0' ? 'selected' : ''}>✗ 拒绝</option>
          </select>
        </td>
        <td>
          <select class="review-status" data-cid="${escapeHtml(cid)}">
            <option value="approved_applied" ${st.status === 'approved_applied' ? 'selected' : ''}>已通过-已应用</option>
            <option value="approved_pending" ${st.status === 'approved_pending' ? 'selected' : ''}>已通过-待应用</option>
            <option value="rejected" ${st.status === 'rejected' ? 'selected' : ''}>已拒绝</option>
            <option value="pending_review" ${st.status === 'pending_review' ? 'selected' : ''}>待复核</option>
          </select>
        </td>
        <td class="cell-id">${escapeHtml(cid)}</td>
        <td>${escapeHtml(row[colIndex['match_type']] || '')}</td>
        <td class="cell-wide"><textarea class="review-ta" data-cid="${escapeHtml(cid)}" data-field="bank_summary" rows="4">${escapeHtml(st.bank_summary)}</textarea></td>
        <td class="cell-wide"><textarea class="review-ta" data-cid="${escapeHtml(cid)}" data-field="ledger_summary" rows="4">${escapeHtml(st.ledger_summary)}</textarea></td>
        <td class="cell-wide"><textarea class="review-ta" data-cid="${escapeHtml(cid)}" data-field="llm_reason" rows="4">${escapeHtml(st.llm_reason)}</textarea></td>
        <td class="cell-mid">${escapeHtml(row[colIndex['review_reason']] || '')}</td>
        <td><textarea class="review-ta" data-cid="${escapeHtml(cid)}" data-field="manual_note" rows="2">${escapeHtml(st.manual_note)}</textarea></td>
      </tr>`
    }).join('')

    modal.setBody(`
      <div class="review-toolbar">
        <span class="subtle">直接编辑 bank_summary / ledger_summary / llm_reason，修改后点击保存写回 CSV。</span>
        <div>
          <button id="review-save" class="primary">💾 保存到 CSV</button>
          <button id="review-refresh">🔄 重新加载</button>
        </div>
      </div>
      <div class="review-table-wrap">
        <table class="table review-table">
          <thead>${thead}</thead>
          <tbody>${tbody}</tbody>
        </table>
      </div>
    `)

    // 绑定 textarea 变更 → 更新 editState
    modal.getBodyEl().querySelectorAll('.review-ta').forEach(ta => {
      ta.addEventListener('input', () => {
        const cid = ta.dataset.cid
        const field = ta.dataset.field
        if (editState[cid]) {
          editState[cid][field] = ta.value
        }
      })
    })

    // 绑定 select 变更
    modal.getBodyEl().querySelectorAll('.review-approve').forEach(sel => {
      sel.addEventListener('change', () => {
        const cid = sel.dataset.cid
        if (editState[cid]) {
          editState[cid].approve = sel.value
        }
      })
    })

    modal.getBodyEl().querySelectorAll('.review-status').forEach(sel => {
      sel.addEventListener('change', () => {
        const cid = sel.dataset.cid
        if (editState[cid]) {
          editState[cid].status = sel.value
        }
      })
    })

    // 保存按钮
    modal.getBodyEl().querySelector('#review-save').addEventListener('click', () => saveReview(records, headers, colIndex, editState))

    // 刷新按钮
    modal.getBodyEl().querySelector('#review-refresh').addEventListener('click', async () => {
      modal.close()
      await openReviewEditor()
    })
  }

  renderTable()
  modal.open()
}

async function saveReview(records, headers, colIndex, editState) {
  // 将编辑后的数据重建为 CSV 字符串
  const newRows = [headers]

  records.forEach((row, rowIdx) => {
    const cid = row[colIndex['candidate_id']] || `row_${rowIdx}`
    const st = editState[cid] || {}
    const newRow = [...row]

    // 更新编辑过的字段
    if (colIndex['bank_summary'] !== undefined) newRow[colIndex['bank_summary']] = st.bank_summary
    if (colIndex['ledger_summary'] !== undefined) newRow[colIndex['ledger_summary']] = st.ledger_summary
    if (colIndex['llm_reason'] !== undefined) newRow[colIndex['llm_reason']] = st.llm_reason
    if (colIndex['approve'] !== undefined) newRow[colIndex['approve']] = st.approve
    if (colIndex['status'] !== undefined) newRow[colIndex['status']] = st.status
    if (colIndex['manual_note'] !== undefined) newRow[colIndex['manual_note']] = st.manual_note

    newRows.push(newRow)
  })

  const csvContent = newRows.map(r =>
    r.map(cell => {
      const s = String(cell ?? '')
      // 包含逗号、换行或引号时需要引用
      if (s.includes(',') || s.includes('\n') || s.includes('"') || s.includes('\r')) {
        return `"${s.replaceAll('"', '""')}"`
      }
      return s
    }).join(',')
  ).join('\n')

  try {
    await api.writeFile({
      path: REVIEW_CSV_PATH,
      content: csvContent,
      commit_message: 'manual review edit from desktop app',
    })
    alert('✅ 人工复核表已保存。')
  } catch (err) {
    alert(`❌ 保存失败: ${err.message}`)
  }
}
