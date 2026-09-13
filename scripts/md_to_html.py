#!/usr/bin/env python3
"""Render one of this project's Markdown documents to a styled HTML page.

Exists so `docs/*.md` stays the single source of truth. The alternative --
hand-authoring an HTML copy of each document -- means every correction has
to be made twice, and the two copies drift silently.

Deliberately handles only the Markdown this project actually uses:
headings, paragraphs, fenced code, tables, ordered and unordered lists,
blockquotes, horizontal rules, and inline code/bold/italic/links. Anything
outside that is passed through as text rather than half-rendered, so a
missing feature shows up as visibly wrong instead of quietly wrong.

Usage:
    python3 scripts/md_to_html.py docs/project_report.md out.html "Title"
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

STYLE = """
  :root {
    --ground: #f7f8fa; --surface: #ffffff; --surface-sunk: #eef1f5;
    --ink: #1b2129; --muted: #5c6672; --accent: #0d7d8c;
    --accent-soft: #d8eef1; --rule: #dee3e9; --rule-strong: #c3ccd6;
    --display: "Archivo", "Helvetica Neue", Arial, sans-serif;
    --body: "Source Serif 4", Georgia, "Times New Roman", serif;
    --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --ground: #141920; --surface: #1a2029; --surface-sunk: #10151b;
      --ink: #e4e9ef; --muted: #97a3b1; --accent: #4fc3d4;
      --accent-soft: #12333a; --rule: #2a323d; --rule-strong: #3b4553;
    }
  }
  :root[data-theme="dark"] {
    --ground: #141920; --surface: #1a2029; --surface-sunk: #10151b;
    --ink: #e4e9ef; --muted: #97a3b1; --accent: #4fc3d4;
    --accent-soft: #12333a; --rule: #2a323d; --rule-strong: #3b4553;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--ground); color: var(--ink);
    font-family: var(--body); font-size: 16.5px; line-height: 1.6;
    -webkit-font-smoothing: antialiased;
  }
  .page { max-width: 860px; margin: 0 auto; padding-inline: 22px; padding-block: 44px 64px; }
  h1 {
    font-family: var(--display); font-weight: 700; font-size: clamp(30px, 5vw, 42px);
    line-height: 1.08; letter-spacing: -0.02em; margin: 0 0 6px;
    padding-bottom: 16px; border-bottom: 2px solid var(--ink); text-wrap: balance;
  }
  h2 {
    font-family: var(--display); font-weight: 600; font-size: 24px;
    letter-spacing: -0.012em; margin: 42px 0 0; text-wrap: balance;
  }
  h3 { font-family: var(--display); font-weight: 600; font-size: 17px; margin: 26px 0 0; }
  h4 { font-family: var(--display); font-weight: 600; font-size: 15px; margin: 20px 0 0; color: var(--muted); }
  p { margin: 13px 0 0; max-width: 72ch; }
  strong { font-weight: 600; }
  em { font-style: italic; }
  a { color: var(--accent); word-break: break-word; }
  code {
    font-family: var(--mono); font-size: 0.85em; background: var(--surface-sunk);
    padding: 1px 5px; border-radius: 3px;
  }
  pre {
    margin: 16px 0 0; background: var(--surface-sunk); border: 1px solid var(--rule);
    border-left: 3px solid var(--accent); border-radius: 4px; padding: 13px 15px;
    overflow-x: auto; font-family: var(--mono); font-size: 12.5px; line-height: 1.55;
  }
  pre code { background: none; padding: 0; font-size: inherit; }
  ul, ol { margin: 12px 0 0; padding-left: 24px; }
  li { margin: 5px 0; max-width: 70ch; }
  blockquote {
    margin: 16px 0 0; padding: 12px 16px; background: var(--accent-soft);
    border-radius: 4px;
  }
  blockquote p { margin: 0; }
  blockquote p + p { margin-top: 8px; }
  hr { margin: 38px 0 0; border: none; border-top: 1px solid var(--rule); }
  .table-wrap {
    margin: 16px 0 0; overflow-x: auto; border: 1px solid var(--rule);
    border-radius: 4px; background: var(--surface);
  }
  table { border-collapse: collapse; width: 100%; font-size: 14.5px; font-variant-numeric: tabular-nums; }
  th, td { text-align: left; padding: 9px 13px; border-bottom: 1px solid var(--rule); vertical-align: top; }
  th {
    font-family: var(--display); font-weight: 600; font-size: 11.5px;
    letter-spacing: 0.07em; text-transform: uppercase; color: var(--muted);
    background: var(--surface-sunk); white-space: nowrap;
  }
  tbody tr:last-child td { border-bottom: none; }
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=Archivo:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500'
         '&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap">')


def inline(text: str) -> str:
    """Inline spans. Code is extracted first and restored last, so markup
    inside a code span is shown literally rather than rendered."""
    spans: list[str] = []

    def stash(m: re.Match) -> str:
        spans.append(html.escape(m.group(1)))
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = html.escape(text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{spans[int(m.group(1))]}</code>", text)


def _row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def convert(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("```"):
            body = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                body.append(html.escape(lines[i]))
                i += 1
            out.append("<pre><code>" + "\n".join(body) + "</code></pre>")
            i += 1
            continue

        # A table needs its separator row; without it these are paragraphs.
        if (line.strip().startswith("|") and i + 1 < len(lines)
                and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip())):
            head = _row(line)
            i += 2
            body = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                body.append(_row(lines[i]))
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            rows = "".join(
                "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body
            )
            out.append(f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead>'
                       f"<tbody>{rows}</tbody></table></div>")
            continue

        if re.match(r"^---+$", line.strip()):
            out.append("<hr />")
            i += 1
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
            i += 1
            continue

        if line.startswith(">"):
            body = []
            while i < len(lines) and lines[i].startswith(">"):
                body.append(lines[i].lstrip(">").strip())
                i += 1
            paras = "".join(f"<p>{inline(p)}</p>"
                            for p in "\n".join(body).split("\n\n") if p.strip())
            out.append(f"<blockquote>{paras}</blockquote>")
            continue

        if re.match(r"^\s*[-*]\s+", line) or re.match(r"^\s*\d+\.\s+", line):
            ordered = bool(re.match(r"^\s*\d+\.\s+", line))
            items = []
            while i < len(lines) and (re.match(r"^\s*[-*]\s+", lines[i])
                                      or re.match(r"^\s*\d+\.\s+", lines[i])
                                      or (lines[i].startswith("  ") and lines[i].strip() and items)):
                if lines[i].startswith("  ") and not re.match(r"^\s*([-*]|\d+\.)\s+", lines[i]):
                    items[-1] += " " + lines[i].strip()     # continuation of the previous item
                else:
                    items.append(re.sub(r"^\s*([-*]|\d+\.)\s+", "", lines[i]))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(x)}</li>" for x in items) + f"</{tag}>")
            continue

        if line.strip():
            para = [line.strip()]
            i += 1
            while i < len(lines) and lines[i].strip() and not re.match(
                r"^(#{1,4}\s|```|>|\s*[-*]\s|\s*\d+\.\s|\|)", lines[i]
            ) and not re.match(r"^---+$", lines[i].strip()):
                para.append(lines[i].strip())
                i += 1
            out.append(f"<p>{inline(' '.join(para))}</p>")
            continue

        i += 1
    return "\n".join(out)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    title = sys.argv[3] if len(sys.argv) > 3 else src.stem.replace("_", " ").title()
    body = convert(src.read_text(encoding="utf-8"))
    dst.write_text(
        f"<title>{html.escape(title)}</title>{FONTS}<style>{STYLE}</style>"
        f'<div class="page">{body}</div>',
        encoding="utf-8",
    )
    print(f"wrote {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
