import os
import re
import json
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional, Tuple, Any

from difflib import SequenceMatcher
import feedparser
import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(BASE_DIR, "docs")
DATA_DIR = os.path.join(DOCS_DIR, "data")
DATES_DIR = os.path.join(DATA_DIR, "dates")

# 林業・木材産業関連のキーワード（関連性の低いニュースを弾くフィルター）
RINGYO_KEYWORDS = [
    "林業", "木材", "製材", "林野庁", "森林", "森林組合", "国産材", 
    "木造", "丸太", "原木", "CLT", "木質バイオマス", "スギ", "ヒノキ", 
    "間伐", "植林", "造林", "森林環境税", "木育", "伐採", "合板", 
    "林道", "林業機械", "スマート林業", "主伐", "皆伐", "再造林", 
    "ウッドショック", "J-クレジット", "木づかい", "森林管理", "治山"
]

def is_ringyo_relevant(title: str, text: str) -> bool:
    """林業・木材産業関連のニュースであるかを厳密に判定"""
    combined = title + " " + text
    return any(kw in combined for kw in RINGYO_KEYWORDS)

def decode_google_news_url(google_rss_url: str) -> str:
    """Google News RSSの暗号化URLから実際の記事URLを復元"""
    try:
        headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
        }
        resp = requests.get(google_rss_url, headers=headers, timeout=6)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, 'html.parser')
        container = soup.select_one('c-wiz[data-p]')
        if not container:
            return google_rss_url
        
        data_p = container.get('data-p')
        obj = json.loads(data_p.replace('%.@.', '["garturlreq",'))

        payload = {
            'f.req': json.dumps([[
                ['Fbv4je', json.dumps(obj[:-6] + obj[-2:]), 'null', 'generic']
            ]])
        }
        
        api_url = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
        api_headers = {
            'content-type': 'application/x-www-form-urlencoded;charset=UTF-8',
            'user-agent': headers['user-agent']
        }

        response = requests.post(api_url, headers=api_headers, data=payload, timeout=6)
        response.raise_for_status()

        raw_text = response.text.replace(")]}'", "")
        response_json = json.loads(raw_text)
        
        array_string = response_json[0][2]
        article_url = json.loads(array_string)[1]
        return article_url
    except Exception:
        return google_rss_url

def is_garbled_text(text: str) -> bool:
    """UTF-8バイト列がISO-8859-1等で誤読されたような文字化け（æ, ã, é等の連続、日本語の欠如）を検知"""
    if not text:
        return False
    jp_chars = len(re.findall(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fa5]', text))
    mojibake_chars = len(re.findall(r'[æøåãðþïéèàù§©®]', text))
    if mojibake_chars >= 2 and jp_chars < 5:
        return True
    return False

def clean_for_dedup(s: str) -> str:
    """文の重複・類似比較用に記号・空白を除去"""
    return re.sub(r'[\s\u3000「」『』【】（）()・…、。！？!?\-\|:：]', '', s).lower()

def fetch_article_content(url: str) -> Tuple[str, str]:
    """元記事のメタディスクリプションと本文テキストを取得（文字化け自動補正＆br区切り対応）"""
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=6)
        resp.raise_for_status()

        # requestsのISO-8859-1誤認による文字化けを防止
        if not resp.encoding or resp.encoding.lower() in ('iso-8859-1', 'latin1', 'us-ascii'):
            resp.encoding = resp.apparent_encoding or 'utf-8'

        soup = BeautifulSoup(resp.text, 'html.parser')
        
        meta_desc = soup.find('meta', attrs={'name': 'description'}) or soup.find('meta', attrs={'property': 'og:description'})
        desc = meta_desc['content'].strip() if meta_desc and meta_desc.get('content') else ''
        # 末尾が … や ... で途切れている不完全なディスクリプションは破棄して本文を優先
        if desc.endswith('…') or desc.endswith('...') or len(desc) < 30 or is_garbled_text(desc):
            desc = ''
        
        for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'noscript']):
            tag.decompose()

        bad_words = [
            'JavaScript', 'クッキー', 'Cookie', '利用規約', '無断転載', '禁無断転載',
            '有料会員', 'ログイン', '本文へスキップ', '閲覧支援', '文字サイズ', 'サイトマップ',
            'サイト内検索', 'プライバシーポリシー', 'お問い合わせ', 'ページ先頭', '報道発表日', '更新日',
            '本プレスリリースの内容は', 'プレスリリース一覧', '電話', 'TEL', 'FAX', 'メールアドレス',
            'アンケート', '質問1', '質問2', 'お問合せ先', '外部リンク', '再生ボタン', '担当者名', '係'
        ]

        # 本文領域を優先探索
        main_container = soup.find(['article', 'main']) or soup.find(id=re.compile(r'content|main|article', re.I)) or soup.find(class_=re.compile(r'entry|content|main|article', re.I)) or soup
            
        paragraphs = []
        raw_lines = [line.strip() for line in main_container.get_text().splitlines() if len(line.strip()) > 15]
        for line in raw_lines:
            if any(bad in line for bad in bad_words) or is_garbled_text(line):
                continue
            line_norm = clean_for_dedup(line)
            if not any(SequenceMatcher(None, clean_for_dedup(p), line_norm).ratio() > 0.65 for p in paragraphs):
                paragraphs.append(line)
            if len(paragraphs) >= 8:
                break
                
        body_text = "\n".join(paragraphs[:6])
        return desc, body_text
    except Exception:
        return "", ""

def classify_category(title: str, text: str) -> str:
    """林業・木材産業のカテゴリ分類"""
    combined = (title + " " + text).lower()
    
    categories = [
        ("林政・施策", ["林野庁", "森林環境税", "補助金", "森林法", "計画", "施策", "審議会", "知事", "自治体", "交付金", "税"]),
        ("建築・木材利用", ["木造", "clt", "中高層", "建築", "木質空間", "非住宅", "公共建築", "住宅", "建材", "内装", "木育"]),
        ("流通・市場", ["木材価格", "原木", "丸太", "製材", "輸出", "輸入", "合板", "市況", "セリ", "共販", "市場", "企業", "新商品"]),
        ("スマート林業・技術", ["ドローン", "機械", "高性能", "ict", "自動化", "スマート林業", "gps", "安全", "バイオマス", "技術", "研究", "開発"]),
        ("森林管理・地域", ["間伐", "造林", "再造林", "植林", "森林組合", "治山", "j-クレジット", "カーボン", "間伐材", "花粉", "苗木", "獣害", "鹿"])
    ]
    
    for cat_name, keywords in categories:
        for kw in keywords:
            if kw.lower() in combined:
                return cat_name
    return "業界・一般"

def generate_summary_with_gemini(title: str, body: str, api_key: str) -> Optional[str]:
    """Gemini APIによる要約生成（利用可能な場合）"""
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = f"""以下の林業・木材産業に関するニュース記事を、業界人が30秒で理解できるように「3つの要点」に要約してください。
必ず以下のフォーマットで出力してください。挨拶や前置き、解説は一切不要です。

【タイトル】{title}
【内容】{body}

出力形式：
・（要点1：50字以内）
・（要点2：50字以内）
・（要点3：50字以内）"""

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        if response and response.text:
            return response.text.strip()
    except Exception as e:
        print(f"Gemini API Error: {e}")
    return None

def create_auto_summary(title: str, desc: str, body: str) -> str:
    """自動要約生成（重複文・タイトル重複を徹底排除）"""
    base_text = (desc + "\n" + body).strip() if (desc and body) else (desc if desc else body)
    if not base_text or is_garbled_text(base_text):
        return f"・{title}"
    clean = re.sub(r'【写真を見る】.*', '', base_text)
    clean = re.sub(r'（.*放送）', '', clean)
    clean = re.sub(r'https?://\S+', '', clean)
    clean = clean.strip()

    title_norm = clean_for_dedup(title)
    raw_sentences = [s.strip() for s in re.split(r'。|\n', clean) if len(s.strip()) > 10 and not is_garbled_text(s)]

    unique_sentences: List[str] = []
    for s in raw_sentences:
        # 末尾が … や ... で切れている不完全な文はスキップ
        if s.endswith('…') or s.endswith('...') or s.endswith('..'):
            continue
        s_norm = clean_for_dedup(s)
        if len(s_norm) < 10:
            continue
        # タイトルと類似（70%以上一致、またはタイトルの包含）する文をスキップ
        if SequenceMatcher(None, title_norm, s_norm).ratio() > 0.70:
            continue
        if s_norm in title_norm or (len(s_norm) < 60 and title_norm in s_norm):
            continue
        # 既に選んだ要約文と類似（60%以上一致）する文をスキップ
        if any(SequenceMatcher(None, clean_for_dedup(u), s_norm).ratio() > 0.60 for u in unique_sentences):
            continue
        unique_sentences.append(s)
        if len(unique_sentences) >= 3:
            break

    def finish_sentence(text: str) -> str:
        t = text.strip()
        if not t.endswith(('。', '！', '!', '？', '?')):
            return t + '。'
        return t

    if len(unique_sentences) >= 2:
        return "\n".join(f"・{finish_sentence(s)}" for s in unique_sentences[:3])
    elif len(unique_sentences) == 1:
        return f"・{finish_sentence(unique_sentences[0])}"
    else:
        return f"・{title}"

def process_single_entry(entry: Any, gemini_key: str) -> Optional[Dict[str, Any]]:
    google_url = entry.get('link', '')
    if not google_url:
        return None
    if hasattr(entry, 'published_parsed') and entry.published_parsed:
        dt_utc = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        dt_jst = dt_utc.astimezone(JST)
        article_date = dt_jst.strftime('%Y-%m-%d')
        published_str = dt_jst.strftime('%Y-%m-%d %H:%M')
    else:
        now_jst = datetime.now(JST)
        article_date = now_jst.strftime('%Y-%m-%d')
        published_str = now_jst.strftime('%Y-%m-%d %H:%M')
        
    raw_title = entry.get('title', '').strip()
    source = entry.get('source', {}).get('title', 'ニュース配信元')
    clean_title = re.sub(r' - [^-]+$', '', raw_title)
    
    real_url = decode_google_news_url(google_url)
    desc, body = fetch_article_content(real_url)
    
    if not is_ringyo_relevant(clean_title, desc + " " + body):
        return None
        
    category = classify_category(clean_title, desc + " " + body)
    
    summary_type = 'auto'
    summary_text = ""
    if gemini_key:
        ai_sum = generate_summary_with_gemini(clean_title, desc + "\n" + body, gemini_key)
        if ai_sum:
            summary_text = ai_sum
            summary_type = 'ai'
            
    if not summary_text:
        summary_text = create_auto_summary(clean_title, desc, body)
        summary_type = 'auto'
        
    return {
        "article_date": article_date,
        "title": clean_title,
        "url": real_url,
        "source": source,
        "published_at": published_str,
        "summary": summary_text,
        "summary_type": summary_type,
        "category": category
    }

def clean_title_for_clustering(title: str) -> str:
    """タイトルの記号やメディア表記を除去して比較用文字列を作成"""
    t = re.sub(r'[\(（【\[].*?[\)）】\]]', '', title)
    t = re.sub(r'\s*-\s*[^-]+$', '', t)
    t = re.sub(r'\s*\|\s*[^|]+$', '', t)
    t = re.sub(r'[「」『』【】（）()・…、。！？!?\s]', '', t)
    return t

def extract_title_keywords(title: str) -> set:
    """タイトルから2文字以上の重要キーワードを抽出"""
    words = set(re.findall(r'[\u4e00-\u9fa5]{2,}|[\u30a1-\u30f6]{2,}|[A-Za-z0-9]{2,}', title))
    stopwords = {"ニュース", "詳細", "放送", "速報", "今日", "昨日", "全国", "日本", "株式会社", "有限会社"}
    return words - stopwords

def are_articles_same_topic(a1: dict, a2: dict) -> bool:
    """2つの記事が同一の林業・木材産業トピックを報じているか判定"""
    t1 = clean_title_for_clustering(a1.get('title', ''))
    t2 = clean_title_for_clustering(a2.get('title', ''))
    
    if t1 and t2 and (t1 in t2 or t2 in t1):
        return True
        
    ratio = SequenceMatcher(None, t1, t2).ratio()
    if ratio >= 0.50:
        return True
        
    kw1 = extract_title_keywords(a1.get('title', ''))
    kw2 = extract_title_keywords(a2.get('title', ''))
    common = kw1 & kw2
    if len(common) >= 3:
        return True
        
    return False

def cluster_articles(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同じトピックの記事を1つの代表記事にまとめ、他を related_articles に格納"""
    clusters: List[Dict[str, Any]] = []
    for item in articles:
        existing_related = item.pop("related_articles", [])
        candidates = [item] + existing_related
        
        for cand in candidates:
            matched = False
            for group in clusters:
                if are_articles_same_topic(group, cand):
                    urls = {group.get("url")} | {r.get("url") for r in group.get("related_articles", [])}
                    if cand.get("url") not in urls:
                        group.setdefault("related_articles", []).append({
                            "title": cand.get("title"),
                            "url": cand.get("url"),
                            "source": cand.get("source"),
                            "published_at": cand.get("published_at")
                        })
                    matched = True
                    break
            if not matched:
                cand["related_articles"] = []
                clusters.append(cand)
                
    return clusters

def build_static_site():
    print("=== 全国の林業・木材産業デイリーニュース 構築開始 ===")
    os.makedirs(DATES_DIR, exist_ok=True)
    
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    
    # 既存のdocs/data/dates/*.jsonがあればロード
    articles_by_date = {}
    if os.path.exists(DATES_DIR):
        for fn in os.listdir(DATES_DIR):
            if fn.endswith(".json"):
                date_str = fn.replace(".json", "")
                fpath = os.path.join(DATES_DIR, fn)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                        articles_by_date[date_str] = existing
                except Exception as e:
                    print(f"Error loading {fn}: {e}")

    # Google Newsから全国の林業・木材産業ニュースを取得
    queries = [
        "林業",
        "木材産業 OR 国産材",
        "森林組合 OR 林野庁",
        "製材 OR 木造建築 OR CLT",
        "森林環境税 OR 再造林"
    ]
    seen_links = set()
    raw_entries = []
    
    for q in queries:
        encoded = urllib.parse.quote(q)
        rss_url = f"https://news.google.com/rss/search?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
        feed = feedparser.parse(rss_url)
        for e in feed.entries[:20]:
            link = e.get('link')
            if link and link not in seen_links:
                seen_links.add(link)
                raw_entries.append(e)

    print(f"Google Newsから {len(raw_entries)} 件の候補を取得。並列解析中...")
    
    new_articles = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(process_single_entry, entry, gemini_key) for entry in raw_entries]
        for f in as_completed(futures):
            try:
                res = f.result()
                if res:
                    new_articles.append(res)
            except Exception as ex:
                print(f"Entry error: {ex}")

    # 日付ごとにマージ
    new_added_count = 0
    for a in new_articles:
        d = a["article_date"]
        if d not in articles_by_date:
            articles_by_date[d] = []
        # URL重複チェック
        existing_urls = {item["url"] for item in articles_by_date[d]}
        if a["url"] not in existing_urls:
            articles_by_date[d].append(a)
            new_added_count += 1

    print(f"新着追加記事数: {new_added_count} 件")

    # 各日付のJSONファイルを保存
    date_list = []
    total_articles = 0
    all_articles_index = []

    for d in sorted(articles_by_date.keys(), reverse=True):
        raw_items = articles_by_date[d]
        raw_items.sort(key=lambda x: x.get("published_at", ""), reverse=True)
        # 同一トピックの記事をグルーピング
        items = cluster_articles(raw_items)
        
        date_file = os.path.join(DATES_DIR, f"{d}.json")
        with open(date_file, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
            
        # 記事総数（関連報道含む）
        day_total = sum(1 + len(x.get("related_articles", [])) for x in items)
        total_articles += day_total
        date_list.append({
            "article_date": d,
            "count": len(items)  # 表示されるトピック数
        })
        
        for item in items:
            if len(all_articles_index) < 150:
                all_articles_index.append(item)

    # index.json を保存
    index_data = {
        "dates": date_list,
        "stats": {
            "total_articles": total_articles,
            "total_days": len(date_list),
            "last_updated": datetime.now(JST).isoformat()
        }
    }
    with open(os.path.join(DATA_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)

    with open(os.path.join(DATA_DIR, "search_index.json"), "w", encoding="utf-8") as f:
        json.dump(all_articles_index, f, ensure_ascii=False, indent=2)

    print(f"=== 構築完了: 全 {total_articles} 件、{len(date_list)} 日分のアーカイブを生成 ===")

if __name__ == "__main__":
    build_static_site()
