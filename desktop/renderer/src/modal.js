import { $, escapeHtml } from './dom.js'

/**
 * 通用 Modal 组件 — 在页面中央弹出浮层，支持 ESC 关闭、点击遮罩关闭。
 * 
 * 用法:
 *   const modal = createModal({ title: '标题', width: '90vw' })
 *   modal.setBody('<p>内容</p>')
 *   modal.open()
 *   modal.close()
 *   modal.onClose = () => { ... }
 */

export function createModal({
  title = '',
  width = '85vw',
  height = '85vh',
  closable = true,
} = {}) {
  let onCloseCallback = null

  const overlay = document.createElement('div')
  overlay.className = 'modal-overlay'
  overlay.setAttribute('role', 'dialog')
  overlay.setAttribute('aria-modal', 'true')

  const container = document.createElement('div')
  container.className = 'modal-container'
  container.style.width = width
  container.style.height = height

  const header = document.createElement('div')
  header.className = 'modal-header'

  const titleEl = document.createElement('h2')
  titleEl.className = 'modal-title'
  titleEl.textContent = title

  header.appendChild(titleEl)

  if (closable) {
    const closeBtn = document.createElement('button')
    closeBtn.className = 'modal-close'
    closeBtn.textContent = '✕'
    closeBtn.setAttribute('aria-label', '关闭')
    closeBtn.addEventListener('click', close)
    header.appendChild(closeBtn)
  }

  const body = document.createElement('div')
  body.className = 'modal-body'

  container.appendChild(header)
  container.appendChild(body)
  overlay.appendChild(container)

  // 点击遮罩关闭
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) close()
  })

  function close() {
    if (overlay.parentNode) {
      overlay.parentNode.removeChild(overlay)
    }
    if (onCloseCallback) onCloseCallback()
  }

  function open() {
    // 防止重复挂载
    if (overlay.parentNode) return
    document.body.appendChild(overlay)
    // 聚焦以便 ESC 能触发
    container.focus()
  }

  function setBody(html) {
    body.innerHTML = html
  }

  function getBodyEl() {
    return body
  }

  // ESC 关闭
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && overlay.parentNode) {
      close()
    }
  })

  return {
    open,
    close,
    setBody,
    getBodyEl,
    get overlay() { return overlay },
    get container() { return container },
    set onClose(cb) { onCloseCallback = cb },
  }
}
