"""리포트 markdown → PDF (HTML 리포트와 같은 내용: 표·차트·설명).

- 스크립트 없이 정적 문서만 만든다. 차트는 저장소의 PNG를 내장한다.
- A4 세로, 한 열. 차트는 본문 폭 전체, 차트·표가 페이지 경계에서 잘리지 않게 한다.
- 머리말(제목)과 꼬리말(쪽 번호)을 넣는다.
사용: python3 report_pdf.py 입력.md 출력.pdf 저장소경로
리포트 본문은 비공개(프로젝트)이므로 출력 PDF를 저장소에 커밋하지 않는다.
"""
import base64
import html as h
import re
import sys
from pathlib import Path

import markdown
from playwright.sync_api import sync_playwright

src, dst, repo = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
md = src.read_text(encoding="utf-8")
title = (re.search(r"^#\s+(.+)$", md, re.M) or [None, "지표추적자 리포트"])[1]
PREFIX = "https://sambory9.github.io/market-indicator-tracker/"


def inline(m):
    alt, url = m.group(1), m.group(2)
    if url.startswith(PREFIX):
        p = repo / "docs" / url[len(PREFIX):]
        if p.exists():
            url = "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()
        else:
            return f"*(차트 없음: {alt})*"
    return f"![{alt}]({url})"


md = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", inline, md)
md = re.sub(r"(?m)^  - ", "    - ", md)
body = markdown.markdown(md, extensions=["tables", "sane_lists"])
# 이미지 문단 → figure (페이지 경계에서 잘리지 않게)
body = re.sub(r"<p>(<img[^>]*>)</p>", r'<figure>\1</figure>', body)
# 섹션마다 차트를 첫 표 바로 아래로 모은다 (HTML 리포트와 같은 배치)
parts, out = re.split(r"(?=<h2>)", body), []
for part in parts:
    figs = re.findall(r"<figure>.*?</figure>", part, flags=re.S)
    if figs and part.startswith("<h2>"):
        part = re.sub(r"<figure>.*?</figure>", "", part, flags=re.S)
        block = "".join(figs)
        part = part.replace("</table>", "</table>" + block, 1) if "</table>" in part else \
            re.sub(r"(</h2>)", r"\1" + block, part, count=1)
    out.append(part)
body = "".join(out)

doc = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{h.escape(title)}</title>
<style>
@page {{ size: A4; margin: 16mm 14mm 16mm 14mm; }}
* {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{ font-family: "Noto Sans CJK KR","Noto Sans KR","Apple SD Gothic Neo","NanumGothic",sans-serif;
       font-size: 10.5pt; line-height: 1.6; color: #1d2330; margin: 0; }}
h1 {{ font-size: 17pt; line-height: 1.3; margin: 0 0 8pt; }}
h2 {{ font-size: 13pt; margin: 18pt 0 6pt; padding-top: 6pt; border-top: 1px solid #e5e7eb;
      break-after: avoid; }}
p, li {{ orphans: 2; widows: 2; }}
ul {{ padding-left: 16pt; }}
table {{ border-collapse: collapse; width: 100%; font-size: 9pt; margin: 6pt 0; break-inside: avoid;
         font-variant-numeric: tabular-nums; }}
th {{ background: #f3f4f6; }}
th, td {{ border-bottom: 1px solid #e5e7eb; padding: 3pt 5pt; text-align: left; }}
blockquote {{ margin: 8pt 0; padding: 6pt 10pt; background: #f8fafc; border-radius: 6px; color: #4b5563;
              font-size: 9pt; }}
blockquote p {{ margin: 3pt 0; }}
figure {{ margin: 6pt 0; break-inside: avoid; }}
figure img {{ width: 100%; height: auto; border: 1px solid #e5e7eb; border-radius: 6px; display: block; }}
a {{ color: #2f6fdb; text-decoration: none; }}
hr {{ border: 0; border-top: 1px solid #e5e7eb; margin: 14pt 0 6pt; }}
hr + p {{ font-size: 8.5pt; color: #6b7280; }}
</style></head><body>{body}</body></html>"""

header = '<div style="font-size:7.5pt;color:#9ca3af;width:100%;padding:0 14mm;">' \
         f'{h.escape(title)}</div>'
footer = '<div style="font-size:7.5pt;color:#9ca3af;width:100%;text-align:center;">' \
         '<span class="pageNumber"></span> / <span class="totalPages"></span></div>'

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    pg.set_content(doc, wait_until="load")
    pg.pdf(path=str(dst), format="A4", print_background=True, display_header_footer=True,
           header_template=header, footer_template=footer,
           margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"})
    b.close()
print(dst, f"{dst.stat().st_size / 1024:.0f}KB")
