#!/usr/bin/env python3
"""
chase.py - 상장법인 네이버 뉴스 감성 분석기

1) 상장법인목록.xls에서 (회사명, 종목코드) 읽기
2) 네이버 뉴스 검색으로 오늘 날짜 기사 URL 수집 후 본문 추출
3) llama.cpp (port 8000) EXAONE-3.5-32B로 감성 분석 (긍정/부정/중립)
4) chase-<date>.csv에 결과 저장 (이어쓰기 지원)
"""

import csv
import io
import itertools
import os
import re
import time
import datetime
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup
import requests

# ── 설정 ─────────────────────────────────────────────────────────────────────
XLS_FILE    = "상장법인목록.xls"
MODEL_NAME  = "EXAONE-3.5-32B"
LLAMA_CPP_URL = "http://localhost:8000/v1/chat/completions"
TODAY       = datetime.date.today()
TODAY_STR   = TODAY.strftime("%Y.%m.%d")        # e.g. "2026.03.29"
OUTPUT_CSV  = f"chase-{TODAY.strftime('%Y%m%d')}.csv"

CLIENT_ID     = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")

ARTICLE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}

ARTICLE_DELAY = 0.5   # 기사 본문 요청 간격(초)

# ── 스레드별 stdout 캡처 ──────────────────────────────────────────────────────

_real_stdout = sys.stdout
_thread_local = threading.local()
_worker_counter = itertools.count(1)
_worker_counter_lock = threading.Lock()
_thread_outputs: dict[int, io.StringIO] = {}
_thread_outputs_lock = threading.Lock()


class _ThreadSplitStream:
    """실제 stdout과 각 스레드의 개별 버퍼에 동시에 출력."""

    def write(self, text: str) -> int:
        _real_stdout.write(text)
        buf = getattr(_thread_local, "buffer", None)
        if buf is not None:
            buf.write(text)
        return len(text)

    def flush(self) -> None:
        _real_stdout.flush()

    def fileno(self) -> int:
        return _real_stdout.fileno()


# ── 헬퍼 함수 ─────────────────────────────────────────────────────────────────

def load_companies(path: str) -> list[tuple[str, str]]:
    """HTML-as-XLS 파일에서 (회사명, 종목코드) 리스트 반환"""
    with open(path, "rb") as f:
        content = f.read().decode("euc-kr", errors="replace")
    soup = BeautifulSoup(content, "lxml")
    rows = soup.find_all("tr")
    companies = []
    for row in rows[1:]:  # 헤더 제외
        cells = row.find_all(["th", "td"])
        if len(cells) >= 3:
            name = cells[0].get_text(strip=True)
            code = cells[2].get_text(strip=True)
            if name:
                companies.append((name, code))
    return companies


def get_article_urls(keyword: str) -> list[tuple[str, str]]:
    """
    네이버 뉴스 검색 API로 기사 URL 수집 (display=100, 최신순).
    n.news.naver.com URL을 우선 사용하고, 없으면 originallink 사용.
    반환값: (url, pubDate) 튜플 리스트
    """
    api_url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": CLIENT_ID,
        "X-Naver-Client-Secret": CLIENT_SECRET,
    }
    params = {"query": keyword, "display": 100, "sort": "date"}

    try:
        resp = requests.get(api_url, headers=headers, params=params, timeout=12)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [경고] 검색 API 요청 실패 ({keyword}): {e}")
        return []

    items = resp.json().get("items", [])
    seen: set[str] = set()
    urls: list[tuple[str, str]] = []
    for item in items:
        url = item.get("link") or item.get("originallink", "")
        pub_date = item.get("pubDate", "")
        if url and url not in seen:
            seen.add(url)
            urls.append((url, pub_date))
    return urls


def is_today_pub(pub_date: str) -> bool | None:
    """
    pubDate 문자열로 오늘 날짜 여부 확인.
    - True: 오늘
    - False: 오늘 아님 (과거)
    - None: 파싱 실패 (크롤링으로 확인 필요)
    """
    if not pub_date:
        return None
    try:
        dt = parsedate_to_datetime(pub_date)
        return dt.date() >= TODAY
    except Exception:
        return None


_PAST_TODAY = object()  # fetch_article 반환용 sentinel: 날짜가 지난 기사 → 루프 중단


def fetch_article(url: str, pub_date: str = "", keyword: str = "") -> dict | None | object:
    """
    n.news.naver.com 기사 URL에서 제목, 날짜, 본문을 추출.
    pubDate가 있으면 크롤링 전에 날짜를 먼저 확인해 불필요한 요청을 줄인다.
    - 성공(오늘 기사): dict 반환
    - 예외 / 본문 부족: None 반환 (계속 진행)
    - 오늘 날짜 아님: _PAST_TODAY 반환 (루프 중단 신호)
    """
    # pubDate로 먼저 날짜 확인 — 파싱 성공 시 크롤링 없이 판단
    pub_check = is_today_pub(pub_date)
    if pub_check is False:
        return _PAST_TODAY

    try:
        resp = requests.get(url, headers=ARTICLE_HEADERS, timeout=12)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [경고] 기사 요청 실패 ({url}): {e}")
        return None

    soup = BeautifulSoup(resp.text, "lxml")

    # pubDate 파싱 실패 시 HTML에서 날짜 재확인
    date_text = ""
    if pub_check is None:
        date_el = soup.select_one(
            "span.media_end_head_info_datestamp_time, "
            "span.article_info_date, "
            ".date"
        )
        if date_el is None:
            return None  # 날짜 파악 불가 → 건너뜀
        date_text = date_el.get_text(strip=True)
        if not date_text:
            return None
        is_today = (
            TODAY_STR in date_text
            or "분 전" in date_text
            or "시간 전" in date_text
        )
        if not is_today:
            return _PAST_TODAY
    else:
        # pubDate로 오늘 확인된 경우 — HTML 날짜 표시용으로만 읽기
        date_el = soup.select_one(
            "span.media_end_head_info_datestamp_time, "
            "span.article_info_date, "
            ".date"
        )
        date_text = date_el.get_text(strip=True) if date_el else pub_date

    # 제목
    title_el = soup.select_one(
        "h2#title_area, "
        "h2.media_end_head_headline, "
        ".news_headline, "
        "h1.tit_art"
    )
    title = title_el.get_text(strip=True) if title_el else ""

    # 본문 추출: 전체 body 텍스트에서 키워드가 포함된 가장 큰 청크를 선택
    raw = soup.body.get_text() if soup.body else ""
    # 탭/개행 사이의 공백 제거 (2회 반복으로 교차 패턴 처리)
    temp = re.sub(r'\n[ ]+\n', '\n\n', raw)
    temp = re.sub(r'\n[ ]+\t', '\n\t', temp)
    temp = re.sub(r'\t[ ]+\n', '\t\n', temp)
    temp = re.sub(r'\t[ ]+\t', '\t\t', temp)
    temp = re.sub(r'\n[ ]+\n', '\n\n', temp)
    temp = re.sub(r'\n[ ]+\t', '\n\t', temp)
    temp = re.sub(r'\t[ ]+\n', '\t\n', temp)
    temp = re.sub(r'\t[ ]+\t', '\t\t', temp)
    # 10개 이상 연속된 탭/개행을 구분자로 분할
    chunks = re.split(r'[\t\n]{10,}', temp)
    body = ""
    for chunk in chunks:
        if len(chunk) > len(body) and (not keyword or keyword in chunk):
            body = chunk
    body = body.strip()

    print(f"  [기사] {date_text} | {title} | {url}")

    # 본문이 너무 짧으면 skip
    #if len(body) < 50 and len(title) < 10:
    if len(body) < 50:
        return None

    # 본문 길이 제한 (너무 길면 LLM에 부담)
    #body = body[:2000]
    body = body[:4000]

    return {"title": title, "body": body, "date": date_text}


def analyze_sentiment(company: str, title: str, body: str) -> str:
    """
    llama.cpp (port 8000) EXAONE-3.5-32B로 기사 감성 분석.
    '긍정', '부정', '중립' 중 하나를 반환.
    """
    prompt = (
        f"다음은 '{company}' 회사에 관한 뉴스 기사입니다.\n"
        f"이 기사가 해당 회사에 긍정적인지, 부정적인지, 중립적인지 판단하세요.\n"
        f"반드시 아래 세 단어 중 하나만 답하세요: 긍정, 부정, 중립\n\n"
        f"제목: {title}\n"
        f"내용: {body}\n\n"
        f"답변:"
    )

    try:
        resp = requests.post(
            LLAMA_CPP_URL,
            json={
                "model": MODEL_NAME,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 20,
            },
            timeout=120,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        if "긍정" in text:
            return "긍정"
        elif "부정" in text:
            return "부정"
        else:
            return "중립"
    except Exception as e:
        print(f"  [경고] llama.cpp 오류: {e}")
        return "중립"


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit("오류: CLIENT_ID 또는 CLIENT_SECRET 환경 변수가 설정되지 않았습니다.")

    print(f"=== CHASE 뉴스 감성 분석 ({TODAY}) ===")
    print(f"모델: {MODEL_NAME}")
    print(f"출력: {OUTPUT_CSV}")
    print()

    companies = load_companies(XLS_FILE)
    print(f"총 {len(companies)}개 회사 로드 완료\n")

    # 이미 처리된 회사는 건너뜀 (재실행 시 이어쓰기)
    done_companies: set[str] = set()
    try:
        with open(OUTPUT_CSV, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader, None)  # 헤더 skip
            for row in reader:
                if row:
                    done_companies.add(row[0])
        print(f"기존 결과 {len(done_companies)}개 회사 발견 → 이어쓰기 모드\n")
    except FileNotFoundError:
        # 새 파일 생성 — 헤더 작성
        with open(OUTPUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["회사명", "종목코드", "긍정기사수", "부정기사수", "중립기사수"])

    total = len(companies)
    csv_lock = threading.Lock()

    def process_company(idx: int, name: str, code: str) -> None:
        # 스레드 최초 실행 시 개별 버퍼 초기화 및 worker ID 부여
        if not hasattr(_thread_local, "buffer"):
            with _worker_counter_lock:
                _thread_local.worker_id = next(_worker_counter)
            _thread_local.buffer = io.StringIO()
            with _thread_outputs_lock:
                _thread_outputs[_thread_local.worker_id] = _thread_local.buffer

        print(f"[{idx}/{total}] {name} ({code})", flush=True)

        # 1) 기사 URL 수집
        article_urls = get_article_urls(name)
        print(f"  → URL {len(article_urls)}개 발견 [{name}]", flush=True)

        # 2) 기사 본문 추출 (오늘 날짜 기사만)
        # URL이 최신순으로 정렬되어 있으므로, 오늘 날짜가 아닌 기사가 나오면 이후는 모두 과거 → 중단
        articles = []
        for u, pub_date in article_urls:
            art = fetch_article(u, pub_date, keyword=name)
            if art is _PAST_TODAY:
                break
            if art is not None:
                articles.append(art)
            time.sleep(ARTICLE_DELAY)

        print(f"  → 오늘 기사 {len(articles)}건 [{name}]", flush=True)

        # 3) 감성 분석
        pos = neg = neu = 0
        for i, art in enumerate(articles, 1):
            sentiment = analyze_sentiment(name, art["title"], art["body"])
            if sentiment == "긍정":
                pos += 1
            elif sentiment == "부정":
                neg += 1
            else:
                neu += 1
            words = art["body"].split()
            word_idx = next((wi for wi, w in enumerate(words) if name in w), None)
            if word_idx is not None:
                label = " ".join(words[max(0, word_idx-10):word_idx+11])
            else:
                label = " ".join(words[:21])
            print(f"  [{name}][{i}] {sentiment}: {label}")

        # 4) CSV 기록 (lock으로 보호)
        with csv_lock:
            with open(OUTPUT_CSV, "a", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([name, code, pos, neg, neu])

        print(f"  [{name}] 결과: 긍정={pos}, 부정={neg}, 중립={neu}\n")

    pending = [
        (idx, name, code)
        for idx, (name, code) in enumerate(companies, 1)
        if name not in done_companies
    ]

    sys.stdout = _ThreadSplitStream()
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(process_company, idx, name, code): name
                       for idx, name, code in pending}
            for future in as_completed(futures):
                future.result()  # 예외 발생 시 재발생
    finally:
        sys.stdout = _real_stdout

    # 스레드별 출력 파일로 저장
    log_base = OUTPUT_CSV.replace(".csv", "")
    for worker_id, buf in sorted(_thread_outputs.items()):
        log_path = f"{log_base}_thread{worker_id}.log"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
        print(f"스레드 {worker_id} 출력 저장됨: {log_path}")

    print(f"=== 완료! → {OUTPUT_CSV} ===")


if __name__ == "__main__":
    main()
