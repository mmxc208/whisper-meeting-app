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

/**
 * The viewer is always light. Mermaid's default theme draws dark edges and
 * labels, so a body that follows a dark system theme leaves diagrams
 * unreadable. Every diagram sits on its own white card and fills the card's
 * width instead of keeping Mermaid's natural (often tiny) size.
 */
const BASE_CSS = `
  html { color-scheme: light; }
  body { margin: 0; padding: 16px; font: 14px system-ui; background: #fff !important; color: #1f2328 !important; }
  table { border-collapse: collapse; }
  td, th { border: 1px solid #888; padding: 6px; }
  pre { white-space: pre-wrap; }
  .mermaid { display: flex; justify-content: center; background: #fff; color: #1f2328; border: 1px solid #d0d7de; border-radius: 8px; padding: 16px; margin: 12px 0; overflow: auto; }
  .mermaid svg { width: 100%; max-width: none !important; height: auto; max-height: 85vh; }
`

/**
 * Runs inside the sandboxed sketch document. It promotes model-authored code
 * blocks into Mermaid hosts: the explicit spellings always, and a bare
 * <pre><code> when its text opens with a Mermaid diagram keyword, because the
 * model does not reliably add a class. Diagram text is copied node by node so a
 * <br> inside a label survives as a real element, which is how Mermaid expects
 * an HTML-embedded line break; textContent would drop it.
 */
export const SKETCH_BOOTSTRAP = `(function(){
  var m = window.mermaid;
  if (!m) return;
  var KEYWORD = /^(?:flowchart|graph\\s+(?:TD|TB|BT|RL|LR)|sequenceDiagram|classDiagram|stateDiagram|erDiagram|journey|gantt|pie|quadrantChart|requirementDiagram|gitGraph|C4Context|C4Container|C4Component|C4Dynamic|C4Deployment|mindmap|timeline|zenuml|sankey|xychart|block|packet|kanban|architecture|radar|treemap)\\b/;
  function sourceText(node){
    var out = '';
    for (var child = node.firstChild; child; child = child.nextSibling){
      if (child.nodeType === 3) out += child.nodeValue;
      else if (child.nodeType === 1) out += child.tagName === 'BR' ? '<br/>' : sourceText(child);
    }
    return out;
  }
  function copyInto(target, node){
    for (var child = node.firstChild; child; child = child.nextSibling){
      if (child.nodeType === 3) target.appendChild(document.createTextNode(child.nodeValue));
      else if (child.nodeType === 1){
        if (child.tagName === 'BR') target.appendChild(document.createElement('br'));
        else copyInto(target, child);
      }
    }
  }
  function diagramHead(text){
    // Skip Mermaid front matter (--- ... ---) and %% directive or comment lines,
    // so the keyword test sees the diagram type itself. Plain YAML or a comment
    // block that never reaches a diagram keyword is left as ordinary code.
    return text.replace(/^\\s*---[\\s\\S]*?\\n---[ \\t]*\\n/, '').replace(/^(?:[ \\t]*%%.*(?:\\n|$))+/, '').replace(/^\\s+/, '');
  }
  function explicit(code){
    var parent = code.parentElement;
    return code.classList.contains('language-mermaid') || code.classList.contains('mermaid')
      || (parent !== null && parent.tagName === 'PRE' && parent.classList.contains('mermaid'));
  }
  function promote(){
    var nodes = document.querySelectorAll('code.language-mermaid, code.mermaid, pre > code');
    for (var i = 0; i < nodes.length; i++){
      var code = nodes[i];
      if (!code.parentNode) continue;
      if (!explicit(code) && !KEYWORD.test(diagramHead(sourceText(code)))) continue;
      var host = code.closest('pre') || code;
      var div = document.createElement('div');
      div.className = 'mermaid';
      copyInto(div, code);
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
const BOOTSTRAP = SKETCH_BOOTSTRAP

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
