"""리포트 markdown → 차트가 바로 보이는 단일 HTML.

- 차트 이미지(sambory9.github.io/.../charts/...)는 저장소의 로컬 파일을 base64로 넣어, 외부 이미지 차단 없이 바로 보이게 한다.
- 이미지를 누르면 화면 가득 확대되고, 다시 누르거나 X로 닫는다.
사용: python3 report_html.py 입력.md 출력.html 저장소경로
리포트 본문은 비공개(프로젝트)이므로 출력 HTML을 저장소에 커밋하지 않는다.
"""
import base64
import re
import sys
from pathlib import Path

import markdown

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
    return f"![{alt}]({url})"


md = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", inline, md)
md = re.sub(r"(?m)^  - ", "    - ", md)  # 2칸 들여쓴 하위 목록을 markdown 규칙(4칸)에 맞춤
body = markdown.markdown(md, extensions=["tables", "sane_lists"])
body = re.sub(r"<img ", '<img loading="lazy" ', body)
# 연속된 차트 문단을 하나의 그리드로 묶는다 (넓은 화면에서 2열)
body = re.sub(r"(?:<p><img[^>]*></p>\s*)+",
              lambda m: '<div class="charts">' + re.sub(r"</?p>", "", m.group(0)) + "</div>", body)
# 섹션마다 차트를 한곳(첫 표 바로 아래, 표가 없으면 제목 아래)에 모은다
parts = re.split(r"(?=<h2>)", body)
out = []
for part in parts:
    imgs = re.findall(r"<img[^>]*>", part)
    if len(imgs) and part.startswith("<h2>"):
        part = re.sub(r'<div class="charts">.*?</div>', "", part, flags=re.S)
        grid = f'<div class="charts n{min(len(imgs),2)}">' + "".join(imgs) + "</div>"
        if "</table>" in part:
            part = part.replace("</table>", "</table>" + grid, 1)
        else:
            part = re.sub(r"(</h2>)", r"\1" + grid, part, count=1)
    out.append(part)
body = "".join(out)
# 목차용 id
toc = []
def hid(m):
    i = len(toc); toc.append(re.sub(r"<[^>]+>", "", m.group(1)))
    return f'<h2 id="s{i}">{m.group(1)}</h2>'
body = re.sub(r"<h2>(.*?)</h2>", hid, body)
nav = "".join(f'<a href="#s{i}">{t}</a>' for i, t in enumerate(toc))

html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>
:root{{--bg:#fff;--fg:#1d2330;--mut:#6b7280;--line:#e5e7eb;--card:#f8fafc}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111418;--fg:#e6e8ec;--mut:#9aa1ad;--line:#2a3039;--card:#181c22}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif}}
.wrap{{max-width:1240px;margin:auto;padding:16px 20px;display:grid;grid-template-columns:1fr;gap:28px}}
nav{{display:none}}
@media (min-width:1100px){{.wrap{{grid-template-columns:180px minmax(0,1fr)}}
 nav{{display:block;position:sticky;top:16px;align-self:start;font-size:13px;border-right:1px solid var(--line);padding-right:12px}}
 nav a{{display:block;padding:5px 0;color:var(--mut);text-decoration:none}}nav a:hover{{color:var(--fg)}}}}
main{{min-width:0}}main>p,main>ul,main>blockquote{{max-width:820px}}
.charts{{display:grid;grid-template-columns:1fr;gap:12px;margin:10px 0}}

.charts img{{margin:0}}
h1{{font-size:21px;line-height:1.35}}h2{{font-size:17px;margin:28px 0 8px;padding-top:10px;border-top:1px solid var(--line)}}
table{{border-collapse:collapse;max-width:100%;font-size:13px;font-variant-numeric:tabular-nums;display:block;overflow-x:auto}}
th,td{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;white-space:nowrap}}
blockquote{{margin:10px 0;padding:8px 12px;background:var(--card);border-radius:8px;color:var(--mut);font-size:13px}}
blockquote p{{margin:4px 0}}
img{{display:block;width:100%;height:auto;border-radius:8px;border:1px solid var(--line);margin:10px 0;background:#fff;cursor:zoom-in}}
a{{color:#2f6fdb}}
#lb{{position:fixed;inset:0;background:rgba(0,0,0,.92);display:none;align-items:center;justify-content:center;z-index:9;padding:12px}}
#lb.on{{display:flex}}#lb img{{max-width:min(100%,1400px);max-height:100%;width:auto;border:0;cursor:zoom-out;margin:0}}
#lb button{{position:fixed;top:14px;right:14px;width:40px;height:40px;border-radius:20px;border:0;background:#fff3;color:#fff;font-size:22px}}
</style></head><body><div class="wrap"><nav><b style="display:block;margin-bottom:6px;color:var(--fg)">목차</b>{nav}</nav><main>{body}</main></div>
<div id="lb"><button aria-label="닫기">×</button><img alt=""></div>
<script>
const lb=document.getElementById('lb'),li=lb.querySelector('img');
document.querySelectorAll('main img').forEach(i=>i.addEventListener('click',()=>{{li.src=i.src;li.alt=i.alt;lb.classList.add('on')}}));
lb.addEventListener('click',()=>lb.classList.remove('on'));
document.addEventListener('keydown',e=>{{if(e.key==='Escape')lb.classList.remove('on')}});
</script></body></html>"""
dst.write_text(html, encoding="utf-8")
print(dst, f"{dst.stat().st_size/1024:.0f}KB")
