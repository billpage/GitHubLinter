"""
Markdown LaTeX-math linter for GitHub-hosted repositories.

Catches the rendering pitfalls that cause LaTeX math to display incorrectly
on GitHub:

1. **Static** — patterns GitHub's MathJax config rejects even though vanilla
   MathJax/KaTeX would accept them (e.g. ``\\operatorname``, ``\\bm``,
   ``\\href``). These cause a visible "macro is not allowed" error in the
   rendered page.

2. **GFM** — corruption introduced by GitHub's markdown sanitiser before
   the math is handed to MathJax. Two distinct failure modes are covered:

   * CommonMark backslash-strip: ``\\X`` (where X is ASCII punctuation) is
     rewritten as literal X inside math. ``\\,`` becomes a literal comma,
     ``\\!`` a literal bang, ``\\bigl\\{`` becomes ``\\bigl{`` — the last
     produces a hard "Missing or unrecognized delimiter for \\bigl" error;
     the others corrupt spacing silently.
   * The ``}_`` emphasis-trap: when `_` follows `}` inside an
     inline ``$...$`` region, CommonMark's emphasis rule fires
     (``}`` is punctuation so the ``_`` is unconditionally
     left-flanking). The underscore is eaten regardless of what
     follows it -- ``}_q``, ``}_0``, ``}_{``, ``}_\\vec`` are all
     broken. See community discussion 65772.

   Both failure modes are skipped for fenced ``\\`\\`\\`math`` blocks and
   for the alternative inline syntax ``$\\`...\\`$`` (backtick-dollar) —
   both forms are exempt from CommonMark inline processing on GitHub.
   The render passes likewise feed protected content to the engines
   verbatim, while dollar-delimited content is stripped first.

3. **Structural** — multi-line ``$$...$$`` blocks placed inside a list item.
   GitHub's markdown preprocessor silently fails to recognise these as math,
   then re-tokenises the indented ``+`` / ``-`` lines as nested bullet items.
   No error message — just garbled output.

4. **Render (KaTeX, optional)** — every expression is fed to KaTeX in strict
   mode *after* applying GitHub's CommonMark backslash-strip transformation,
   so the engine sees what GitHub actually feeds the renderer rather than
   the raw source. Catches malformed LaTeX surviving the strip.

5. **Render (MathJax, optional)** — same expressions through MathJax 3 with
   only the ``base`` and ``ams`` packages loaded, matching GitHub's actual
   config. Catches undefined macros (``\\thickspace``, ``\\medspace``, ...)
   that MathJax with the AllPackages set would silently render as raw text.

The two render passes need ``node`` plus the ``katex`` and ``mathjax-full``
npm packages. When those aren't available the passes are skipped with a
warning; the static, GFM, and structural passes always run.

Run as a CLI from the repository root::

    python check_md_math.py docs/ README.md

Math expressions are extracted from ``$...$`` and ``$$...$$``
dollar-delimited math, ``\\`\\`\\`math``-fenced display math, and the
``$\\`...\\`$`` backtick-dollar inline form.

Exits 0 if clean, 1 if any issues found.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# --------------------------------------------------------------------------- #
# 1. Strip code regions from markdown so $-signs in code don't false-match.   #
#    All fenced blocks (including ```math) are blanked here; ```math is        #
#    re-found by :func:`extract_fenced_math` so its source can be tracked.    #
# --------------------------------------------------------------------------- #

_FENCED_BLOCK = re.compile(
    r"^[ \t]*(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)\n"
    r"(?P<content>.*?)\n[ \t]*(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_INDENTED_CODE_LINE = re.compile(r"^(?: {4}|\t).*$", re.MULTILINE)


def _blank_keep_newlines(match: re.Match) -> str:
    s = match.group(0)
    return "".join("\n" if c == "\n" else " " for c in s)


def strip_code(text: str) -> str:
    """Replace code regions with whitespace, preserving line numbers.

    All fenced code blocks are blanked, including ``\\`\\`\\`math``; the
    fenced-math contents are re-found by :func:`extract_fenced_math` so
    that the linter can track which math expressions came from a fenced
    block (exempt from GitHub's CommonMark backslash-strip — verified
    empirically) versus from ``$...$`` / ``$$...$$`` (subject to it).
    """
    text = _FENCED_BLOCK.sub(_blank_keep_newlines, text)
    text = _INLINE_CODE.sub(_blank_keep_newlines, text)
    text = _INDENTED_CODE_LINE.sub(_blank_keep_newlines, text)
    return text


# --------------------------------------------------------------------------- #
# 2. Extract math expressions from the (code-stripped) text.                  #
# --------------------------------------------------------------------------- #

_BLOCK_MATH = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_INLINE_MATH = re.compile(
    r"(?<![\\$])"    # opening $ not after \ or another $
    r"\$"
    r"(?![ \t\n$`])"  # not followed by whitespace, $, or backtick
    r"([^\n$]+?)"    # body: no newlines, no $
    r"(?<![ \t])"    # last char not whitespace
    r"\$"
    r"(?![0-9$])"    # not followed by a digit (e.g. $5) or another $
)
# GitHub-specific inline math syntax: $`...`$. The backticks protect the
# content from CommonMark's inline processing (emphasis markers, escapes),
# making this form robust to the `}_{` trap and other markdown-sanitiser
# corruption that plain $...$ is subject to. Documented at
# https://github.blog/changelog/2023-05-08-new-delimiter-syntax-for-inline-mathematical-expressions/
_INLINE_MATH_BACKTICK = re.compile(r"\$`([^`\n]+?)`\$")


@dataclass
class MathExpr:
    mode: str    # "inline" or "display"
    expr: str
    line: int
    start: int
    source: str = "dollar"
    # "dollar"   — $...$ or $$...$$ (subject to GitHub's markdown sanitiser)
    # "fenced"   — ```math (exempt — verified empirically)
    # "backtick" — $`...`$ (exempt — backticks protect content from markdown)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def extract_math(stripped: str) -> list[MathExpr]:
    """Return every $-delimited math expression in the (code-stripped) text.

    Three flavours are recognised:

    * ``$$...$$`` block math — tagged ``source="dollar"``, subject to
      GitHub's CommonMark backslash-strip.
    * ``$\\`...\\`$`` inline math — tagged ``source="backtick"``, exempt
      from CommonMark inline processing because the backticks make the
      content a code span as far as markdown is concerned.
    * ``$...$`` inline math — tagged ``source="dollar"``, subject to
      CommonMark backslash-strip and the ``}_{`` emphasis-trap.
    """
    out: list[MathExpr] = []
    masked = stripped

    for m in _BLOCK_MATH.finditer(stripped):
        out.append(MathExpr("display", m.group(1),
                             _line_of(stripped, m.start()), m.start(),
                             source="dollar"))
        s, e = m.span()
        masked = masked[:s] + _blank_keep_newlines(m) + masked[e:]

    # Backtick-dollar inline math — find these *before* the plain $...$
    # pass so the plain regex doesn't try to consume the $`...`$ form.
    for m in _INLINE_MATH_BACKTICK.finditer(masked):
        out.append(MathExpr("inline", m.group(1),
                             _line_of(masked, m.start()), m.start(),
                             source="backtick"))
        s, e = m.span()
        masked = masked[:s] + _blank_keep_newlines(m) + masked[e:]

    for m in _INLINE_MATH.finditer(masked):
        out.append(MathExpr("inline", m.group(1),
                             _line_of(masked, m.start()), m.start(),
                             source="dollar"))

    out.sort(key=lambda r: r.start)
    return out


# Re-find ```math blocks in the *raw* text so we can tag them as fenced.
_FENCED_MATH = re.compile(
    r"^[ \t]*(?P<fence>`{3,})(?P<info>math[^\n]*)\n"
    r"(?P<content>.*?)\n[ \t]*(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def extract_fenced_math(text: str) -> list[MathExpr]:
    """Return every ``\\`\\`\\`math`` fenced block as a display-math
    expression tagged ``source="fenced"``.

    Operates on the *raw* text (not the code-stripped form), since
    :func:`strip_code` blanks every fenced block and the contents would
    otherwise be lost. Fenced math is exempt from GitHub's CommonMark
    backslash-strip (verified empirically), so the linter applies a
    different set of checks to expressions tagged ``"fenced"``.
    """
    out: list[MathExpr] = []
    for m in _FENCED_MATH.finditer(text):
        # Tighten the info-string check: we only want exactly "math"
        # (possibly followed by whitespace) — not "mathematica" etc.
        info = m.group("info")
        if info != "math" and not info.startswith("math "):
            continue
        out.append(MathExpr(
            mode="display",
            expr=m.group("content"),
            line=_line_of(text, m.start()) + 1,  # +1 to land on the content
            start=m.start(),
            source="fenced",
        ))
    return out


# --------------------------------------------------------------------------- #
# 3. Static scan: GitHub-specific blocked macros.                              #
# --------------------------------------------------------------------------- #

_STATIC_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\\operatorname\b"),
     r"\operatorname{...} — GitHub's MathJax config rejects this. "
     r"Use \mathrm{...} (function names) or \text{...} (prose)."),
    (re.compile(r"\\DeclareMathOperator\b"),
     r"\DeclareMathOperator — not supported. Use \mathrm{...} inline."),
    (re.compile(r"\\newcommand\b|\\renewcommand\b|\\def\b"),
     r"\newcommand / \def — custom macros are not supported in GitHub math."),
    (re.compile(r"\\begin\{equation\*?\}"),
     r"\begin{equation} — wrap in $$...$$ instead; GitHub does not auto-number."),
    (re.compile(r"\\href\b"),
     r"\href{...} — disabled in GitHub's renderer."),
    (re.compile(r"\\verb\b"),
     r"\verb — not supported."),
    (re.compile(r"\\label\b|\\ref\b|\\eqref\b"),
     r"\label / \ref / \eqref — cross-references are not rendered."),
    (re.compile(r"\\intertext\b"),
     r"\intertext — not supported."),
    (re.compile(r"\\tag\b"),
     r"\tag — equation numbering is not supported."),
    (re.compile(r"\\mathds\b"),
     r"\mathds — dsfont package not available; use \mathbb."),
    (re.compile(r"\\bm\b"),
     r"\bm — bm package not loaded; use \boldsymbol or \mathbf."),
    (re.compile(r"\\colorbox\b|\\fcolorbox\b"),
     r"\colorbox / \fcolorbox — not supported."),
    (re.compile(r"\\definecolor\b"),
     r"\definecolor — not supported."),
]


def static_scan(expr: str) -> list[str]:
    return [msg for pat, msg in _STATIC_PATTERNS if pat.search(expr)]


# --------------------------------------------------------------------------- #
# 4. GFM scan: backslash-escapes that GitHub's markdown preprocessor strips.  #
# --------------------------------------------------------------------------- #
#
# GitHub's markdown preprocessor applies CommonMark backslash-escape rules
# *inside* math content, even though it shouldn't — any ``\X`` where X is an
# ASCII-punctuation character is rewritten to literal ``X`` before the math
# is handed to MathJax. This corrupts the most common TeX shortcuts.
#
# Two safe replacement strategies:
#   * **Letter-named macro** — works only when the macro is defined in
#     MathJax's base+ams package set, which is what GitHub uses. ``\,``,
#     ``\!``, ``\{``, ``\}`` have working letter-named alternatives;
#     ``\;`` (thick space) and ``\:`` (medium space) DO NOT — their
#     letter-named forms ``\thickspace`` and ``\medspace`` are not defined
#     in MathJax 3 base+ams and render as raw text on GitHub.
#   * **Doubled backslash** — universal: ``\\;`` is parsed by CommonMark
#     as escaped-backslash (``\\``) followed by literal ``;``, leaving
#     ``\;`` for MathJax. Works for every short form because the underlying
#     short forms are all in MathJax's base package.
#
# The recommendations below pick the simpler form when it's known to work,
# and fall back to doubled-backslash when there's no working letter-named
# alternative.

_GFM_TARGETS = [
    # (single-bs pattern, recommended replacement, fallback, description)
    ("\\,",  r"\thinspace",     r"\\,",  "thin space"),
    ("\\!",  r"\negthinspace",  r"\\!",  "negative thin space"),
    ("\\;",  r"\\;",            r"\\;",  "thick space (no working letter-named form)"),
    ("\\:",  r"\\:",            r"\\:",  "medium space (no working letter-named form)"),
    ("\\{",  r"\lbrace",        r"\\{",  "literal left brace (CRITICAL with \\bigl etc.)"),
    ("\\}",  r"\rbrace",        r"\\}",  "literal right brace (CRITICAL with \\bigr etc.)"),
]

# Compile a regex per target with a negative lookbehind so we skip
# occurrences already preceded by a backslash (i.e. already doubled).
_GFM_RE = {
    pat: re.compile(r"(?<!\\)" + re.escape(pat))
    for pat, _, _, _ in _GFM_TARGETS
}


def gfm_escape_scan(expr: str) -> list[tuple[str, str, str, str, int]]:
    """Find TeX shortcuts that GitHub's CommonMark preprocessor will strip.

    Returns a list of ``(pattern, recommended, fallback, description, count)``
    per pattern that occurs in ``expr`` not already doubled.
    """
    out: list[tuple[str, str, str, str, int]] = []
    for pat, repl, fallback, desc in _GFM_TARGETS:
        n = len(_GFM_RE[pat].findall(expr))
        if n > 0:
            out.append((pat, repl, fallback, desc, n))
    return out


def commonmark_strip(s: str) -> str:
    """Apply GitHub's (mis-applied) CommonMark backslash-escape rule
    inside math content: ``\\X`` -> ``X`` for any ASCII punctuation X.

    Used to render expressions through KaTeX/MathJax the way GitHub's
    pipeline actually feeds them, so the render passes catch real failures
    rather than rejecting valid post-strip forms.
    """
    return re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]^_`{|}~])", r"\1", s)


# --------------------------------------------------------------------------- #
# 4b. Emphasis-trap scan: punctuation + `_` inside inline $...$ math.         #
# --------------------------------------------------------------------------- #
# Per CommonMark S6.1, an underscore opens emphasis when it is left-flanking.
# The rule permits emphasis to open when `_` is followed by non-whitespace AND
# preceded by any Unicode punctuation character.  A closing brace `}` is the
# most common trigger (subscript right after a group: `V^{(2)}_{\vec q}`), but
# any ASCII punctuation before `_` is equally broken:
#
#   `}_q`, `}_0`, `}_{`  -- subscript after closing brace (most common)
#   `'_i`, `'_m`, `'_{`  -- subscript after prime (x'_i, f'_{n})
#   `)_n`, `|_{a}`       -- subscript after closing delimiter
#
# The result is that the underscore is eaten and the whole $...$ region
# fails to render; later inline math on the same paragraph line often
# fails too (cascading).
#
# Note: `_` preceded by an ordinary letter or digit is NOT left-flanking
# by this rule, so `r_{ij}` and `\alpha_i` are safe.
#
# Fix: wrap the expression in backtick-dollar form ``$`...`$``.
# Any doubled-backslash spacing (``\\,`` ``\\;``) must be simplified to
# ``\,`` / ``\;`` inside the backtick-dollar region (CommonMark no longer
# strips them).
#
# Community discussion: https://github.com/orgs/community/discussions/65772

def emphasis_trap_scan(expr: str, mode: str) -> list[str]:
    """Return a list of messages for emphasis-trap hits in an inline
    ``$...$`` math expression.

    Only applies to inline math; ``$$...$$`` block math is processed by
    GitHub's parser as a different shape and is not affected by the
    same emphasis-marker rule.
    """
    if mode != "inline":
        return []
    if not re.search(r"(?<=[^\w\s])_\S", expr):
        return []
    m = re.search(r"(?<=[^\w\s])_\S", expr)
    trigger = expr[max(0, m.start()-1):m.end()] if m else "_"
    return [
        f"Inline math contains `{trigger}` -- "
        "GitHub's CommonMark preprocessor treats `_` preceded by "
        "punctuation as an italic opener (CommonMark S6.1). "
        "The underscore is eaten and the whole $...$ region fails to "
        "render; later inline math in the same paragraph may cascade. "
        "Common triggers: `}_{`, `}_q`, `}_0`, `'_i`, `'_{{`, `)_n`. "
        "Fix: switch to backtick-dollar form `$`...`$`. "
        r"Simplify any `\\,` / `\\;` spacing to `\,` / `\;` inside "
        "the backtick-dollar form (CommonMark no longer strips them). "
        "See https://github.com/orgs/community/discussions/65772 ."
    ]


# --------------------------------------------------------------------------- #
# 4c. Inverted-backtick scan: `` `$...$` `` in raw markdown text.             #
# --------------------------------------------------------------------------- #
# GitHub's math pipeline is applied before (or in parallel with) inline-code
# processing in some contexts.  The result is that `` `$\mathbb{Z}_4$` ``
# (backtick OUTSIDE the dollar signs) is still attempted as math rather than
# rendered as plain code.  Because the content `\mathbb{Z}_4` contains `}_4`
# (the emphasis trap), the math fails visually.
#
# The correct protected-math syntax is ``$`...`$`` (backtick INSIDE the
# dollars).  This check detects the inverted form in the raw markdown text.

_INVERTED_BACKTICK_MATH = re.compile(
    r"(?<!\$)"       # leading ` not at the tail of a valid $`...`$ expression
    r"`\$"           # backtick then dollar (inverted form)
    r"(?![`.\s])"    # not followed by backtick, period, or whitespace
    r"[^`\n]{1,120}"
    r"\$`"
    r"(?!\$)"        # trailing ` not at the head of the next $`...`$ expression
)


def inverted_backtick_scan(text: str) -> list[tuple[int, str]]:
    """Scan raw markdown text for the inverted-backtick math pattern
    `` `$...$` `` and return ``(line_number, expr)`` tuples.

    This form is a common mistake when trying to protect inline math from
    CommonMark processing.  The correct syntax is ``$`...`$``.
    """
    results = []
    for m in _INVERTED_BACKTICK_MATH.finditer(text):
        ln = text.count("\n", 0, m.start()) + 1
        results.append((ln, m.group()))
    return results



# --------------------------------------------------------------------------- #

_LIST_ITEM_OPEN = re.compile(r"^(\s*)(?:[-+*]\s+|\d+[.)]\s+)")
_BLANK_LINE = re.compile(r"^\s*$")
_DOLLAR_DOLLAR_OPEN = re.compile(r"^(\s*)\$\$\s*$")
_DOLLAR_DOLLAR_INLINE = re.compile(r"^\s*\$\$.+\$\$\s*$")


def list_item_block_math(text: str) -> list[tuple[int, str, str]]:
    """Find multi-line ``$$...$$`` blocks placed inside a list item.

    Returns ``(line_no, message, snippet)`` for each occurrence.
    """
    lines = text.splitlines()
    issues: list[tuple[int, str, str]] = []
    in_list = False
    list_indent = -1
    in_fenced = False

    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fenced = not in_fenced
            continue
        if in_fenced:
            continue

        m = _LIST_ITEM_OPEN.match(line)
        if m:
            in_list = True
            list_indent = len(m.group(1))
            continue

        if _BLANK_LINE.match(line):
            continue

        # Inside a list item if the current line is indented past list_indent.
        if in_list and (len(line) - len(line.lstrip())) > list_indent:
            md = _DOLLAR_DOLLAR_OPEN.match(line)
            if md and not _DOLLAR_DOLLAR_INLINE.match(line):
                # Look ahead for the closing $$ on a later line.
                for j in range(i, min(i + 60, len(lines))):
                    if _DOLLAR_DOLLAR_OPEN.match(lines[j]):
                        snippet = "\n".join(lines[i - 1:j + 1])
                        if len(snippet) > 200:
                            snippet = snippet[:200] + "..."
                        issues.append((
                            i,
                            "Multi-line $$...$$ block inside a list item — "
                            "GitHub will not recognise it as math. Fix: "
                            "collapse to a single line, "
                            r"or use $$\begin{aligned}...\end{aligned}$$ on one line, "
                            "or rewrite as a ```math fenced code block "
                            "(which is recognised inside list items), "
                            "or move the block out of the list.",
                            snippet.replace("\n", " ↵ "),
                        ))
                        break
            continue

        in_list = False
        list_indent = -1

    return issues


# --------------------------------------------------------------------------- #
# 6. Optional render checks via node + katex / mathjax-full.                  #
# --------------------------------------------------------------------------- #

_KATEX_JS = r"""
const katex = require('katex');
const readline = require('readline');
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  if (!line.trim()) return;
  let item;
  try { item = JSON.parse(line); }
  catch (e) { return; }
  const { id, expr, mode } = item;
  try {
    katex.renderToString(expr, {
      displayMode: (mode === "display"),
      throwOnError: true,
      strict: "error",
      trust: false,
    });
    process.stdout.write(JSON.stringify({id, ok: true}) + "\n");
  } catch (e) {
    process.stdout.write(JSON.stringify({
      id, ok: false, error: String(e.message || e),
    }) + "\n");
  }
});
"""

_MATHJAX_JS = r"""
const { mathjax } = require('mathjax-full/js/mathjax.js');
const { TeX } = require('mathjax-full/js/input/tex.js');
const { SVG } = require('mathjax-full/js/output/svg.js');
const { liteAdaptor } = require('mathjax-full/js/adaptors/liteAdaptor.js');
const { RegisterHTMLHandler } = require('mathjax-full/js/handlers/html.js');
require('mathjax-full/js/input/tex/base/BaseConfiguration.js');
require('mathjax-full/js/input/tex/ams/AmsConfiguration.js');

const adaptor = liteAdaptor();
RegisterHTMLHandler(adaptor);

// GitHub renders math via MathJax 3 with (effectively) only the base and
// ams TeX packages. Use that minimal set here so undefined commands like
// \thickspace and \medspace throw, instead of being silently rendered as
// raw <mtext> by the noundefined fallback that AllPackages would supply.
const tex = new TeX({ packages: ['base', 'ams'] });
const svg = new SVG({ fontCache: 'none' });
const html = mathjax.document('', { InputJax: tex, OutputJax: svg });

const readline = require('readline');
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  if (!line.trim()) return;
  let item;
  try { item = JSON.parse(line); } catch (e) { return; }
  const { id, expr, mode } = item;
  try {
    const node = html.convert(expr, { display: (mode === 'display') });
    const out = adaptor.outerHTML(node);
    const errMatch = out.match(/data-mjx-error="([^"]+)"/);
    if (errMatch) {
      process.stdout.write(JSON.stringify({
        id, ok: false,
        error: errMatch[1].replace(/&quot;/g, '"'),
      }) + "\n");
    } else {
      process.stdout.write(JSON.stringify({id, ok: true}) + "\n");
    }
  } catch (e) {
    process.stdout.write(JSON.stringify({
      id, ok: false, error: String(e.message || e),
    }) + "\n");
  }
});
"""


def _node_available() -> bool:
    return shutil.which("node") is not None


def _run_engine(items: list[tuple[int, MathExpr]],
                engine_js: str,
                engine_name: str,
                node_cwd: Path) -> dict[int, dict]:
    """Run a list of expressions through a node-based engine.

    ``items`` is a list of ``(id, MathExpr)`` pairs. ``node_cwd`` is the
    directory whose ``node_modules/`` provides the engine (``katex`` or
    ``mathjax-full``).
    """
    if not items:
        return {}

    if not _node_available():
        print(f"  (skipping {engine_name}: 'node' not found in PATH)",
              file=sys.stderr)
        return {}

    if not (node_cwd / "node_modules").is_dir():
        print(f"  (skipping {engine_name}: no node_modules in {node_cwd})",
              file=sys.stderr)
        return {}

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     dir=str(node_cwd)) as f:
        f.write(engine_js)
        script_path = f.name

    try:
        payload = "\n".join(
            json.dumps({"id": iid, "expr": me.expr, "mode": me.mode})
            for iid, me in items
        )
        try:
            proc = subprocess.run(
                ["node", script_path],
                input=payload, capture_output=True, text=True,
                cwd=str(node_cwd), timeout=180,
            )
        except subprocess.TimeoutExpired:
            print(f"  ({engine_name} timed out)", file=sys.stderr)
            return {}

        results: dict[int, dict] = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in r:
                results[r["id"]] = r
        return results
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 7. Top-level scan driver.                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class Issue:
    file: Path
    line: int
    severity: str   # STATIC, GFM, STRUCT, KATEX, MATHJX
    mode: str       # inline, display, ""
    expr: str
    message: str


def _walk_md(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix == ".md":
            out.append(p)
        elif p.is_dir():
            out.extend(sorted(q for q in p.rglob("*.md")
                               if ".git" not in q.parts))
    return sorted(set(out))


def scan_paths(paths: Iterable[Path],
               *,
               run_katex: bool = True,
               run_mathjax: bool = True,
               node_cwd: Path | None = None) -> list[Issue]:
    """Scan markdown files for math-rendering issues.

    Returns a flat list of ``Issue`` records. An empty list means clean.
    """
    md_files = _walk_md(paths)
    by_id: dict[int, tuple[Path, MathExpr]] = {}
    next_id = 0
    issues: list[Issue] = []

    # Map id -> the post-CommonMark-strip expression we will actually
    # send to KaTeX and MathJax. Render the form GitHub will receive,
    # not the source-as-written.
    items_for_render: list[tuple[int, MathExpr]] = []

    for md in md_files:
        text = md.read_text(encoding="utf-8")

        # structural pass works on the raw text
        for line, msg, snippet in list_item_block_math(text):
            issues.append(Issue(md, line, "STRUCT", "display", snippet, msg))
        # inverted-backtick pass also works on raw text
        for line, expr in inverted_backtick_scan(text):
            msg = (
                f"Found `{expr}` -- backtick OUTSIDE the dollar signs. "
                "This is the inverted form of the protected-math syntax. "
                "GitHub's math pipeline may still attempt to render "
                "the content as math, bypassing the code-span protection. "
                "Use the correct form: `$`...`$` (backtick INSIDE the "
                "dollars), which is GitHub's documented inline-math syntax "
                "that bypasses CommonMark emphasis processing. "
                "See https://github.blog/changelog/"
                "2023-05-08-new-delimiter-syntax-for-inline-"
                "mathematical-expressions/ ."
            )
            issues.append(Issue(md, line, "STATIC", "inline", expr, msg))

        stripped = strip_code(text)

        # Gather every math expression: $-delimited and ```math fenced.
        # Each is tagged with `source` so the GFM and render passes can
        # treat them differently — fenced math is exempt from GitHub's
        # CommonMark backslash-strip.
        all_exprs = list(extract_math(stripped)) + list(extract_fenced_math(text))

        for me in all_exprs:
            iid = next_id
            next_id += 1
            by_id[iid] = (md, me)

            # For render: dollar-source content is fed through CommonMark
            # strip first (matching what GitHub feeds MathJax). Fenced and
            # backtick-protected content goes to the engines verbatim.
            if me.source == "dollar":
                render_expr = commonmark_strip(me.expr)
            else:
                render_expr = me.expr

            stripped_me = MathExpr(
                mode=me.mode,
                expr=render_expr,
                line=me.line,
                start=me.start,
                source=me.source,
            )
            items_for_render.append((iid, stripped_me))

            # Static pass applies regardless of source: GitHub's MathJax
            # config blocks the same set of macros either way.
            for hit in static_scan(me.expr):
                issues.append(Issue(md, me.line, "STATIC", me.mode, me.expr, hit))

            # GFM and emphasis-trap passes apply *only* to dollar-delimited
            # math. Both fenced ```math and $`...`$ forms are protected
            # from GitHub's CommonMark inline processing.
            if me.source == "dollar":
                for pat, repl, fallback, desc, n in gfm_escape_scan(me.expr):
                    qty = "" if n == 1 else f" (×{n})"
                    if repl == fallback:
                        suggestion = f"`{repl}`"
                    else:
                        suggestion = f"`{repl}` (or `{fallback}`)"
                    msg = (f"GitHub's CommonMark preprocessor strips the "
                           f"backslash from `{pat}` ({desc}) inside math, "
                           f"leaving a literal `{pat[1]}` for MathJax. "
                           f"Replace with {suggestion}{qty}.")
                    issues.append(Issue(md, me.line, "GFM ", me.mode, me.expr, msg))
                for hit in emphasis_trap_scan(me.expr, me.mode):
                    issues.append(Issue(md, me.line, "GFM ", me.mode, me.expr, hit))

    if (run_katex or run_mathjax) and node_cwd is None:
        node_cwd = Path.cwd()

    if run_katex:
        kr = _run_engine(items_for_render, _KATEX_JS, "katex", node_cwd)
        for iid, r in kr.items():
            if not r.get("ok"):
                md, me = by_id[iid]
                err = str(r.get("error", "unknown")).split("\n", 1)[0]
                issues.append(Issue(md, me.line, "KATEX ", me.mode, me.expr, err))

    if run_mathjax:
        mr = _run_engine(items_for_render, _MATHJAX_JS, "mathjax", node_cwd)
        for iid, r in mr.items():
            if not r.get("ok"):
                md, me = by_id[iid]
                err = str(r.get("error", "unknown")).split("\n", 1)[0]
                issues.append(Issue(md, me.line, "MATHJX", me.mode, me.expr, err))

    return issues


# --------------------------------------------------------------------------- #
# 8. CLI.                                                                      #
# --------------------------------------------------------------------------- #

def _format_report(issues: list[Issue], all_files: list[Path]) -> str:
    lines: list[str] = []
    by_file: dict[Path, list[Issue]] = {}
    for it in issues:
        by_file.setdefault(it.file, []).append(it)

    for md in all_files:
        rel = md
        try:
            rel = md.relative_to(Path.cwd())
        except ValueError:
            pass

        if md not in by_file:
            lines.append(f"OK   {rel}")
            continue

        lines.append(f"=== {rel} ({len(by_file[md])} issue(s)) ===")
        for it in sorted(by_file[md], key=lambda i: (i.line, i.severity)):
            preview = it.expr if len(it.expr) <= 90 else it.expr[:87] + "..."
            preview = preview.replace("\n", " ↵ ")
            lines.append(f"  L{it.line:4d} [{it.severity:6s}] [{it.mode:7s}] "
                          f"{it.message}")
            lines.append(f"         EXPR: {preview}")
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python check_md_math.py",
        description="Lint markdown files for GitHub LaTeX math-rendering issues.",
    )
    parser.add_argument("paths", nargs="*", default=["docs/", "README.md"],
                        help="Files or directories to scan "
                             "(default: docs/ and README.md).")
    parser.add_argument("--no-render", action="store_true",
                        help="Skip the optional KaTeX and MathJax render "
                             "passes (only run static + structural checks).")
    parser.add_argument("--node-cwd", type=Path, default=None,
                        help="Directory whose node_modules/ provides "
                             "katex and mathjax-full (default: cwd).")

    args = parser.parse_args(argv)
    paths = [Path(p) for p in args.paths]

    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"error: paths not found: {', '.join(map(str, missing))}",
              file=sys.stderr)
        return 2

    issues = scan_paths(
        paths,
        run_katex=not args.no_render,
        run_mathjax=not args.no_render,
        node_cwd=args.node_cwd,
    )

    all_files = _walk_md(paths)
    report = _format_report(issues, all_files)
    if report:
        print(report)

    n_static = sum(1 for i in issues if i.severity == "STATIC")
    n_gfm    = sum(1 for i in issues if i.severity == "GFM ")
    n_struct  = sum(1 for i in issues if i.severity == "STRUCT")
    n_render  = sum(1 for i in issues if i.severity in ("KATEX ", "MATHJX"))

    print(f"Summary: {len(issues)} issue(s) "
          f"({n_static} static, {n_gfm} gfm, {n_struct} structural, "
          f"{n_render} render) across {len(all_files)} file(s).")

    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
