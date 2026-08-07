#!/usr/bin/env python3
"""Inject data.json into dashboard/template.html -> dashboard/console.html.
Kept separate so the daily refresh reuses the exact publish artifact the first
publish used. `<` is escaped to \\u003c so a value like "<5 YOE" can never end
the <script> block; JSON.parse turns it back into `<` in the browser.
"""
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
data = open(os.path.join(ROOT, "data.json"), encoding="utf-8").read().replace("<", "\\u003c")
tpl = open(os.path.join(ROOT, "dashboard", "template.html"), encoding="utf-8").read()
out = tpl.replace("__BOTDATA__", data)
assert "__BOTDATA__" not in out, "placeholder not replaced"
# Two outputs from the same build:
#   dashboard/console.html — kept for the claude.ai artifact / manual publish
#   site/index.html        — the static site Vercel deploys
for rel in ("dashboard/console.html", "site/index.html"):
    p = os.path.join(ROOT, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(out)
    print(f"{rel} written ({len(out):,} bytes)")
