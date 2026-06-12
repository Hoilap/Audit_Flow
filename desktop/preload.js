const { contextBridge } = require('electron')

contextBridge.exposeInMainWorld('desktop', {
  // Reserved for future secure IPC exposure
})
