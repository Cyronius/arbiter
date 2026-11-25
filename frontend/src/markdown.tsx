// A small Markdown renderer for conversation text. It builds React elements directly
// (never innerHTML), so transcript content cannot inject markup or script.
import {useEffect, useRef, useState, type ReactNode} from 'react';
import {ActionIcon} from './action-icon';

type Block =
  | {kind: 'code'; lang: string; text: string}
  | {kind: 'heading'; level: number; text: string}
  | {kind: 'rule'}
  | {kind: 'quote'; lines: string[]}
  | {kind: 'list'; ordered: boolean; start: number; items: string[][]}
  | {kind: 'table'; align: string[]; head: string[]; rows: string[][]}
  | {kind: 'paragraph'; lines: string[]};

const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([\w+#.-]*)/;
const HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^ {0,3}([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^ {0,3}> ?(.*)$/;
const ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function cells(line: string) {
  return line.trim().replace(/^\||\|$/g, '').split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, '|'));
}

function parse(text: string): Block[] {
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  const blocks: Block[] = [];
  let i = 0;
  const startsBlock = (line: string, next?: string) => FENCE.test(line) || HEADING.test(line) || RULE.test(line) ||
    QUOTE.test(line) || ITEM.test(line) || (line.includes('|') && next !== undefined && TABLE_RULE.test(next));
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    let match = line.match(FENCE);
    if (match) {
      const close = new RegExp(`^ {0,3}${match[1][0]}{${match[1].length},}\\s*$`);
      const body: string[] = [];
      for (i++; i < lines.length && !close.test(lines[i]); i++) body.push(lines[i]);
      blocks.push({kind: 'code', lang: match[2].toLowerCase(), text: body.join('\n')});
      i++;
      continue;
    }
    if ((match = line.match(HEADING))) { blocks.push({kind: 'heading', level: match[1].length, text: match[2]}); i++; continue; }
    if (RULE.test(line)) { blocks.push({kind: 'rule'}); i++; continue; }
    if (QUOTE.test(line)) {
      const body: string[] = [];
      for (; i < lines.length && (match = lines[i].match(QUOTE)); i++) body.push(match[1]);
      blocks.push({kind: 'quote', lines: body});
      continue;
    }
    if (line.includes('|') && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])) {
      const head = cells(line);
      const align = cells(lines[i + 1]).map(cell => cell.startsWith(':') && cell.endsWith(':') ? 'center' : cell.endsWith(':') ? 'right' : 'left');
      const rows: string[][] = [];
      for (i += 2; i < lines.length && lines[i].includes('|') && lines[i].trim(); i++) rows.push(cells(lines[i]));
      blocks.push({kind: 'table', align, head, rows});
      continue;
    }
    if ((match = line.match(ITEM))) {
      // Collect the list: items at the first item's indent; deeper lines stay with their item.
      const indent = match[1].length;
      const ordered = /\d/.test(match[2]);
      const items: string[][] = [];
      while (i < lines.length) {
        const current = lines[i];
        const item = current.match(ITEM);
        if (item && item[1].length === indent && /\d/.test(item[2]) === ordered) {
          items.push([item[3]]);
        } else if (current.trim() && (current.match(/^\s*/)![0].length > indent || !startsBlock(current, lines[i + 1])) && items.length) {
          items[items.length - 1].push(current.slice(Math.min(indent + 2, current.match(/^\s*/)![0].length)));
        } else if (!current.trim() && i + 1 < lines.length && (lines[i + 1].match(/^\s*/)![0].length > indent ||
                   (lines[i + 1].match(ITEM)?.[1].length === indent))) {
          items[items.length - 1].push('');
        } else break;
        i++;
      }
      blocks.push({kind: 'list', ordered, start: ordered ? parseInt(match[2], 10) : 1, items});
      continue;
    }
    const body: string[] = [];
    for (; i < lines.length && lines[i].trim() && (!body.length || !startsBlock(lines[i], lines[i + 1])); i++) body.push(lines[i]);
    blocks.push({kind: 'paragraph', lines: body});
  }
  return blocks;
}

const INLINE = new RegExp([
  /(`+)([\s\S]*?[^`])\1(?!`)/.source,                       // 1,2 code span
  /\[([^\]\n]+)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)/.source, // 3,4 link
  /\*\*(?=\S)([\s\S]*?\S)\*\*|(?<!\w)__(?=\S)([\s\S]*?\S)__(?!\w)/.source,    // 5,6 strong
  /~~(?=\S)([\s\S]*?\S)~~/.source,                          // 7 strike
  /(?<![\w*])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?!\w)|(?<!\w)_(?=\S)([^_\n]*?\S)_(?!\w)/.source, // 8,9 emphasis
  /(https?:\/\/[^\s<>()]*[^\s<>().,;:!?'"\]])/.source,      // 10 bare URL
].join('|'), 'g');

function safeHref(url: string) {
  return /^(https?:|mailto:)/i.test(url) ? url : undefined;
}

function inline(text: string, key = 'i'): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    const at = match.index!;
    if (at > last) out.push(text.slice(last, at));
    const k = `${key}-${at}`;
    if (match[2] !== undefined) out.push(<code key={k}>{match[2].replace(/^ (.*) $/, '$1')}</code>);
    else if (match[3] !== undefined) {
      const href = safeHref(match[4]);
      out.push(href ? <a key={k} href={href} target="_blank" rel="noreferrer noopener">{inline(match[3], k)}</a>
                    : <span key={k} className="md-link">{inline(match[3], k)}</span>);
    } else if (match[5] !== undefined || match[6] !== undefined) out.push(<strong key={k}>{inline(match[5] ?? match[6], k)}</strong>);
    else if (match[7] !== undefined) out.push(<del key={k}>{inline(match[7], k)}</del>);
    else if (match[8] !== undefined || match[9] !== undefined) out.push(<em key={k}>{inline(match[8] ?? match[9], k)}</em>);
    else if (match[10] !== undefined) out.push(<a key={k} href={match[10]} target="_blank" rel="noreferrer noopener">{match[10]}</a>);
    last = at + match[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function lines(body: string[], key: string): ReactNode[] {
  return body.flatMap((line, index) => index ? [<br key={`${key}-br${index}`}/>, ...inline(line, `${key}-${index}`)] : inline(line, `${key}-${index}`));
}

// -- code highlighting ----------------------------------------------------------

const KEYWORDS = new Set(('as async await break case catch class const continue def default del do elif else enum export extends ' +
  'false fn for from func function go if impl import in interface is lambda let loop match mod mut new nil none not null ' +
  'or package pass pub raise return self static struct super switch this throw true try type typeof use var void while with ' +
  'yield None True False and local then end fi done esac elif echo export unset readonly SELECT FROM WHERE INSERT UPDATE DELETE ' +
  'INTO VALUES CREATE TABLE AND OR NOT NULL JOIN ON GROUP BY ORDER LIMIT').split(' '));
const HASH_COMMENTS = new Set(['py', 'python', 'sh', 'bash', 'shell', 'zsh', 'console', 'toml', 'yaml', 'yml', 'ruby', 'rb', 'conf', 'ini', 'dockerfile', 'make', 'makefile', 'r', 'perl']);
const DIFF = new Set(['diff', 'patch']);

export function highlight(text: string, lang: string): ReactNode {
  if (DIFF.has(lang) || (!lang && /^(@@ |--- |\+\+\+ )/m.test(text) && /^[+-]/m.test(text))) {
    return text.split('\n').map((line, index) => {
      const kind = line.startsWith('@@') ? 'hunk' : /^(\+\+\+|---)( |$)/.test(line) ? 'file' : line[0] === '+' ? 'add' : line[0] === '-' ? 'del' : '';
      return <span key={index} className={['tok-line', kind && `tok-${kind}`].filter(Boolean).join(' ')}>{line || ' '}</span>;
    });
  }
  if (['json', 'jsonl'].includes(lang) || !lang && /^\s*[{[]/.test(text)) lang = 'json';
  // Unlabeled blocks are often terminal output or tables; coloring their words would mislead.
  if (!lang || ['text', 'txt', 'plain', 'plaintext', 'output', 'log'].includes(lang)) return text;
  const hash = HASH_COMMENTS.has(lang);
  const pattern = new RegExp([
    hash ? /#.*$/.source : /\/\/.*$|\/\*[\s\S]*?\*\//.source + (lang === 'sql' || lang === 'lua' ? /|--.*$/.source : ''),
    /"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`/.source,
    /\b(?:0x[\da-f]+|\d+(?:\.\d+)?(?:e[+-]?\d+)?)\b/.source,
    /\$\{?[A-Za-z_][\w]*\}?/.source,
    /\b[A-Za-z_][\w]*\b(?=\s*\()/.source,
    /\b[A-Za-z_][\w]*\b/.source,
  ].map(source => `(${source})`).join('|'), 'gmi');
  const out: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    const at = match.index!;
    if (at > last) out.push(text.slice(last, at));
    const token = match[0];
    const kind = match[1] ? 'comment' : match[2] ? (lang === 'json' && /^\s*:/.test(text.slice(at + token.length)) ? 'key' : 'string')
      : match[3] ? 'number' : match[4] ? (hash || !lang ? 'variable' : '') : match[5] ? (KEYWORDS.has(token) ? 'keyword' : 'function')
      : KEYWORDS.has(token) ? 'keyword' : /^[A-Z][a-z]\w*$/.test(token) && lang !== 'json' ? 'type' : '';
    out.push(kind ? <span key={at} className={`tok-${kind}`}>{token}</span> : token);
    last = at + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// The Clipboard API needs a secure context; the board is also served over plain HTTP on the tailnet.
async function copyText(text: string) {
  if (window.isSecureContext && navigator.clipboard) return navigator.clipboard.writeText(text);
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
  // Inside a modal <dialog>, only its subtree can take focus, so attach the helper there.
  (document.activeElement?.closest('dialog') ?? document.body).appendChild(area);
  area.select();
  const ok = document.execCommand('copy');
  area.remove();
  if (!ok) throw new Error('copy refused');
}

function CopyButton({text}: {text: string}) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const timer = useRef<number>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  async function copy() {
    try {
      await copyText(text);
      setState('copied');
    } catch {
      setState('failed');
    }
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState('idle'), 1600);
  }
  return <ActionIcon icon={state === 'copied' ? 'check' : 'copy'} className={`md-copy is-${state}`} onClick={() => void copy()}
    label={state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy code'}/>;
}

export function CodeBlock({text, lang = ''}: {text: string; lang?: string}) {
  return <div className="md-code-block">
    <pre className="md-code"><code>{highlight(text, lang)}</code></pre>
    <div className="md-code-tools">{lang && <span className="md-code-lang">{lang}</span>}<CopyButton text={text}/></div>
  </div>;
}

// -- blocks ----------------------------------------------------------------------

function render(blocks: Block[], key: string): ReactNode[] {
  return blocks.map((block, index) => {
    const k = `${key}.${index}`;
    switch (block.kind) {
      case 'code': return <CodeBlock key={k} text={block.text} lang={block.lang}/>;
      case 'heading': {
        const Tag = `h${Math.min(6, block.level + 3)}` as 'h4';
        return <Tag key={k} className={`md-h md-h${block.level}`}>{inline(block.text, k)}</Tag>;
      }
      case 'rule': return <hr key={k}/>;
      case 'quote': return <blockquote key={k}>{render(parse(block.lines.join('\n')), k)}</blockquote>;
      case 'list': {
        const items = block.items.map((item, n) => {
          const task = item[0].match(/^\[([ xX])\]\s+(.*)$/);
          if (task) item = [task[2], ...item.slice(1)];
          const inner = parse(item.join('\n'));
          // Keep tight list items inline rather than wrapping them in paragraphs.
          const content = inner.length === 1 && inner[0].kind === 'paragraph' ? lines(inner[0].lines, `${k}-${n}`) : render(inner, `${k}-${n}`);
          return <li key={n} className={task ? 'md-task' : undefined}>
            {task && <input type="checkbox" checked={task[1] !== ' '} readOnly disabled aria-label={task[1] !== ' ' ? 'Done' : 'Not done'}/>}
            {content}
          </li>;
        });
        return block.ordered ? <ol key={k} start={block.start !== 1 ? block.start : undefined}>{items}</ol> : <ul key={k}>{items}</ul>;
      }
      case 'table': return <div key={k} className="md-table"><table>
        <thead><tr>{block.head.map((cell, n) => <th key={n} style={{textAlign: block.align[n] as 'left'}}>{inline(cell, `${k}h${n}`)}</th>)}</tr></thead>
        <tbody>{block.rows.map((row, r) => <tr key={r}>{block.head.map((_, n) =>
          <td key={n} style={{textAlign: block.align[n] as 'left'}}>{inline(row[n] ?? '', `${k}r${r}c${n}`)}</td>)}</tr>)}</tbody>
      </table></div>;
      case 'paragraph': return <p key={k}>{lines(block.lines, k)}</p>;
    }
  });
}

export function Markdown({text}: {text: string}) {
  return <div className="md">{render(parse(text), 'm')}</div>;
}
