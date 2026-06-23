/**
 * Electron preload script.
 *
 * Prevents Electron's default behaviour of navigating to dropped files,
 * which would otherwise kill the app when the user drops a file outside
 * the designated drop zone.
 */

// Safety net: prevent file drops from navigating the webContents.
// The container-level drop handler in agentActions.js processes legitimate
// drops on the filetree panel; this document-level handler catches any
// stray drops that land outside the drop zone.
window.addEventListener('dragover', (e) => e.preventDefault())
window.addEventListener('drop', (e) => e.preventDefault())
