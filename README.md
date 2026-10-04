# GitHubLinter — Markdown LaTeX Math Linter

A Python linter that catches LaTeX math rendering failures in GitHub-hosted
Markdown files **before** they reach readers. Designed to run as a GitHub
Actions CI check on every push that touches `.md` files.

Originally developed in the [WPMW project](https://github.com/billpage/wpmw)
by Claude Opus 4.7.

---

## What it checks

GitHub's math pipeline has several quirks that cause valid LaTeX to render
incorrectly or not at all. The linter catches five classes of problems:

| Pass | Severity | Description |
|------|----------|-------------|
| **Static** | Error | Macros GitHub's MathJax config blocks outright — `\operatorname`, `\bm`, `\boldsymbol`, `\href`, `\newcommand`, etc. Cause a visible "macro is not allowed" error. Also covers two preprocessor-level failures where `$...$` never reaches MathJax at all: an opening `$` glued to a hyphen or a quotation mark (`-$x$`, `"$x$`), and math sitting inside a single-delimiter emphasis span (`*text $x$ text*`) — GitHub renders markdown to HTML *before* scanning for `$...$`, so math inside the resulting `<em>` is never picked up. Both leave the dollar signs on the rendered page verbatim. Also an unescaped `|` inside math on a table row (see *Additional tips*). |
| **GFM** | Error/Warning | Corruption introduced by GitHub's CommonMark preprocessor before content reaches MathJax. Covers the backslash-strip (`\,` → literal comma, `\bigl\{` → delimiter error) *and* the punctuation-underscore emphasis-trap: `_` preceded by ANY punctuation (not just `}`) opens italic — `}_q`, `}_0`, `}_{`, `'_i`, `)_n` are all broken. Applies only to `$...$` / `$$...$$` — fenced ` ```math ` and `` $`...`$ `` (backtick-dollar) are both exempt. |
| **Structural** | Error | (1) Multi-line `$$...$$` blocks inside list items: GitHub silently re-tokenises the indented content as nested bullet items — no error, just garbled output. (2) A ` ```math ` fence inside a **list that already has inline math**: GitHub shows it as raw code. (3) A `` $`...`$ `` span wrapped onto a line that starts with a block marker (`-`, `+`, `*`, `1.`, `#`): markdown ends the paragraph there and the whole span shows as code. |
| **KaTeX** | Error | Every expression rendered by KaTeX in strict mode *after* applying the CommonMark strip, so the engine sees exactly what GitHub feeds its renderer. |
| **MathJax** | Error | Same expressions through MathJax 3 with only `base` + `ams` packages — matching GitHub's actual config. Catches macros like `\thickspace` / `\medspace` that a full MathJax install would silently accept. |

The static, GFM, and structural passes are pure Python and always run.
The KaTeX and MathJax render passes require `node` and the respective npm
packages; they are skipped with a warning if unavailable.

---

## Quick start

### Local usage

```bash
# Static + GFM + structural passes only (no node required):
python check_md_math.py --no-render docs/ README.md

# All five passes (requires node + npm packages):
npm install --no-save katex mathjax-full
python check_md_math.py docs/ README.md
```

### As a GitHub Action

Copy `.github/workflows/check_md_math.yml` into your repository. The workflow
triggers on pushes and pull requests that touch any `.md` file, runs all five
passes, and fails the check if any issues are found.

```yaml
# .github/workflows/check_md_math.yml
name: check-md-math

on:
  push:
    branches: [main]
    paths: ["**.md", "check_md_math.py", ".github/workflows/check_md_math.yml"]
  pull_request:
    branches: [main]
    paths: ["**.md", "check_md_math.py", ".github/workflows/check_md_math.yml"]

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.x"
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - name: Install npm render engines
        run: npm install --no-save katex mathjax-full
      - name: Run markdown math linter
        run: python check_md_math.py docs/ README.md
```

Adjust the paths in the final step to match the directories containing your
Markdown files.

---

## CLI reference

```
usage: python check_md_math.py [-h] [--no-render] [--node-cwd NODE_CWD] [paths ...]

positional arguments:
  paths              Files or directories to scan (default: docs/ and README.md).
                     Directories are scanned recursively for *.md files.

options:
  --no-render        Skip the KaTeX and MathJax render passes. Only the static,
                     GFM, and structural passes run. No node required.
  --node-cwd DIR     Directory whose node_modules/ provides katex and mathjax-full
                     (default: current working directory).
```

Exit codes: `0` = clean, `1` = issues found, `2` = bad arguments / path not found.

---

## Sample output

```
=== docs/algorithm.md (2 issue(s)) ===
  L  42 [STATIC] [display] \operatorname{...} — GitHub's MathJax config rejects this.
                             Use \mathrm{...} (function names) or \text{...} (prose).
         EXPR:  \operatorname{Tr}\!\left[\hat{\rho}\,\hat{A}\right]

  L  87 [GFM   ] [inline ] GitHub's CommonMark preprocessor strips the backslash
                             from `\,` (thin space) inside math, leaving a literal `,`
                             for MathJax. Replace with `\thinspace` (or `\\,`).
         EXPR:  a\,+\,b

OK   docs/supplement.md
Summary: 2 issue(s) (1 static, 1 gfm, 0 structural, 0 render) across 2 file(s).
```

---

## Style guide for math in GitHub Markdown

A cheat sheet for writing math that renders correctly on GitHub.

### GFM backslash-strip

GitHub's Markdown preprocessor incorrectly applies CommonMark backslash-escape
rules inside math regions. Any `\X` where X is an ASCII punctuation character
is rewritten to `X` before the expression reaches MathJax. Replace with one of
the safe forms:

| Don't write | Write instead | Notes |
|-------------|---------------|-------|
| `\,` | `\thinspace` (preferred) or `\\,` | thin space |
| `\!` | `\negthinspace` (preferred) or `\\!` | negative thin space |
| `\;` | `\\;` | thick space — **no working letter-named form** (see below) |
| `\:` | `\\:` | medium space — **no working letter-named form** (see below) |
| `\{` | `\lbrace` (preferred) or `\\{` | literal left brace — **CRITICAL** with `\bigl` etc. |
| `\}` | `\rbrace` (preferred) or `\\}` | literal right brace — **CRITICAL** with `\bigr` etc. |

> **Why not `\thickspace` and `\medspace`?** These look like the natural
> letter-named alternatives for `\;` and `\:`, but they are *not* defined in
> MathJax 3 with only `base` and `ams` packages — GitHub's actual config.
> On GitHub they render as raw text instead of math spacing. Use doubled
> backslash (`\\;` / `\\:`) for thick and medium spaces.

Fenced ` ```math ` blocks are **exempt** from this strip (verified
empirically). Inside a fenced math block you can write `\,`, `\;`, `\!`,
`\bigl\{`, `\bigr\}` directly — the backslashes reach MathJax intact.

The linter's **GFM pass** enforces this rule for `$...$` and `$$...$$`
expressions and skips it for fenced blocks.

### Blocked macros

GitHub's MathJax configuration loads only the `base` and `ams` packages and
disables several extensions. These macros are not supported:

| Macro | Replacement |
|-------|-------------|
| `\operatorname{...}` | `\mathrm{...}` for function names, `\text{...}` for prose |
| `\DeclareMathOperator` | Use `\mathrm{...}` inline |
| `\newcommand`, `\renewcommand`, `\def` | Not supported — write macros out in full |
| `\begin{equation}` / `\begin{equation*}` | Use `$$...$$` instead |
| `\href{...}` | Disabled |
| `\verb` | Not supported |
| `\label`, `\ref`, `\eqref` | Cross-references are not rendered |
| `\tag` | Equation numbering is not supported |
| `\intertext` | Not supported |
| `\mathds` | Use `\mathbb` instead |
| `\bm`, `\boldsymbol` | Use `\mathbf{x}` for Latin letters and `\pmb{\xi}` for Greek (`\mathbf{\xi}` renders but is not bold) |
| `\colorbox`, `\fcolorbox`, `\definecolor` | Not supported |

The linter's **static pass** catches all of these.

### Display math inside list items

A `$$...$$` block that spans multiple lines inside a Markdown list item is
silently misinterpreted — GitHub re-tokenises the indented content as nested
bullets. Fix options in order of preference:

1. **Write the equation as inline `` $`...`$ `` spans** joined by prose
   ("... which equals ..."). This always works.
2. **Collapse to a single line**: `` $$E = mc^2$$ ``
3. **Use `aligned` on one line**: `$$\begin{aligned} ... \\ ... \end{aligned}$$`
4. **Move the block out of the list** entirely.
5. **Use a ` ```math ` fenced block** — but only if the list has no inline
   math anywhere before it. A fence in a list that already has inline math
   renders as raw code (see *Render probes* below).

The linter's **structural pass** detects this and suggests the fixes above.

### Fenced math blocks (` ```math `)

GitHub also accepts a fenced-code form for display math:

````
```math
\frac{\partial W}{\partial t} + \frac{p}{m}\frac{\partial W}{\partial x} = 0
```
````

This is equivalent to `$$...$$` for display math, and is **exempt from the
CommonMark backslash-strip pipeline** (verified empirically). Inside a fenced
math block you can write `\,`, `\;`, `\!`, `\bigl\{`, `\bigr\}` directly.

Two reasons to prefer the fenced form:

1. **Heavy use of backslash-escapes** — equations with lots of TeX spacing or
   sized-delimiter braces are clearer with `\,` `\;` `\bigl\{` than with
   `\thinspace` `\\;` `\bigl\lbrace`. Switch to a fenced block and write
   natural TeX.
2. **Awkward Markdown context** — a fence is safer than a multi-line `$$`
   block in a blockquote or beside list markers, **except inside a list that
   has inline math**, where it renders as raw code (see *Render probes*
   below).

Trade-offs: extra surrounding syntax, display-only (no inline use), and
visual diff churn if switching a long-established `$$...$$` block.

The linter applies the static pass (blocked macros) and both render passes to
fenced content, but **skips the GFM pass** — fenced math is exempt by design.

### Render probes (2026-10-04)

Ten fenced blocks in the WPMW docs showed up as raw code. Pasting minimal
cases into a GitHub comment's *Preview* tab isolated the cause:

| Construct | Result |
|---|---|
| ` ```math ` fence at top level, with or without a blank line before it | typesets |
| fence in a list item or blockquote, no inline math in the list | typesets |
| fence in a blockquote, with inline math before it | typesets |
| fence in a list item, content like `\;` and `\thinspace`, no inline math | typesets |
| single-line or multi-line `$$` in a blockquote; single-line `$$` in a list | typesets |
| **fence in a list that has inline math earlier in it** (same item or an earlier item) | **raw code** |

Neither nesting nor a missing blank line is the cause by itself. The case
"inline math appears only *after* the fence" was not tested, so it is not
flagged. `python test_check_md_math_structural.py` runs the cases, and the table-pipe and `\boldsymbol` rules.

### Additional tips

- **Function names**: use `\mathrm{erf}`, `\mathrm{Tr}`, `\mathrm{sgn}`, etc.
  — same glyph and math-mode spacing as `\operatorname`, universally
  supported.
- **Prose in math**: use `\text{...}` for subscripts like
  `_{\text{short-range}}`, unit labels, etc.
- **Bold math**: `\mathbf{x}` for Latin letters and `\pmb{\xi}` for Greek.
  Neither `\bm` nor `\boldsymbol` is loaded on GitHub (base and ams only),
  and `\mathbf{\xi}` renders but is not bold.
- **Inline math adjacent to digits**: `$x$5` can confuse GitHub's parser.
  A space — `$x$ 5` — avoids the problem entirely.
- **Inline math with `}_` or `'_` (subscript right after a brace or prime): wrap in
  backtick-dollar.** GitHub's markdown preprocessor treats any `_`
  preceded by punctuation as the start of an italic span — regardless of what
  follows the `_`. All of `}_q` (letter), `}_0` (digit), `}_{`
  (brace), `}_\vec` (command), and `'_i` (prime) trigger the trap.
  The underscore is eaten, the whole `$...$` fails to render, and
  other inline math later in the same paragraph often cascades and
  breaks too.
  Reference: community discussion
  [#65772](https://github.com/orgs/community/discussions/65772).

  The fix is GitHub's documented alternative inline-math syntax,
  `$`...`$` (backtick-dollar). The backticks make the content a
  code span as far as markdown is concerned, so the inline emphasis
  rule is skipped entirely.

  | Don't write | Write instead |
  | --- | --- |
  | `$V^{(2)}_{\vec q}$` | `` $`V^{(2)}_{\vec q}`$ `` |
  | `$\|\Gamma^{(2)}_q(r)\|$` | `` $`\|\Gamma^{(2)}_q(r)\|`$ `` |
  | `$W^{(2)}_0(x, p)$` | `` $`W^{(2)}_0(x, p)`$ `` |
  | `$(X_i, X'_i)$` | `` $`(X_i, X'_i)`$ `` |

  **Important:** any doubled-backslash spacing such as `\\,` or `\\;`
  inside the expression must be simplified to `\,` / `\;` inside the
  backtick-dollar form, because the backticks bypass CommonMark's
  processing — the extra backslash is no longer needed.

  Inline math without a punctuation-then-underscore pattern (e.g.
  `$\vec r_{ij}$`, `$V_2$`) is fine as plain `$...$`. Display
  math `$$...$$` is also not affected by this rule.

  The linter's GFM pass enforces this.

- **Inline math with `^*` (complex conjugate) or `^{*}`.** A `*` between
  punctuation can both open and close emphasis, so two of them in one
  paragraph -- often the two in `$(x^*, t^*)$` itself -- pair up as
  `<em>`, the tags land inside the `$...$` span, and GitHub shows the raw
  source. Use `` $`...`$ `` for any expression containing `*`. The
  linter's paragraph pass enforces this by emulating CommonMark's
  delimiter matching, so a lone `^*` beside a balanced `**bold**` is
  correctly left alone. Checked against cmark-gfm on every note in the
  source repository and on 18,000 random paragraphs, with no disagreement.

- **A plain `$...$` span wrapped over two source lines** is invisible to
  the per-expression passes, so the `}_{` trap above can go unreported
  there. The paragraph pass checks it; use `` $`...`$ `` here too.

- **`` `$...$` `` (backtick outside the dollars) is actually fine.** An
  earlier version of this linter flagged this as broken, on the theory
  that GitHub's math pipeline could still reach inside a code span. Tested
  directly against GitHub's live renderer (`api.github.com/markdown`,
  `mode=gfm`) and found to be unfounded: a code span protects its content
  completely, including content that contains the `}_` emphasis-trap
  pattern from the bullet above. The check has been removed. If you've
  seen this actually fail on a real page, please open an issue — that
  would mean GitHub's behavior has changed since this was tested (July
  2026), and the check should come back.

- **Inline math with `$` immediately preceded by a hyphen or a quotation
  mark** (e.g. `Fourier-in-$s$`, `-$N$`, `"$x$ returns to zero,"`). GitHub's
  math parser excludes `$` as a math delimiter when the preceding character
  is a hyphen, a straight or curly double quote (`"` “ ”), or a straight or
  curly single quote / apostrophe (`'` ‘ ’) — the hyphen case mirrors the
  practice of treating `-$` as a negative-value dollar sign rather than
  math; the quotation-mark case has no such rationale, it simply isn't
  recognised. Either way the `$...$` expression fails to render and the
  dollar signs are left on the page verbatim.
  Fix: use the backtick-dollar form `$`...`$` — it is recognised
  regardless of the preceding character:

  | Don't write | Write instead |
  | --- | --- |
  | `Fourier-in-$s$` | `Fourier-in-$`s`$` |
  | `-$N$` | `-$`N`$` |
  | `"$x$ returns to zero,"` | `"$`x`$ returns to zero,"` |

  The linter's Static pass detects this.

- **In a table row, escape every `|` inside math as `\|`.** GitHub splits
  a table row into cells at each unescaped pipe *before* it recognises code
  spans or math, so an absolute value or norm on a table row cuts the math
  span in two: the cell ends at the first `|` and the rest is lost. In a
  header row the stray pipes change the column count, and the whole block
  renders as plain text instead of a table.

  ```text
  Don't write:   | rate | $`\sum_q |K_q|`$ |
  Write instead: | rate | $`\sum_q \|K_q\|`$ |
  ```

  (Shown in a code block because a table cannot display an unescaped pipe
  even inside a code span, which is the same rule again.) GFM removes the
  backslash while splitting the row, so the math renderer receives an
  ordinary `|` and the absolute value is drawn correctly. The same `\|`
  *outside* a table is LaTeX's double bar, so this is a table-only rule.
  The linter's table-pipe pass enforces this.

- **Never put `$...$` inside a `*...*` or `_..._` emphasis span.** GitHub
  renders the markdown to HTML *first* and only then scans for `$...$`
  pairs to hand to MathJax. Math that has ended up inside the resulting
  `<em>` is not picked up, and the dollar signs are left on the page
  verbatim — in italics, which makes it look almost deliberate.

  | Don't write | Write instead |
  | --- | --- |
  | `*carries the same $\mu$.*` | `*carries the same $`\mu`$.*` |
  | `*linear in $N$*` | `*linear in $`N`$*` |

  This bites hardest in **figure captions**, which are often italicised
  by house style and often mention symbols; one caption naming a symbol
  three times over produces six stray dollar signs.

  Doubled delimiters (`**strong**`, `__strong__`) are *not* known to have
  this problem — long-standing `**...$x$...**` run-in headers have been
  observed to render correctly — so the linter does not flag them. If a
  counterexample turns up, widen `_EMPH_SPAN` in the linter.

  The linter's Static pass detects this.
- **Multi-line `$$...$$` outside lists**: fine and preferred for long
  derivations. The structural restriction applies only inside list items.

---

## Requirements

- Python 3.10+ (uses `|` union type hints from 3.10)
- For render passes: Node.js 18+ with `katex` and `mathjax-full` npm packages

No third-party Python packages are required.

---

## Repository layout

```
check_md_math.py              # The linter — single self-contained file
test_check_md_math_structural.py  # Regression cases for the structural and table-pipe/boldsymbol rules
.github/
  workflows/
    check_md_math.yml         # GitHub Actions CI workflow
README.md                     # This file
```

---

## Origin

This linter was cherry-picked from the
[WPMW project](https://github.com/billpage/wpmw) where it lives at
`src/wpmwlib/check_md_math.py`. The WPMW version is invoked as
`PYTHONPATH=src python -m wpmwlib.check_md_math`; this standalone version
drops the package wrapper and is invoked directly as
`python check_md_math.py`.

The checks are derived from rendering failures actually encountered while
authoring LaTeX-heavy Markdown on GitHub, and have been validated against
GitHub's live renderer.

---

## License

TODO — license not yet selected.
