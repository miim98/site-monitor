#!/usr/bin/env python3
"""
웹페이지 변경 모니터 (롤링 보존형)

동작:
  1) config.json 의 사이트 목록을 하나씩 가져온다
  2) 직전 스냅샷(snapshots/<name>.txt)과 비교한다
  3) 바뀐 줄(추가/삭제)을 타임스탬프와 함께 changes.json 에 기록한다
  4) 보존 기간(기본 7일)이 지난 변경 이력은 자동 삭제한다
  5) 사람이 읽기 좋은 CHANGES.md 를 다시 생성한다

비교 기준이 되는 스냅샷은 절대 기간제로 지우지 않는다(매번 최신본으로 덮어쓰기).
지워지는 것은 '변경 이력' 뿐이다.
"""
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
SNAP_DIR = ROOT / "snapshots"
CHANGES_JSON = ROOT / "changes.json"
CHANGES_MD = ROOT / "CHANGES.md"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}
MAX_LINES_PER_CHANGE = 60  # 변경 한 건당 기록할 최대 줄 수(폭주 방지)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def normalize(text):
    """공백 정리 + 빈 줄 제거. 의미 없는 차이로 인한 오탐을 줄인다."""
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def extract_text(html, selector=None):
    """HTML에서 보이는 텍스트만 뽑아 정규화한다(requests/playwright 공용)."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    if selector:
        nodes = soup.select(selector)
        if nodes:
            text = "\n".join(n.get_text("\n") for n in nodes)
        else:  # 셀렉터가 아무것도 못 잡으면 전체 페이지로 폴백
            print(f"  [warn] selector '{selector}' matched nothing -> 전체 페이지 사용",
                  file=sys.stderr)
            text = (soup.body or soup).get_text("\n")
    else:
        text = (soup.body or soup).get_text("\n")
    return normalize(text)


def fetch_text(url, selector=None):
    """정적 HTML 경로(requests). 대부분의 사이트는 이걸로 충분."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return extract_text(resp.text, selector)


def fetch_text_js(url, selector=None):
    """JS 렌더링 경로(Playwright). config 에서 "js": true 인 사이트만 사용."""
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
                    pass  # 못 잡으면 extract_text 의 전체-페이지 폴백에 맡긴다
            html = page.content()
        finally:
            browser.close()
    return extract_text(html, selector)


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def diff_lines(old, new):
    old_lines, new_lines = old.splitlines(), new.splitlines()
    old_set, new_set = set(old_lines), set(new_lines)
    added = [ln for ln in new_lines if ln not in old_set]
    removed = [ln for ln in old_lines if ln not in new_set]
    return added, removed


def load_changes():
    if CHANGES_JSON.exists():
        with open(CHANGES_JSON, encoding="utf-8") as f:
            return json.load(f)
    return []


def prune(changes, retention_days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    kept = []
    for c in changes:
        try:
            if datetime.fromisoformat(c["timestamp"]) >= cutoff:
                kept.append(c)
        except (KeyError, ValueError):
            continue
    return kept


def render_md(changes, retention_days):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# 변경 내역 (최근 {retention_days}일)", "",
           f"_마지막 실행: {now}_", ""]
    if not changes:
        out.append("최근 변경 없음.")
    else:
        for c in sorted(changes, key=lambda x: x["timestamp"], reverse=True):
            out += [f"## {c['timestamp']} — {c['site']}", f"<{c['url']}>", ""]
            if c.get("added"):
                out.append("**추가/변경된 내용:**")
                out += [f"- {ln}" for ln in c["added"]]
                out.append("")
            if c.get("removed"):
                out.append("**사라진 내용:**")
                out += [f"- ~~{ln}~~" for ln in c["removed"]]
                out.append("")
    CHANGES_MD.write_text("\n".join(out), encoding="utf-8")


def main():
    config = load_config()
    retention = int(config.get("retention_days", 7))
    SNAP_DIR.mkdir(exist_ok=True)
    changes = load_changes()
    now_iso = datetime.now(timezone.utc).isoformat()

    for site in config.get("sites", []):
        name, url = site["name"], site["url"]
        selector = site.get("selector")
        snap_path = SNAP_DIR / f"{safe_name(name)}.txt"
        fetch = fetch_text_js if site.get("js") else fetch_text
        try:
            new_text = fetch(url, selector)
        except Exception as e:
            print(f"[ERROR] {name}: {e}", file=sys.stderr)
            continue

        if not snap_path.exists():
            snap_path.write_text(new_text, encoding="utf-8")
            print(f"[INIT]  {name}: 기준 스냅샷 저장 ({len(new_text.splitlines())} 줄)")
            continue

        old_text = snap_path.read_text(encoding="utf-8")
        if new_text == old_text:
            print(f"[SAME]  {name}")
            continue

        added, removed = diff_lines(old_text, new_text)
        changes.append({
            "timestamp": now_iso,
            "site": name,
            "url": url,
            "added": added[:MAX_LINES_PER_CHANGE],
            "removed": removed[:MAX_LINES_PER_CHANGE],
        })
        snap_path.write_text(new_text, encoding="utf-8")
        print(f"[CHANGED] {name}: +{len(added)} -{len(removed)}")

    changes = prune(changes, retention)
    with open(CHANGES_JSON, "w", encoding="utf-8") as f:
        json.dump(changes, f, ensure_ascii=False, indent=2)
    render_md(changes, retention)
    print(f"완료. 보존 기간 {retention}일 내 변경 기록 {len(changes)}건.")


if __name__ == "__main__":
    main()
