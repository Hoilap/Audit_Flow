export const $ = (selector) => document.querySelector(selector)
export const $$ = (selector) => Array.from(document.querySelectorAll(selector))

export function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;')
}

export function formatSeconds(total) {
  const minutes = String(Math.floor(total / 60)).padStart(2, '0')
  const seconds = String(total % 60).padStart(2, '0')
  return `${minutes}:${seconds}`
}

export function isCsv(path) {
  return path.toLowerCase().endsWith('.csv')
}
