// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { SKETCH_BOOTSTRAP, buildSketchSrcdoc, buildStandaloneSketchHtml } from '../src/sketchSrcdoc'

type Rendered = { html: string; text: string }

/** Load a built srcdoc into the test document and run the bootstrap against a fake Mermaid. */
function render(modelHtml: string): Rendered[] {
  const srcdoc = buildSketchSrcdoc(modelHtml, 'http://127.0.0.1:1')
  document.documentElement.innerHTML = srcdoc.replace(/^<!doctype html>\n/i, '')
  const rendered: Rendered[] = []
  ;(window as unknown as { mermaid: unknown }).mermaid = {
    initialize: () => undefined,
    run: () => {
      document.querySelectorAll('.mermaid').forEach(node => {
        rendered.push({ html: node.innerHTML, text: node.textContent ?? '' })
      })
    },
  }
  window.eval(SKETCH_BOOTSTRAP)
  return rendered
}

describe('sketch viewer theme', () => {
  it('pins the viewer and the standalone export to a light theme', () => {
    const live = buildSketchSrcdoc('<p>x</p>', 'http://127.0.0.1:1')
    const standalone = buildStandaloneSketchHtml('<p>x</p>', 'window.mermaid = {}')
    for (const html of [live, standalone]) {
      expect(html).toContain('color-scheme: light;')
      expect(html).not.toContain('light dark')
      expect(html).toMatch(/body \{[^}]*background: #fff !important/)
    }
  })

  it('gives every diagram its own white card that fills the available width', () => {
    const html = buildSketchSrcdoc('<p>x</p>', 'http://127.0.0.1:1')
    expect(html).toMatch(/\.mermaid \{[^}]*background: #fff/)
    expect(html).toMatch(/\.mermaid svg \{[^}]*max-width: none !important/)
  })
})

describe('sketch bootstrap promotion', () => {
  it('still promotes the explicit Mermaid spellings', () => {
    const rendered = render(
      '<pre><code class="language-mermaid">graph TD\nA-->B</code></pre>'
      + '<pre class="mermaid"><code>sequenceDiagram\nA->>B: hi</code></pre>'
      + '<code class="mermaid">pie\n"a": 1</code>',
    )
    expect(rendered.map(item => item.text)).toEqual([
      'graph TD\nA-->B',
      'sequenceDiagram\nA->>B: hi',
      'pie\n"a": 1',
    ])
  })

  it('promotes a bare code block whose text starts with a Mermaid diagram keyword', () => {
    const rendered = render(
      '<pre><code>flowchart LR\n  A --> B</code></pre>'
      + '<pre><code>\n  stateDiagram-v2\n  [*] --> Idle</code></pre>'
      + '<pre><code>%%{init: {"theme": "default"}}%%\ngraph LR\nA-->B</code></pre>'
      + '<pre><code>---\ntitle: Flow\n---\nflowchart TD\n  A --> B</code></pre>',
    )
    expect(rendered.map(item => item.text.trim())).toEqual([
      'flowchart LR\n  A --> B',
      'stateDiagram-v2\n  [*] --> Idle',
      '%%{init: {"theme": "default"}}%%\ngraph LR\nA-->B',
      '---\ntitle: Flow\n---\nflowchart TD\n  A --> B',
    ])
  })

  it('leaves ordinary code blocks alone', () => {
    const rendered = render(
      '<pre><code>print("graph")</code></pre>'
      + '<pre><code>SELECT * FROM flowchart</code></pre>'
      + '<p>graph TD is not code</p>'
      + '<pre><code>---\ntitle: deploy\nsteps: 3\n---\nrun: true</code></pre>'
      + '<pre><code>graph G { a -- b }</code></pre>'
      + '<pre><code>%% only a comment\nnot a diagram</code></pre>',
    )
    expect(rendered).toEqual([])
    expect(document.querySelectorAll('pre > code')).toHaveLength(5)
  })

  it('keeps line breaks inside node labels as real br elements for Mermaid', () => {
    const rendered = render(
      '<pre class="mermaid"><code>flowchart TD\n  A["first<br>second"] --> B["x<br/>y"]</code></pre>',
    )
    expect(rendered).toHaveLength(1)
    expect(rendered[0].html).toBe('flowchart TD\n  A["first<br>second"] --&gt; B["x<br>y"]')
  })

  it('flattens inline markup a model wrapped around diagram text', () => {
    const rendered = render(
      '<pre><code><span>flowchart</span> LR\n  <b>A</b> --> B</code></pre>',
    )
    expect(rendered.map(item => item.html)).toEqual(['flowchart LR\n  A --&gt; B'])
  })
})
