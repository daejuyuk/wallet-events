"""카드·페이 이벤트 원문 수집기.

사이트마다 원문(텍스트)을 가져와 data/sources/<id>.json 으로 저장하고,
data/index.json 에 수집 결과 요약과 각 파일의 raw 주소를 남긴다.
구조화(혜택·조건·기간 정리)는 새벽 1시 Claude 예약 작업이 맡는다.

사용법:
    python collector/collect.py            # 전부
    python collector/collect.py kbcard     # 일부만
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data"
UA = "Mozilla/5.0 (compatible; wallet-events-collector/1.0; personal use)"
KST = dt.timezone(dt.timedelta(hours=9))
TEXT_LIMIT = 20000      # 페이지 하나당 저장할 최대 글자 수
DETAIL_LIMIT = 1500     # 상세 페이지 하나당 저장할 최대 글자 수
DELAY = 0.8             # 요청 사이 간격(초). 사이트에 부담 주지 않도록

KB_BASE = "https://card.kbcard.com"
KB_PATH = "/BON/DVIEW/HBBMCXCRVNEC0001"


def now() -> str:
    return dt.datetime.now(KST).isoformat(timespec="seconds")


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def html_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    return squash(soup.get_text(" "))


# ---------- robots.txt ----------
_robots: dict[str, robotparser.RobotFileParser | None] = {}


def allowed(url: str) -> bool:
    host = "{0.scheme}://{0.netloc}".format(urlparse(url))
    if host not in _robots:
        rp: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
        try:
            r = requests.get(host + "/robots.txt", headers={"User-Agent": UA}, timeout=15)
            if r.status_code in (401, 403):
                rp.disallow_all = True
            elif r.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(r.text.splitlines())
        except requests.RequestException:
            rp = None          # robots.txt를 못 읽으면 허용으로 봄
        _robots[host] = rp
    rp = _robots[host]
    return True if rp is None else rp.can_fetch(UA, url)


# ---------- 수집 방식 ----------
def collect_static(src: dict, session: requests.Session, _browser) -> list[dict]:
    r = session.get(src["url"], timeout=30)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")
    title = squash(soup.title.get_text()) if soup.title else ""
    return [{"url": src["url"], "title": title, "text": html_text(r.text)[:TEXT_LIMIT]}]


def collect_render(src: dict, _session, browser) -> list[dict]:
    page = browser.new_page(user_agent=UA, locale="ko-KR")
    try:
        page.goto(src["url"], wait_until="networkidle", timeout=60000)
        for _ in range(int(src.get("scrolls", 2))):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(800)
        items = [{"url": src["url"], "title": page.title(),
                  "text": squash(page.inner_text("body"))[:TEXT_LIMIT]}]
        pattern = src.get("follow")
        if pattern:
            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            links = [h for h in dict.fromkeys(hrefs) if re.search(pattern, h)]
            for href in links[: int(src.get("max_follow", 30))]:
                if not allowed(href):
                    continue
                time.sleep(DELAY)
                try:
                    page.goto(href, wait_until="networkidle", timeout=60000)
                    items.append({"url": href, "title": page.title(),
                                  "text": squash(page.inner_text("body"))[:DETAIL_LIMIT * 2]})
                except Exception as e:  # 상세 하나 실패해도 계속
                    items.append({"url": href, "error": f"{type(e).__name__}: {e}"[:200]})
        return items
    finally:
        page.close()


def parse_kb_list(html: str, seen: set[str]) -> list[dict]:
    out = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        m = re.search(r"goDetail\('(\d+)'", a["href"])
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        t = squash(a.get_text(" "))
        period = re.search(r"(\d{4})\.(\d{2})\.(\d{2})\s*~\s*(?:(\d{4})\.(\d{2})\.(\d{2}))?", t)
        start = end = None
        if period:
            start = "-".join(period.group(1, 2, 3))
            if period.group(4):
                end = "-".join(period.group(4, 5, 6))
        out.append({"id": m.group(1), "list_text": t, "start": start, "end": end, "url": KB_BASE + KB_PATH})
    return out


def parse_kb_detail(html: str) -> str:
    text = html_text(html)
    i = text.find("진행 이벤트")
    body = text[i + len("진행 이벤트"):] if i >= 0 else text
    j = body.find("이용 전 확인")
    return squash(body[:j] if j > 0 else body)[:DETAIL_LIMIT]


def collect_kbcard(src: dict, session: requests.Session, _browser) -> list[dict]:
    """KB국민카드 진행중 이벤트: 목록은 POST(pageCount)로 넘기고, 상세는 이벤트일련번호로 POST."""
    first = session.get(KB_BASE + KB_PATH, timeout=30)
    first.raise_for_status()
    form = BeautifulSoup(first.text, "html.parser").find("form", attrs={"name": "mainForm"})
    if form is None:
        raise RuntimeError("mainForm을 찾지 못함 (사이트 구조가 바뀌었을 수 있음)")
    fields = {i.get("name"): i.get("value", "") for i in form.find_all("input") if i.get("name")}

    items: list[dict] = []
    seen: set[str] = set()
    for page_no in range(1, int(src.get("max_pages", 15)) + 1):
        if page_no == 1:
            html = first.text
        else:
            time.sleep(DELAY)
            html = session.post(KB_BASE + KB_PATH, data={**fields, "pageCount": str(page_no)}, timeout=30).text
        found = parse_kb_list(html, seen)
        if not found:
            break
        items.extend(found)

    for it in items[: int(src.get("max_details", 150))]:
        time.sleep(DELAY)
        data = {**fields, "이벤트일련번호": it["id"], "대고객게시여부": "1"}
        try:
            r = session.post(f"{KB_BASE}{KB_PATH}?mainCC=a&eventNum={it['id']}", data=data, timeout=30)
            it["detail"] = parse_kb_detail(r.text)
        except requests.RequestException as e:
            it["detail_error"] = f"{type(e).__name__}: {e}"[:200]
    return items


HANDLERS = {"static": collect_static, "render": collect_render, "kbcard": collect_kbcard}


# ---------- 실행 ----------
def raw_url(rel: str) -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{rel}" if repo else None


def write_index(index: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")


def main(argv: list[str]) -> int:
    index: dict = {"generated_at": now(), "sources": []}
    write_index(index)          # 시작하자마자 한 번 써 둬서, 중간에 죽어도 흔적이 남게
    try:
        conf = yaml.safe_load((ROOT / "collector" / "sources.yml").read_text(encoding="utf-8"))
        only = set(argv)
        sources = [s for s in conf["sources"] if s.get("enabled", True) and (not only or s["id"] in only)]
        (OUT / "sources").mkdir(parents=True, exist_ok=True)

        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})

        browser = pw = None
        if any(s["type"] == "render" for s in sources):
            try:
                from playwright.sync_api import sync_playwright
                pw = sync_playwright().start()
                browser = pw.chromium.launch()
            except Exception as e:   # 브라우저가 안 떠도 static/kbcard 소스는 계속
                index["browser_error"] = f"{type(e).__name__}: {e}"[:300]
                print("[WARN] 브라우저 실행 실패:", index["browser_error"])
                browser = None

        try:
            for src in sources:
                rel = f"data/sources/{src['id']}.json"
                entry = {"id": src["id"], "issuers": src.get("issuers", []), "type": src["type"],
                         "url": src.get("url") or KB_BASE + KB_PATH, "file": raw_url(rel),
                         "fetched_at": now()}
                try:
                    if src["type"] == "render" and browser is None:
                        raise RuntimeError("브라우저를 띄우지 못해 건너뜀 (browser_error 참고)")
                    if not allowed(entry["url"]):
                        raise PermissionError("robots.txt가 수집을 막고 있어 건너뜀")
                    items = HANDLERS[src["type"]](src, session, browser)
                    entry.update(ok=True, count=len(items))
                except Exception as e:  # 한 사이트가 실패해도 나머지는 계속
                    items = []
                    entry.update(ok=False, count=0, error=f"{type(e).__name__}: {e}"[:300])
                (ROOT / rel).write_text(json.dumps({**entry, "items": items}, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
                index["sources"].append(entry)
                write_index(index)      # 소스 하나 끝날 때마다 저장
                print(f"[{'ok' if entry['ok'] else 'FAIL'}] {src['id']}: {entry['count']}건 {entry.get('error', '')}")
        finally:
            if browser:
                browser.close()
            if pw:
                pw.stop()
    except Exception as e:
        index["fatal"] = f"{type(e).__name__}: {e}"[:500]
        print("[FATAL]", index["fatal"])

    index["finished_at"] = now()
    write_index(index)
    return 0 if any(s.get("ok") for s in index["sources"]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
