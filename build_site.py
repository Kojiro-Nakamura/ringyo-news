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

def is_noise_text(line: str) -> bool:
    """日付のみ、会員誘導、おすすめ記事、広告キャンペーンなどのノイズ文を徹底判定"""
    l = line.strip()
    if len(l) < 8:
        return True
    # 日時・日付・時刻のみの行
    # 日時・日付・時刻のみの行、または日付時刻で終わる行
    if re.match(r'^(?:\d{4}[年/\-\.]\s*)?\d{1,2}[月/\-\.]\s*\d{1,2}日?\s*(?:\(?[\w曜]+\)?)?\s*(?:\d{1,2}:\d{2}(?::\d{2})?)?$', l):
        return True
    if re.match(r'^\d{1,2}:\d{2}(?::\d{2})?$', l):
        return True
    if re.search(r'\d{1,2}/\d{1,2}\s*(?:\([^\)]+\))?\s*\d{1,2}:\d{2}', l) and len(l) < 30:
        return True
    # 有料・会員・購読案内
    if any(k in l for k in [
        '会員限定', '有料会員', '有料コンテンツ', '読み放題', '初月無料', '月額', '税込',
        '残り', '全文', 'ログイン', '無料体験', 'お申し込み', '購読', '会員登録', '有料プラン'
    ]):
        return True
    # 広告・レコメンド・ランキング・キャンペーン
    if any(k in l for k in [
        'あなたにおすすめ', 'おすすめ記事', 'おすすめ情報', '関連記事', '他の記事', 'アクセスランキング', '最新ニュース',
        'ニュース一覧', '一覧へ', 'もっと見る', '円引き', '割引き', 'プレゼント', '抽選', '懸賞',
        'クーポン', 'キャンペーン', 'タイアップ', 'PR記事'
    ]):
        return True
    # システム・規約・UI
    if any(k in l for k in [
        'JavaScript', 'Cookie', 'クッキー', '利用規約', '無断転載', '禁無断転載',
        'プライバシーポリシー', 'お問い合わせ', 'サイトマップ', 'シェア', 'ツイート',
        'LINEで送る', 'ブックマーク', 'フォトギャラリー', '写真を見る', '再生ボタン'
    ]):
        return True
    return False

def rescue_meta_desc(raw_desc: str) -> str:
    """末尾が…で切れているメタディスクリプションから、完全な文（「。」で終わる文）を救出"""
    if not raw_desc or is_garbled_text(raw_desc):
        return ''
    raw_desc = raw_desc.strip()
    if not (raw_desc.endswith('…') or raw_desc.endswith('...') or raw_desc.endswith('..')):
        return raw_desc
    # 句点「。」で分割して完全な文だけを救出
    parts = re.split(r'。', raw_desc)
    valid_parts = []
    for p in parts:
        p = p.strip()
        if not p or p.endswith(('…', '...', '..')):
            continue
        if len(p) >= 15 and not is_noise_text(p) and not is_garbled_text(p):
            valid_parts.append(p + '。')
    if valid_parts:
        return ''.join(valid_parts)
    return ''

def extract_meaningful_tokens(text: str) -> set:
    """漢字熟語（2文字以上）、カタカナ語（3文字以上）、英単語（3文字以上）を意味のある語彙として抽出"""
    kanji = re.findall(r'[\u4e00-\u9fa5]{2,}', text)
    katakana = re.findall(r'[\u30a0-\u30ff]{3,}', text)
    alpha = re.findall(r'[a-zA-Z]{3,}', text)
    tokens = set(katakana + alpha)
    for k in kanji:
        tokens.add(k)
        for i in range(len(k) - 1):
            tokens.add(k[i:i+2])
    return tokens

def is_relevant_to_title(title: str, sentence: str) -> bool:
    """文がタイトルと文脈上の関連（実質的な語彙・熟語の一致）を持つか検証し、無関係な別記事や広告を完全排除"""
    t_tokens = extract_meaningful_tokens(title)
    if not t_tokens:
        return True
    s_tokens = extract_meaningful_tokens(sentence)
    common = t_tokens.intersection(s_tokens)
    return len(common) >= 1

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
        raw_desc = meta_desc['content'].strip() if meta_desc and meta_desc.get('content') else ''
        desc = rescue_meta_desc(raw_desc)
        
        # 不要なタグ・セクションの徹底分解・削除
        for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'noscript', 'form', 'figure', 'figcaption']):
            tag.decompose()

        # 広告・レコメンド・ランキング・会員限定誘導・サイドバーのコンテナを全削除
        noise_pattern = re.compile(
            r'(recommend|related|ranking|paywall|member|subscription|banner|ad-|sidebar|sns|social|comment|breadcrumb|share|pickup|popular|follow|entry-date|post-date|campaign)',
            re.I
        )
        for tag in soup.find_all(attrs={'class': noise_pattern}):
            tag.decompose()
        for tag in soup.find_all(attrs={'id': noise_pattern}):
            tag.decompose()

        body_soup = soup.body or soup

        # 本文コンテナの特定（記事本文らしい専用クラスを最優先）
        main_container = None
        for candidate in [
            body_soup.select_one('div.limited_main_text'),
            body_soup.select_one('div.news_item'),
            body_soup.find(['article', 'main']),
            body_soup.find(['div', 'section'], class_=re.compile(r'^(?!.*(css|js|style|grid)).*(entry-content|article-body|main-text|post-content|story-body)', re.I)),
            body_soup.find(['div', 'section'], id=re.compile(r'^(?!.*(css|js|style|grid)).*(entry-content|article-body|main-text|post-content)', re.I)),
            body_soup
        ]:
            if candidate and len(candidate.get_text().strip()) > 60:
                main_container = candidate
                break

        if not main_container:
            main_container = body_soup
            
        paragraphs = []
        for p in main_container.find_all('p'):
            text = p.get_text().strip()
            if len(text) > 20 and not is_noise_text(text) and not is_garbled_text(text):
                if text not in paragraphs:
                    paragraphs.append(text)
            if len(paragraphs) >= 6:
                break

        if len(paragraphs) < 3:
            raw_lines = [line.strip() for line in main_container.get_text().splitlines() if len(line.strip()) > 15]
            for line in raw_lines:
                if is_noise_text(line) or is_garbled_text(line):
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
        # ノイズ文（日付、会員誘導、おすすめ記事、広告など）はスキップ
        if is_noise_text(s):
            continue
        # タイトルとの関連性が全くない文（無関係な別記事や広告リンク）はスキップ
        if not is_relevant_to_title(title, s):
            continue
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

def guess_source_from_url(url: str, default: str = "WEB") -> str:
    """URLドメインから報道メディア・企業名を推定"""
    domain = urllib.parse.urlparse(url).netloc.lower()
    source_map = {
        'yahoo.co.jp': 'Yahoo!ニュース',
        's-housing.jp': '新建ハウジング',
        'jiji.com': '時事通信',
        'prtimes.jp': 'PR TIMES',
        'shinmai.co.jp': '信濃毎日新聞',
        'agara.co.jp': '紀伊民報',
        '373news.com': '南日本新聞',
        'hokkaido-np.co.jp': '北海道新聞',
        'kachimai.jp': '十勝毎日新聞',
        'saga-s.co.jp': '佐賀新聞',
        'sanin-chuo.jp': '山陰中央新報',
        'sakigake.jp': '秋田魁新報',
        'atpress.ne.jp': '@Press',
        'chunichi.co.jp': '中日新聞',
        'asahi.com': '朝日新聞',
        'yomiuri.co.jp': '読売新聞',
        'mainichi.jp': '毎日新聞',
        'nikkei.com': '日本経済新聞',
        'sankei.com': '産経新聞',
        'kyodo.co.jp': '共同通信',
        'kobe-np.co.jp': '神戸新聞',
        'chugoku-np.co.jp': '中国新聞',
        'kahoku.news': '河北新報',
        'kushironews.jp': '釧路新聞',
        'sfc.jp': '住友林業',
        'amazon.com': 'Amazon',
        'kobe-journal.com': '神戸ジャーナル',
        'dellows.jp': 'デローズニュース',
        'j-times.jp': '上越タイムス'
    }
    for d, name in source_map.items():
        if d in domain:
            return name
    return domain.replace('www.', '') if domain else default

def process_article_item(real_url: str, title: str, source: str, article_date: str, published_str: str, gemini_key: str) -> Optional[Dict[str, Any]]:
    """単一記事の本文取得・要約・カテゴリ分類を実行"""
    desc, body = fetch_article_content(real_url)
    
    if not is_ringyo_relevant(title, desc + " " + body):
        return None
        
    category = classify_category(title, desc + " " + body)
    
    summary_type = 'auto'
    summary_text = ""
    if gemini_key:
        ai_sum = generate_summary_with_gemini(title, desc + "\n" + body, gemini_key)
        if ai_sum:
            summary_text = ai_sum
            summary_type = 'ai'
            
    if not summary_text:
        summary_text = create_auto_summary(title, desc, body)
        summary_type = 'auto'
        
    return {
        "article_date": article_date,
        "title": title,
        "url": real_url,
        "source": source,
        "published_at": published_str,
        "summary": summary_text,
        "summary_type": summary_type,
        "category": category
    }

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
    return process_article_item(real_url, clean_title, source, article_date, published_str, gemini_key)

def fetch_ringyou_blog_items(days_limit: int = 7) -> List[Dict[str, Any]]:
    """林業専門ブログ（https://ringyou-news.blogspot.com/）からキュレーションされた記事リストを取得"""
    feed_url = 'https://ringyou-news.blogspot.com/feeds/posts/default'
    print(f"林業ニュース(Blogspot) からフィード取得中: {feed_url}")
    try:
        feed = feedparser.parse(feed_url)
    except Exception as e:
        print(f"Blogspot feed error: {e}")
        return []
    
    blog_items = []
    seen_urls = set()
    
    for entry in feed.entries[:days_limit]:
        post_title = entry.title
        m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', post_title)
        if m:
            entry_date = f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
        elif hasattr(entry, 'published_parsed') and entry.published_parsed:
            dt_utc = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            entry_date = dt_utc.astimezone(JST).strftime('%Y-%m-%d')
        else:
            entry_date = datetime.now(JST).strftime('%Y-%m-%d')
            
        html = entry.content[0].value if hasattr(entry, 'content') else entry.summary
        soup = BeautifulSoup(html, 'html.parser')
        
        for a in soup.find_all('a'):
            href = a.get('href', '').strip()
            if not href or not href.startswith('http') or 'blogspot.com' in href:
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)
                
            title = ''
            parent = a.parent
            prev = parent.find_previous_sibling()
            if prev:
                t = prev.get_text().strip()
                if t and not t.startswith('http'):
                    title = t
            if not title:
                lt = a.get_text().strip()
                if lt and not lt.startswith('http'):
                    title = lt
            if not title:
                parent_text = parent.get_text().strip()
                lines = [l.strip() for l in parent_text.splitlines() if l.strip() and not l.strip().startswith('http')]
                if lines:
                    title = lines[0]
                    
            if title and href:
                source = guess_source_from_url(href, default="林業ニュース")
                blog_items.append({
                    'title': title,
                    'url': href,
                    'source': source,
                    'article_date': entry_date,
                    'published_at': f'{entry_date} 00:30'
                })
                
    print(f"林業ニュース(Blogspot) から {len(blog_items)} 件の記事候補を抽出しました。")
    return blog_items

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

    # 既知のURLセットを作成
    known_urls = set()
    for d, items in articles_by_date.items():
        for item in items:
            known_urls.add(item.get("url"))
            for r in item.get("related_articles", []):
                known_urls.add(r.get("url"))

    # 1. Google Newsから全国の林業・木材産業ニュースを取得
    queries = [
        "林業",
        "木材産業 OR 国産材",
        "森林組合 OR 林野庁",
        "製材 OR 木造建築 OR CLT",
        "森林環境税 OR 再造林"
    ]
    seen_links = set()
    raw_google_entries = []
    
    for q in queries:
        encoded = urllib.parse.quote(q)
        rss_url = f"https://news.google.com/rss/search?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
        try:
            feed = feedparser.parse(rss_url)
            for e in feed.entries[:20]:
                link = e.get('link')
                if link and link not in seen_links:
                    seen_links.add(link)
                    raw_google_entries.append(e)
        except Exception as ex:
            print(f"Google RSS fetch error ({q}): {ex}")

    # 2. 林業ニュース専門ブログ（ringyou-news.blogspot.com）から取得
    blog_items = fetch_ringyou_blog_items(days_limit=7)
    # 既に登録済みのURLはスキップして高速化
    new_blog_items = [it for it in blog_items if it.get('url') not in known_urls]

    print(f"情報ソース取得完了: Google News {len(raw_google_entries)} 件, 林業ニュース(Blogspot) {len(new_blog_items)} 件（新規）")
    print("並列解析および要約生成を開始...")
    
    new_articles = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = []
        # Google News のタスク
        for entry in raw_google_entries:
            futures.append(executor.submit(process_single_entry, entry, gemini_key))
        # Blogspot のタスク
        for b_item in new_blog_items:
            futures.append(executor.submit(
                process_article_item,
                b_item['url'],
                b_item['title'],
                b_item['source'],
                b_item['article_date'],
                b_item['published_at'],
                gemini_key
            ))

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
