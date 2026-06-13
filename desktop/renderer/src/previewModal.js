import { createModal } from './modal.js'
import { parseCsv } from './csv.js'
import { api } from './api.js'
import { escapeHtml } from './dom.js'

/**
 * CSV/文本 预览 Modal — 所有文件预览从侧边栏面板移至独立弹窗。
 */

export async function openCsvPreview(filePath) {
  let content
  try {
    const resp = await api.readFile(filePath)
    content = resp.content || ''
  } catch (err) {
    content = `读取失败：${err.message}`
  }

  const rows = parseCsv(content)
  const isCsvFile = filePath.toLowerCase().endsWith('.csv') && rows.length > 0

  const modal = createModal({
    title: `预览: ${filePath}`,
    width: '92vw',
    height: '88vh',
  })

  if (isCsvFile) {
    const headers = rows[0]
    const bodyRows = rows.slice(1).slice(0, 1000) // 最多预览 1000 行

    modal.setBody(`
      <div class="preview-info">
        <span class="subtle">${filePath} · ${bodyRows.length} 行数据（最多预览 1000 行）</span>
      </div>
      <div class="preview-table-wrap">
        <table class="table preview-table">
          <thead><tr>${headers.map(h => `<th>${escapeHtml(h)}</th>`).join('')}</tr></thead>
          <tbody>
            ${bodyRows.map(row => `<tr>${headers.map((_, i) => `<td>${escapeHtml(row[i] || '')}</td>`).join('')}</tr>`).join('')}
          </tbody>
        </table>
        ${rows.length > 1001 ? `<p class="subtle" style="padding:12px;text-align:center;">（仅显示前 1000 行，完整数据请直接打开文件）</p>` : ''}
      </div>
    `)
  } else {
    // 纯文本预览
    modal.setBody(`
      <pre class="code-block" style="max-height:100%;overflow:auto;">${escapeHtml(content.slice(0, 50000))}</pre>
    `)
  }

  modal.open()
}
