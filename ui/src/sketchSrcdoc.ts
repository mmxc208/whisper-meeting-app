const MERMAID_PATH = '/vendor/mermaid.min.js'
const FORBIDDEN = 'script,iframe,frame,object,embed,applet,link,meta,base,form,input,button'
const URL_ATTRIBUTES = new Set([
  'action',
  'data',
  'formaction',
  'href',
  'poster',
  'src',
  'srcset',
  'xlink:href',
])
const UNSAFE_CSS = /(?:@import|expression\s*\(|url\s*\(|javascript:)/i

const BASE_CSS = `
  html { color-scheme: light dark; }
  body { margin: 0; padding: 16px; font: 14px system-ui; }
  table { border-collapse: collapse; }
  td, th { border: 1px solid #888; padding: 6px; }
  pre { white-space: pre-wrap; }
  .mermaid { display: flex; justify-content: center; }
  .mermaid svg { max-width: 100%; height: auto; }
`

const BOOTSTRAP = `(function(){
  var m = window.mermaid;
  if (!m) return;
  function promote(){
    var nodes = document.querySelectorAll('code.language-mermaid, code.mermaid, pre.mermaid > code');
    for (var i = 0; i < nodes.length; i++){
      var code = nodes[i];
      var host = code.closest('pre') || code;
      var div = document.createElement('div');
      div.className = 'mermaid';
      div.textContent = code.textContent || '';
      if (host.parentNode) host.parentNode.replaceChild(div, host);
    }
  }
  function start(){
    try { promote(); } catch (e) {}
    try { m.initialize({startOnLoad:false,securityLevel:'strict',suppressErrorRendering:true,theme:'default'}); } catch (e) {}
    try { m.run({querySelector:'.mermaid',suppressErrors:true}); } catch (e) {}
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();`

function csp(origin: string): string {
  const runtime = `${origin}${MERMAID_PATH}`
  return [
    "default-src 'none'",
    `script-src 'unsafe-inline' ${runtime}`,
    "style-src 'unsafe-inline'",
    "img-src data:",
    "font-src data:",
    "connect-src 'none'",
    "form-action 'none'",
    "base-uri 'none'",
  ].join('; ')
}

function standaloneCsp(): string {
  return [
    "default-src 'none'",
    "script-src 'unsafe-inline'",
    "style-src 'unsafe-inline'",
    "img-src data:",
    "font-src data:",
    "connect-src 'none'",
    "form-action 'none'",
    "base-uri 'none'",
  ].join('; ')
}

function scrub(fragment: DocumentFragment): void {
  fragment.querySelectorAll(FORBIDDEN).forEach(node => node.remove())
  fragment.querySelectorAll('*').forEach(element => {
    for (const attribute of [...element.attributes]) {
      const name = attribute.name.toLowerCase()
      if (name.startsWith('on') || URL_ATTRIBUTES.has(name)) {
        element.removeAttribute(attribute.name)
      } else if (name === 'style' && UNSAFE_CSS.test(attribute.value)) {
        element.removeAttribute(attribute.name)
      }
    }
  })
  fragment.querySelectorAll('style').forEach(style => {
    if (UNSAFE_CSS.test(style.textContent ?? '')) style.remove()
  })
}

export function buildSketchSrcdoc(modelHtml: string, origin = window.location.origin): string {
  const parsed = new DOMParser().parseFromString(modelHtml, 'text/html')
  const output = document.implementation.createHTMLDocument('')
  const fragment = output.createDocumentFragment()
  for (const child of [...parsed.body.childNodes]) {
    fragment.appendChild(output.importNode(child, true))
  }
  scrub(fragment)

  const policy = output.createElement('meta')
  policy.httpEquiv = 'Content-Security-Policy'
  policy.content = csp(origin)
  output.head.prepend(policy)
  const charset = output.createElement('meta')
  charset.setAttribute('charset', 'utf-8')
  output.head.appendChild(charset)
  const style = output.createElement('style')
  style.textContent = BASE_CSS
  output.head.appendChild(style)
  const runtime = output.createElement('script')
  runtime.src = `${origin}${MERMAID_PATH}`
  runtime.crossOrigin = 'anonymous'
  output.head.appendChild(runtime)
  output.body.appendChild(fragment)
  const bootstrap = output.createElement('script')
  bootstrap.textContent = BOOTSTRAP
  output.body.appendChild(bootstrap)
  return `<!doctype html>\n${output.documentElement.outerHTML}`
}

/** Build a downloaded sketch that keeps rendering after Kiro Crew is closed. */
export function buildStandaloneSketchHtml(
  modelHtml: string,
  mermaidRuntime: string,
): string {
  if (/<\/script/i.test(mermaidRuntime)) {
    throw new Error('The installed Mermaid runtime cannot be embedded safely.')
  }

  const parsed = new DOMParser().parseFromString(modelHtml, 'text/html')
  const output = document.implementation.createHTMLDocument('Meeting sketch')
  const fragment = output.createDocumentFragment()
  for (const child of [...parsed.body.childNodes]) {
    fragment.appendChild(output.importNode(child, true))
  }
  scrub(fragment)

  const policy = output.createElement('meta')
  policy.httpEquiv = 'Content-Security-Policy'
  policy.content = standaloneCsp()
  output.head.prepend(policy)
  const charset = output.createElement('meta')
  charset.setAttribute('charset', 'utf-8')
  output.head.appendChild(charset)
  const viewport = output.createElement('meta')
  viewport.name = 'viewport'
  viewport.content = 'width=device-width, initial-scale=1'
  output.head.appendChild(viewport)
  const style = output.createElement('style')
  style.textContent = BASE_CSS
  output.head.appendChild(style)
  const runtime = output.createElement('script')
  runtime.textContent = mermaidRuntime
  output.head.appendChild(runtime)
  output.body.appendChild(fragment)
  const bootstrap = output.createElement('script')
  bootstrap.textContent = BOOTSTRAP
  output.body.appendChild(bootstrap)
  return `<!doctype html>\n${output.documentElement.outerHTML}`
}
