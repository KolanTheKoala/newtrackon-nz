"""Build a Font Awesome 7.0.1 subset: only the icons the site uses.

Adding an icon: put its name in SOLID (or BRANDS), then
    mkdir fa && cd fa && B=https://cdnjs.cloudflare.com/ajax/libs/font-awesome/7.0.1
    curl -sO $B/css/all.min.css && for f in fa-solid-900 fa-brands-400; do curl -sO $B/webfonts/$f.woff2; done
    pip install fonttools brotli && python ../scripts/fa_subset.py . out
and copy out/*.woff2 to newtrackon/static/fonts/ and out/fa-subset.css to newtrackon/static/css/.
tests/unit/test_icons.py fails if a template uses an icon that isn't in the subset.

Writes fa-subset.css + fa-solid-sub.woff2 + fa-brands-sub.woff2. The CSS is FA's own (base rules kept), with
the per-icon rules cut down to the used icons and @font-face pointing at the subset fonts.
"""
import re
import sys

from fontTools import subset

SRC, OUT = sys.argv[1], sys.argv[2]
SOLID = ["chart-line", "wrench", "moon", "sun", "exclamation-triangle", "user", "trophy", "toolbox", "terminal",
         "rotate", "rss", "question-circle", "plus", "list", "home", "globe-asia", "code", "download", "stopwatch"]
BRANDS = ["github"]
USED = set(SOLID + BRANDS)

css = open(f"{SRC}/all.min.css").read()
css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

# split into top-level rules (handles @font-face / @keyframes / @media blocks)
rules, depth, start = [], 0, 0
for i, ch in enumerate(css):
    if ch == "{":
        depth += 1
    elif ch == "}":
        depth -= 1
        if depth == 0:
            rules.append(css[start:i + 1].strip())
            start = i + 1

icon_rule = re.compile(r"^((?:\.fa-[a-z0-9-]+,?)+)\{--fa:\"(\\[0-9a-f]+|\\.|[^\"\\])\"(?:;--fa--fa:\"[^\"]*\")?\}$")
codes, kept = {}, []
for r in rules:
    m = icon_rule.match(r)
    if m:
        names = [s[4:] for s in m.group(1).split(",") if s]
        for n in names:
            codes.setdefault(n, m.group(2))
        if USED & set(names):
            sel = ",".join(".fa-" + n for n in names if n in USED)
            kept.append(r.replace(m.group(1), sel, 1))
        continue
    if r.startswith("@font-face"):
        continue  # replaced below
    kept.append(r)

missing = USED - set(codes)
if missing:
    sys.exit("icons not found in FA 7.0.1: %s" % sorted(missing))

def cp(n):
    v = codes[n]
    if v.startswith("\\"):
        rest = v[1:]
        return int(rest, 16) if all(c in "0123456789abcdef" for c in rest) and len(rest) > 1 else ord(rest)
    return ord(v)

for font, names, out in (("fa-solid-900", SOLID, "fa-solid-sub"), ("fa-brands-400", BRANDS, "fa-brands-sub")):
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["*"]
    f = subset.load_font(f"{SRC}/{font}.woff2", opts)
    s = subset.Subsetter(opts)
    s.populate(unicodes=[cp(n) for n in names])
    s.subset(f)
    subset.save_font(f, f"{OUT}/{out}.woff2", opts)

faces = (
    '@font-face{font-family:"Font Awesome 7 Free";font-style:normal;font-weight:900;font-display:block;'
    'src:url(../fonts/fa-solid-sub.woff2) format("woff2")}'
    '@font-face{font-family:"Font Awesome 7 Brands";font-style:normal;font-weight:400;font-display:block;'
    'src:url(../fonts/fa-brands-sub.woff2) format("woff2")}'
)
header = "/* Font Awesome Free 7.0.1 (https://fontawesome.com, icons CC BY 4.0, fonts OFL 1.1, code MIT), subset to the icons this site uses */\n"
open(f"{OUT}/fa-subset.css", "w").write(header + "".join(kept) + faces + "\n")
print("icons:", len(USED), "| css rules kept:", len(kept), "of", len(rules))
