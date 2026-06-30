#!/usr/bin/env python3
"""
웹페이지 '새 글(리스트 항목)' 모니터 (롤링 보존형)

동작:
  1) config.json 의 사이트에서 글 목록의 {제목, 상세링크} 를 가져온다
  2) 직전까지 본 링크(seen.json)와 비교해 '새로 올라온 항목'만 골라낸다
  3) 새 항목을 감지 시각과 함께 changes.json 에 한 건씩 기록한다
  4) 보존 기간(기본 14일)이 지난 항목은 자동 삭제한다
  5) 사람이 읽기 좋은 CHANGES.md 와, 빈 화면용 latest.json 을 만든다

링크가 없는(작품 항목이 JS 버튼인 등) 사이트는 새 항목을 만들 수 없어
대시보드에서는 '감시 사이트' 카드로만 보인다.
"""
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
CHANGES_JSON = ROOT / "changes.json"   # 감지된 새 항목 목록(대시보드 카드)
CHANGES_MD = ROOT / "CHANGES.md"
LATEST_JSON = ROOT / "latest.json"     # 사이트별 최신 링크(빈 화면용)
SEEN_JSON = ROOT / "seen.json"         # 사이트별로 이미 본 링크(새 항목 판별 기준)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}
DETECT_LIMIT = 40       # 새 항목 감지를 위해 한 사이트에서 가져올 목록 링크 수
LATEST_PER_SITE = 2     # 빈 화면(latest.json)에 보여줄 사이트별 링크 수
SEEN_CAP = 300          # 사이트별로 기억할 링크 상한(무한정 커지지 않게)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_json(path, default):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def extract_links(html, selector, base_url, limit=DETECT_LIMIT):
    """페이지(또는 selector 영역)에서 의미 있는 글 링크를 위에서부터 추출한다.
    목록 페이지는 보통 최신 글이 위에 있으므로 문서 순서 상위 N개를 최신으로 본다."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    scope = None
    if selector:
        scope = soup.select_one(selector)
    if scope is None:
        scope = soup.body or soup
    # 내비게이션/헤더/푸터 링크는 '새 글'이 아니므로 제거
    for tag in scope(["nav", "header", "footer", "aside"]):
        tag.decompose()

    out, seen = [], set()
    for a in scope.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        link = urljoin(base_url, href)
        if not link.startswith("http"):
            continue
        title = re.sub(r"\s+", " ", a.get_text(" ").strip())
        title = re.sub(r"^(.+?)\s+\1$", r"\1", title)  # "All 344 All 344" -> "All 344"
        if len(title) < 4 or len(title) > 120:
            continue
        if link in seen:
            continue
        seen.add(link)
        out.append({"title": title, "link": link})
        if len(out) >= limit:
            break
    return out


def fetch_html(url, selector=None, js=False):
    """페이지 HTML을 가져온다. js=True 면 Playwright(실제 브라우저)로 렌더링."""
    if not js:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.text

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"], locale="ko-KR")
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            # 인트로 애니메이션이 끝나고 본 콘텐츠가 그려질 시간을 준다.
            page.wait_for_timeout(6000)
            # 지연 로딩(스크롤 시 채워지는) 콘텐츠를 유도한다.
            for _ in range(5):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(800)
            if selector:
                try:
                    page.wait_for_selector(selector, timeout=15000)
                except Exception:
                    pass
            return page.content()
        finally:
            browser.close()


def prune(items, retention_days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    kept = []
    for c in items:
        try:
            if datetime.fromisoformat(c["timestamp"]) >= cutoff:
                kept.append(c)
        except (KeyError, ValueError):
            continue
    return kept


def render_md(items, retention_days):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# 새 글 모니터 (최근 {retention_days}일)", "",
           f"_마지막 실행: {now}_", ""]
    if not items:
        out.append("아직 감지된 새 글이 없습니다.")
    else:
        for it in sorted(items, key=lambda x: x["timestamp"], reverse=True):
            out.append(f"- `{it['timestamp']}` · {it['site']} — "
                       f"[{it['title']}]({it['link']})")
    CHANGES_MD.write_text("\n".join(out) + "\n", encoding="utf-8")


def main():
    config = load_config()
    retention = int(config.get("retention_days", 14))
    items = load_json(CHANGES_JSON, [])   # 누적된 '새 항목' 목록
    seen = load_json(SEEN_JSON, {})        # 사이트별 이미 본 링크
    now_iso = datetime.now(timezone.utc).isoformat()
    latest = []

    for site in config.get("sites", []):
        name, url = site["name"], site["url"]
        selector = site.get("selector")
        try:
            html = fetch_html(url, selector, js=bool(site.get("js")))
        except Exception as e:
            print(f"[ERROR] {name}: {e}", file=sys.stderr)
            continue

        site_items = extract_links(html, selector, url, limit=DETECT_LIMIT)
        if site_items:
            latest.append({"site": name, "url": url,
                           "items": site_items[:LATEST_PER_SITE]})

        prev = seen.get(name)
        if prev is None:
            # 최초 실행: 현재 목록을 기준으로만 저장(새 항목 기록 없음)
            seen[name] = [it["link"] for it in site_items][:SEEN_CAP]
            print(f"[INIT]  {name}: 기준 링크 {len(seen[name])}개 저장")
            continue

        prev_set = set(prev)
        new_items = [it for it in site_items if it["link"] not in prev_set]
        for it in new_items:
            items.append({
                "timestamp": now_iso,   # 감지된 시각
                "site": name,
                "url": url,             # 사이트(목록) 주소 — 참고용
                "title": it["title"],
                "link": it["link"],     # 글 상세 페이지 주소 — 카드 클릭 시 이동
            })
        # 새 링크를 앞에 붙이고 상한까지만 보관
        seen[name] = ([it["link"] for it in new_items] + prev)[:SEEN_CAP]
        tag = "NEW " if new_items else "SAME"
        print(f"[{tag}] {name}: 새 항목 {len(new_items)}개")

    items = prune(items, retention)
    with open(CHANGES_JSON, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)
    with open(SEEN_JSON, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)
    render_md(items, retention)
    print(f"완료. 보존 {retention}일 내 새 항목 {len(items)}건.")


if __name__ == "__main__":
    main()
