import { createModal } from './modal.js'
import { parseCsv } from './csv.js'
import { escapeHtml } from './dom.js'
import { api } from './api.js'

/**
 * 人工复核编辑器 — 在 Modal 中渲染指定的复核 CSV 文件，
 * 支持业务人员直接编辑 bank_summary、ledger_summary、llm_reason 字段并保存。
 *
 * 布局: bank_summary 和 ledger_summary 放在更左侧（宽列），llm_reason 紧随其后。
 *
 * @param {string} filePath - 要编辑的复核 CSV 文件路径
 */

export async function openReviewEditor(filePath) {
  if (!filePath) {
    alert('未指定复核文件路径。')
    return
  }
  let data
  try {
    const resp = await api.readFile(filePath)
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
    // 直接使用 approve 列（已由 _write_candidates 写入 LLM 决定）
    const approveVal = (row[colIndex['approve']] || '').trim()
    
    editState[cid] = {
      bank_summary: row[colIndex['bank_summary']] || '',
      ledger_summary: row[colIndex['ledger_summary']] || '',
      llm_reason: row[colIndex['llm_reason']] || '',
      approve: approveVal,
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
      'approve', 'candidate_id', 'match_type',
      'bank_summary', 'ledger_summary', 'llm_reason',
      'review_reason', 'duplicate_info', 'manual_note',
    ]

    // 检查是否存在重复候选，如有则显示警告
    const duplicateRecords = records.filter((row, rowIdx) => {
      const info = (row[colIndex['duplicate_info']] || '').trim()
      return info && info !== '无重复'
    })
    let dupWarning = ''
    if (duplicateRecords.length > 0) {
      const types = new Set(duplicateRecords.map(r => (r[colIndex['duplicate_info']] || '').trim()))
      const typeList = [...types].join('、')
      dupWarning = `<div class="review-warning">
        ⚠️ 存在 <strong>${duplicateRecords.length}</strong> 条含有重复记录的候选 (${escapeHtml(typeList)})。
        同一笔银行流水或序时账出现在多个候选匹配中，请仔细审核后决定通过/拒绝，避免批准冲突的匹配。
      </div>`
    }

    // 构建表头
    const thead = `<tr>${displayHeaders.map(h => `<th>${escapeHtml(h)}</th>`).join('')}</tr>`

    // 构建表体
    const tbody = records.map((row, rowIdx) => {
      const cid = row[colIndex['candidate_id']] || `row_${rowIdx}`
      const st = editState[cid] || {}
      // approve 可能为空（无 LLM 决策），此时不预选任何按钮
      const approveVal = st.approve || ''
      const llmReason = st.llm_reason || ''
      const hasLlmDecision = llmReason !== ''

      return `<tr data-review-row="${escapeHtml(cid)}">
        <td>
          <div class="approve-buttons">
            <button class="approve-btn approve-yes ${approveVal === '1' ? 'active' : ''}" data-cid="${escapeHtml(cid)}" data-value="1" title="批准">✓ 通过</button>
            <button class="approve-btn approve-no ${approveVal === '0' ? 'active' : ''}" data-cid="${escapeHtml(cid)}" data-value="0" title="拒绝">✗ 拒绝</button>
          </div>
          ${hasLlmDecision ? '<span class="llm-badge" title="LLM 已给出建议">🤖</span>' : ''}
        </td>
        <td class="cell-id">${escapeHtml(cid)}</td>
        <td class="cell-match-type">${escapeHtml(row[colIndex['match_type']] || '')}</td>
        <td class="cell-wide"><textarea class="review-ta" data-cid="${escapeHtml(cid)}" data-field="bank_summary" rows="4">${escapeHtml(st.bank_summary)}</textarea></td>
        <td class="cell-wide"><textarea class="review-ta" data-cid="${escapeHtml(cid)}" data-field="ledger_summary" rows="4">${escapeHtml(st.ledger_summary)}</textarea></td>
        <td class="cell-wide"><textarea class="review-ta" data-cid="${escapeHtml(cid)}" data-field="llm_reason" rows="4">${escapeHtml(st.llm_reason)}</textarea></td>
        <td class="cell-mid">${escapeHtml(row[colIndex['review_reason']] || '')}</td>
        <td class="cell-mid">${escapeHtml(row[colIndex['duplicate_info']] || '')}</td>
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
      ${dupWarning}
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

    // 绑定 approve 按钮点击
    modal.getBodyEl().querySelectorAll('.approve-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const cid = btn.dataset.cid
        const value = btn.dataset.value
        if (editState[cid]) {
          editState[cid].approve = value
          // 更新按钮样式
          const row = btn.closest('tr')
          row.querySelectorAll('.approve-btn').forEach(b => b.classList.remove('active'))
          btn.classList.add('active')
        }
      })
    })

    // 保存按钮
    modal.getBodyEl().querySelector('#review-save').addEventListener('click', () => saveReview(filePath, records, headers, colIndex, editState))

    // 刷新按钮
    modal.getBodyEl().querySelector('#review-refresh').addEventListener('click', async () => {
      modal.close()
      await openReviewEditor(filePath)
    })
  }

  renderTable()
  modal.open()
}

async function saveReview(filePath, records, headers, colIndex, editState) {
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
    if (colIndex['approve'] !== undefined) newRow[colIndex['approve']] = st.approve || '1'
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
      path: filePath,
      content: csvContent,
      commit_message: 'manual review edit from desktop app',
    })
    alert('✅ 人工复核表已保存。')
  } catch (err) {
    alert(`❌ 保存失败: ${err.message}`)
  }
}
