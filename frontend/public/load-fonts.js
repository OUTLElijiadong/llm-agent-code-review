// Enable the preloaded font stylesheet without an inline onload handler,
// so the production CSP can reject inline script and event-handler code.
const fontStylesheet = document.getElementById('google-fonts')
if (fontStylesheet instanceof HTMLLinkElement) {
  fontStylesheet.rel = 'stylesheet'
}
