// Synchronous before styles: first run is light, explicit dark/auto are retained.
try { const theme=JSON.parse(localStorage.getItem('ave.studio.theme')||'null')||'light'; if(theme==='auto')delete document.documentElement.dataset.theme;else document.documentElement.dataset.theme=theme; } catch { document.documentElement.dataset.theme='light'; }
