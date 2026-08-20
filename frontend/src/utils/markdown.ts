/**
 * Self-contained safe markdown renderer — zero dependencies.
 *
 * Security model: the ENTIRE input is HTML-escaped first, so no raw markup
 * from the model/user can ever reach the DOM. All transforms below only emit
 * tags we generate ourselves, and link hrefs are restricted to http(s).
 */

const ESCAPE_MAP: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (ch) => ESCAPE_MAP[ch])
}

function safeHref(raw: string): string | null {
  const url = raw.trim()
  return /^https?:\/\//i.test(url) ? url : null
}

/** Inline transforms applied to already-escaped text. */
function renderInline(text: string): string {
  let out = text

  // Inline code first; park rendered spans in slots (using control-char
  // markers that cannot occur in escaped text) so later transforms cannot
  // touch code content.
  const slots: string[] = []
  out = out.replace(/`([^`\n]+)`/g, (_m, code: string) => {
    slots.push(`<code>${code}</code>`)
    return `\u0001${slots.length - 1}\u0002`
  })

  // Links: [text](url) — text/href are already HTML-escaped; only allow
  // http(s) schemes, force new-tab + noopener.
  out = out.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (match, label: string, href: string) => {
    const url = safeHref(href)
    if (!url) return match
    return `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`
  })

  // Bold, italic, strikethrough (order matters: ** before *).
  out = out.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
  out = out.replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
  out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
  out = out.replace(/~~([^~\n]+)~~/g, '<del>$1</del>')

  // Restore inline-code slots.
  out = out.replace(/\u0001(\d+)\u0002/g, (_m, idx: string) => slots[Number(idx)] ?? '')
  return out
}

function isTableDivider(line: string): boolean {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line)
}

function splitTableRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim())
}

/** Render untrusted markdown (LLM output) to safe HTML. */
export function renderMarkdown(source: string): string {
  const lines = escapeHtml(source.replace(/\r\n/g, '\n')).split('\n')
  const html: string[] = []
  let paragraph: string[] = []
  let listTag: 'ul' | 'ol' | null = null
  let quote: string[] = []
  let i = 0

  const flushParagraph = () => {
    if (paragraph.length) {
      html.push(`<p>${paragraph.map(renderInline).join('<br>')}</p>`)
      paragraph = []
    }
  }
  const flushList = () => {
    if (listTag) {
      html.push(`</${listTag}>`)
      listTag = null
    }
  }
  const flushQuote = () => {
    if (quote.length) {
      html.push(`<blockquote>${quote.map(renderInline).join('<br>')}</blockquote>`)
      quote = []
    }
  }
  const flushAll = () => {
    flushParagraph()
    flushList()
    flushQuote()
  }

  while (i < lines.length) {
    const line = lines[i]

    // Fenced code block: ``` or ```lang (kept open-ended while streaming).
    const fence = line.match(/^```([\w+-]*)\s*$/)
    if (fence) {
      flushAll()
      const lang = fence[1]
      const buffer: string[] = []
      i++
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        buffer.push(lines[i])
        i++
      }
      i++ // skip closing fence (or run past end of input)
      const cls = lang ? ` class="language-${lang}"` : ''
      html.push(`<pre><code${cls}>${buffer.join('\n')}</code></pre>`)
      continue
    }

    // Blank line ends any open block.
    if (!line.trim()) {
      flushAll()
      i++
      continue
    }

    // Horizontal rule.
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flushAll()
      html.push('<hr>')
      i++
      continue
    }

    // Headings # .. ######.
    const heading = line.match(/^(#{1,6})\s+(.+)$/)
    if (heading) {
      flushAll()
      const level = heading[1].length
      html.push(`<h${level}>${renderInline(heading[2].trim())}</h${level}>`)
      i++
      continue
    }

    // Blockquote (input is escaped, so '>' arrives as '&gt;').
    const quoted = line.match(/^\s*&gt;\s?(.*)$/)
    if (quoted) {
      flushParagraph()
      flushList()
      quote.push(quoted[1])
      i++
      continue
    }

    // Table: header row followed by a divider row.
    if (line.includes('|') && i + 1 < lines.length && isTableDivider(lines[i + 1])) {
      flushAll()
      const headers = splitTableRow(line)
      const rows: string[][] = []
      i += 2
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(splitTableRow(lines[i]))
        i++
      }
      const thead = `<thead><tr>${headers.map((h) => `<th>${renderInline(h)}</th>`).join('')}</tr></thead>`
      const tbody = rows.length
        ? `<tbody>${rows
            .map((row) => `<tr>${row.map((cell) => `<td>${renderInline(cell)}</td>`).join('')}</tr>`)
            .join('')}</tbody>`
        : ''
      html.push(`<table>${thead}${tbody}</table>`)
      continue
    }

    // Unordered list item.
    const unordered = line.match(/^\s*[-*+]\s+(.+)$/)
    if (unordered) {
      flushParagraph()
      flushQuote()
      if (listTag !== 'ul') {
        flushList()
        html.push('<ul>')
        listTag = 'ul'
      }
      html.push(`<li>${renderInline(unordered[1])}</li>`)
      i++
      continue
    }

    // Ordered list item.
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/)
    if (ordered) {
      flushParagraph()
      flushQuote()
      if (listTag !== 'ol') {
        flushList()
        html.push('<ol>')
        listTag = 'ol'
      }
      html.push(`<li>${renderInline(ordered[1])}</li>`)
      i++
      continue
    }

    // Plain paragraph text.
    flushList()
    flushQuote()
    paragraph.push(line)
    i++
  }

  flushAll()
  return html.join('')
}
