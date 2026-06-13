"""美赛 COMAP 深入分析"""
import requests, re, json
from bs4 import BeautifulSoup

url = "https://www.comap.com/blog/news-announcements"
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ── 1. 检查 Jina 输出 ──
print("=== Jina Reader output ===")
rj = requests.get(f"https://r.jina.ai/{url}", headers={"Accept": "text/markdown"}, timeout=30)
with open("logs/_comap_jina.md", "w", encoding="utf-8") as f:
    f.write(rj.text)
print(f"Saved {len(rj.text)} bytes to logs/_comap_jina.md")

# 找帖子列表模式
post_titles = re.findall(r'(?:###?\s+|\d+\.\s+|\[\d{2}/\d{2}/\d{4}\]\s*)(.{10,120})', rj.text)
print(f"\nPost-like titles: {len(post_titles)}")
for t in post_titles[:10]:
    print(f"  {t.strip()[:100]}")

# 找日期模式
dates = re.findall(r'\d{1,2}/\d{1,2}/\d{4}', rj.text)
print(f"\nDates found: {len(dates)}")
for d in dates[:10]:
    print(f"  {d}")

# ── 2. 检查是否是 WordPress / CMS ──
print("\n=== CMS detection ===")
r = requests.get(url, headers=headers, timeout=15)
html = r.text

# WordPress
wp = 'wp-content' in html or 'wordpress' in html.lower()
print(f"WordPress: {wp}")

# Ghost CMS
ghost = 'ghost' in html.lower() or 'casper' in html.lower()
print(f"Ghost: {ghost}")

# Wix / Webflow / Squarespace
for kw in ['wix', 'webflow', 'squarespace', 'hubspot']:
    if kw in html.lower():
        print(f"  {kw}: YES")

# ── 3. 查找 API 端点 ──
print("\n=== API patterns in HTML ===")
api_patterns = re.findall(r'(?:api|graphql|wp-json|rest)[^"\'\s]{0,80}', html, re.IGNORECASE)
for a in set(api_patterns)[:15]:
    if len(a) > 5:
        print(f"  {a[:100]}")

# ── 4. 检查是否有 blog post list 结构 ──
soup = BeautifulSoup(html, 'html.parser')
# 找所有带日期样式的元素
for tag in soup.find_all(['time', 'span', 'div']):
    text = tag.get_text(strip=True)
    if re.match(r'\d{1,2}/\d{1,2}/\d{4}', text):
        parent = tag.parent
        links = parent.find_all('a', href=True) if parent else []
        print(f"  Date: {text} | Links in parent: {len(links)}")
        for a in links[:3]:
            print(f"    [{a.get_text(strip=True)[:80]}] -> {a.get('href','')[:80]}")
        break
