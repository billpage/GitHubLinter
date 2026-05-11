# Bibliography

References used in the design of this linter and useful for anyone
debugging LaTeX-math rendering on GitHub.

## Best starting points

- **Nico Schlömer — *Math on GitHub: The Good, the Bad and the Ugly***
  (20 May 2022). <https://nschloe.github.io/2022/05/20/math-on-github.html>
  The clearest explanation of the underlying problem: GitHub's renderer
  first runs the entire page through a Markdown "sanitiser" which
  *removes backslashes that aren't followed by a letter*. This is the
  root cause of every failure the GFM pass of this linter detects.

- **Nico Schlömer — *Math on GitHub: Following up*** (27 June 2022).
  <https://nschloe.github.io/2022/06/27/math-on-github-follow-up.html>
  Six-week follow-up showing which initial bugs were fixed and which
  remained. The fundamental Markdown/MathJax interaction is still
  unresolved at the time of writing.

- **`nschloe/markdown-math-acid-test`.**
  <https://github.com/nschloe/markdown-math-acid-test>
  A curated test suite of GitHub math failure modes — basic examples,
  consecutive math, indented math, math in blockquotes, escaped dollar
  signs, math in footnotes, math in links, escaped symbols, etc. View
  it on GitHub itself to see the rendering bugs live. Useful for
  validating any tool that tries to render or lint GitHub-flavour math.

## GitHub's official documentation

- **GitHub Docs — *Writing mathematical expressions*.**
  <https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/writing-mathematical-expressions>
  Official reference for the `$...$`, `$$...$$`, and ` ```math ` syntaxes.
  Documents what works; says nothing about the failure modes this
  linter catches.

- **The GitHub Blog — *Math support in Markdown*** (19 May 2022).
  <https://github.blog/2022-05-19-math-support-in-markdown/>
  Announcement of MathJax-based math rendering on GitHub.com. Comment
  thread is where many of the surprising failure reports first surfaced.

- **The GitHub Changelog — *New delimiter syntax for inline mathematical
  expressions*** (8 May 2023).
  <https://github.blog/changelog/2023-05-08-new-delimiter-syntax-for-inline-mathematical-expressions/>
  Adds `` $`...`$ `` as a workaround-syntax for inline math that's
  resistant to some of the Markdown-preprocessor mangling. Useful
  alongside the linter's fenced-math recommendation for display.

## Canonical bug reports

- **GitHub Community Discussion #16993 —
  *math: curly brackets `$\{...\}$` not working***.
  <https://github.com/orgs/community/discussions/16993>
  Canonical discussion of the backslash-strip bug as it affects
  `\{` / `\}` (and by extension `\bigl\{...\bigr\}`, the case that
  produces the most visible MathJax error). The OP correctly diagnoses
  the cause: "The Markdown renderer gets called first."

- **GitHub Community Discussion #55368 — *math: `\operatorname` not
  working***.
  <https://github.com/orgs/community/discussions/55368>
  The original report of GitHub's MathJax config rejecting
  `\operatorname{...}`. Basis for the linter's static-pass rule.

- **GitHub Community Discussion #36915 — *LaTeX/MathJax rendering bugs***
  <https://github.com/orgs/community/discussions/36915>
  Comprehensive long-running thread covering kerning, underscores in
  subscripts, multiple inline math on one line, and other long-tail
  bugs. Several maintainers from MathJax and `nschloe` comment.

- **GitHub Community Discussion #18897 — *LaTeX / MathJax rendering bugs***
  <https://github.com/orgs/community/discussions/18897>
  Earlier thread on the same family of issues; includes a
  bug-acknowledgement reply from GitHub Support and points to the
  `nschloe/github-math-bugs` aggregator.

- **GitHub Community Discussion #57950 — *Markdown weirdness when using
  math mode inside `<details>` element***.
  <https://github.com/orgs/community/discussions/57950>
  Edge case where `$...$` inline math and ` ```math ` fenced math
  interact badly with `<details>` collapsible sections. Mentioned in
  this linter's structural-pass message as one reason to prefer fenced
  math in awkward Markdown contexts.

- **`github/markup` Issue #1688 — *Markdown doesn't recognise
  `\operatorname` anymore***.
  <https://github.com/github/markup/issues/1688>
  Maintainer-side tracking of the operatorname behaviour, useful as a
  cross-reference for the static-pass entries.

## Underlying specifications

- **CommonMark Spec — *Backslash escapes*** (§2.4).
  <https://spec.commonmark.org/0.31.2/#backslash-escapes>
  Definitive specification of the backslash-escape rule that GitHub's
  preprocessor incorrectly applies inside math regions. The set of
  ASCII-punctuation characters listed here is exactly the set the GFM
  pass scans for.

- **CommonMark Spec — *Fenced code blocks*** (§4.5).
  <https://spec.commonmark.org/0.31.2/#fenced-code-blocks>
  Specifies that fenced-code content "is treated as literal text, not
  parsed as inlines" — the theoretical basis for ` ```math ` blocks
  being exempt from the backslash-strip pipeline (empirically confirmed
  in the WPMW project that originated this linter).

## Rendering engines

- **MathJax 3 documentation — *TeX/LaTeX input*.**
  <https://docs.mathjax.org/en/latest/input/tex/index.html>
  Reference for the TeX-input pipeline GitHub uses. The page on
  *TeX/LaTeX extensions* documents the `base` and `ams` packages —
  the set GitHub effectively loads. Macros outside this set
  (`\thickspace`, `\medspace`, etc.) render as raw text via the
  `noundefined` fallback rather than throwing an error, which is why
  this linter's MathJax pass uses an explicit `packages: ['base', 'ams']`
  configuration to surface them.

- **KaTeX — *Supported functions*.**
  <https://katex.org/docs/supported.html>
  Reference for the function set KaTeX recognises in strict mode.
  Useful for understanding the linter's KaTeX pass, which is more
  permissive than the MathJax pass in some respects and stricter in
  others.

## Related tooling and alternatives

- **`nschloe/xhub`.** <https://github.com/nschloe/xhub>
  Browser extension that re-renders GitHub pages with its own math
  pipeline, side-stepping the native one. Useful as a reading-side
  workaround when authoring isn't an option.

- **`remark-math` and the `remark-lint` ecosystem.**
  <https://github.com/remarkjs/remark-math>
  Node.js Markdown linting toolchain with AST-based parsing. An
  AST-based approach is in principle more robust than this project's
  regex-based heuristics for the structural pass (math inside list
  items). The reason this project doesn't use `remark` is that none of
  the off-the-shelf plugins address the CommonMark backslash-strip
  bug — the highest-impact failure mode in practice — and rebuilding
  that on top of `remark` would amount to re-implementing this linter
  in another language.
