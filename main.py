# DESC: Tver 목록
# NUM: 3
# TAGS: 티버, 목록
#!/usr/bin/env python3
import json, re, requests, subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

subprocess.run(["pkill", "-f", "termux-media-scan"], check=False)

OUT_PATH    = "/storage/emulated/0/Movies/tver_manager.html"
MAX_WORKERS = 16
TIMEOUT     = 15

# 원하는 재확인 총 횟수 (예: 2번 확인 후 고정)
TARGET_CHECK_COUNT = 2

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "x-tver-platform-type": "web",
    "Origin": "https://tver.jp",
    "Referer": "https://tver.jp/",
    "Accept": "application/json",
}

EXCLUDE_KW = []
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

MAIN_CATS = [
    ("drama",   "드라마"),
    ("variety", "예능"),
]
EXTRA_CATS = [
    ("news_documentary", "보도/다큐"),
    ("anime",            "애니메"),
    ("sports",           "스포츠"),
    ("music",            "음악"),
    ("other",            "기타"),
]

def broadcast_sortkey(label):
    m = re.search(r'(\d+)月(\d+)[日일]', label or "")
    return int(m.group(1)) * 100 + int(m.group(2)) if m else 0

def end_label(end_at):
    if not end_at: return "", ""
    now  = datetime.now().timestamp()
    diff = end_at - now
    if diff <= 0: return "配信終了", ""
    dt = datetime.fromtimestamp(end_at)
    wd = WEEKDAY_JP[dt.weekday()]
    sub = f"{dt.month}月{dt.day}日({wd}){dt.hour}:{dt.minute:02d}"
    if diff > 7 * 86400: return "1週間以上", sub
    days = int(diff / 86400)
    if days >= 1: return f"{days}日", sub
    return f"{int(diff/3600)}時間", sub

def fetch_tag(session, slug, dedup_series=True):
    r = session.get(
        f"https://service-api.tver.jp/api/v1/callTagSearch/{slug}",
        headers=BASE_HEADERS, timeout=TIMEOUT
    )
    r.raise_for_status()
    contents = r.json().get("result", {}).get("contents", [])
    items = []
    now = datetime.now().timestamp()

    latest_lbl = {}  
    for item in contents:  
        c   = item.get("content") or {}  
        sid = c.get("seriesID") or ""  
        lbl = (c.get("broadcastDateLabel") or "").strip()  
        if lbl and sid not in latest_lbl:  
            latest_lbl[sid] = lbl  

    for item in contents:  
        c   = item.get("content") or {}  
        sid = c.get("seriesID") or ""  
        st  = (c.get("seriesTitle") or "").strip()  
        et  = (c.get("title") or "").strip()  
        lbl = (c.get("broadcastDateLabel") or "").strip()  

        if "解説放送" in et or "解説放送" in st: continue  
        if not lbl: continue  
        if dedup_series and lbl != latest_lbl.get(sid): continue  

        end_ts = int(c.get("endAt") or 0)  
        is_recent = True  
        if end_ts > 0 and (now - end_ts) > (30 * 86400):  
            is_recent = False  

        em, es = end_label(end_ts)  
        items.append({  
            "id":          c.get("id") or "",  
            "version":     c.get("version"),  
            "seriesID":    sid,  
            "seriesTitle": st,  
            "title":       et,  
            "description": "",  
            "broadcaster": (c.get("broadcasterName") or c.get("channelName") or "").strip(),  
            "label":       lbl.replace("放送分", "").strip(),  
            "startAt":     int(c.get("startAt") or 0),  
            "year":        0,  
            "bkey":        broadcast_sortkey(lbl),  
            "end_main":    em,  
            "end_sub":     es,  
            "talents":     [],  
            "category":    slug,  
            "isSubtitle":  bool(c.get("isSubtitle", False)),  
            "isRecent":    is_recent  
        })  
    return items

def fetch_detail(session, item):
    eid, ver = item["id"], item.get("version")
    if not eid: return item
    try:
        r = session.get(
            f"https://statics.tver.jp/content/episode/{eid}.json",
            headers=BASE_HEADERS,
            params={"v": ver} if ver else None,
            timeout=TIMEOUT
        )
        data = r.json()
        item["description"] = (data.get("description") or "").strip()

        lbl_full = data.get("broadcastDateLabel", "")  
        year_m = re.search(r'(\d{4})年', lbl_full)  
        if year_m:  
            item["year"] = int(year_m.group(1))  
        else:  
            vs = data.get("viewStatus") or {}  
            start_at = int(vs.get("startAt") or 0)  
            if start_at > 0:  
                item["startAt"] = start_at  
                item["year"] = datetime.fromtimestamp(start_at).year  
    except: pass  
      
    try:  
        r2 = session.get(  
            f"https://contents-api.tver.jp/contents/api/v1/episodes/{eid}/talents",  
            headers=BASE_HEADERS, timeout=TIMEOUT  
        )  
        if r2.status_code == 200:  
            item["talents"] = [  
                {"id": t.get("id",""), "name": t.get("name","").strip(),  
                 "kana": t.get("name_kana","").strip(), "genre1": t.get("genre1","")}  
                for t in r2.json().get("talents", []) if t.get("id","").strip()  
            ]  
    except: pass  
    
    # 수집 완료 횟수 증가 (TARGET_CHECK_COUNT 까지만 제한)
    current_count = item.get("checkCount", 0)
    item["checkCount"] = min(current_count + 1, TARGET_CHECK_COUNT)
    return item

def load_existing_data():
    try:
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        m = re.search(r'const DATA\s*=\s*(\{.*?\});\s*const CAT_META', content, re.DOTALL)
        if m:
            return json.loads(m.group(1))
    except:
        pass
    return {}

def fetch_all():
    session = requests.Session()
    session.get("https://tver.jp/", headers=BASE_HEADERS, timeout=10)

    existing_data = load_existing_data()
    existing_map = {}
    for old_list in existing_data.values():
        for item in old_list:
            eid = item.get("id")
            if eid:
                existing_map[eid] = item

    results   = {}
    all_items = []  
    cat_meta  = []  

    for slug, label in MAIN_CATS:  
        print(f"[수집] {label}...")  
        try:  
            items = fetch_tag(session, slug)  
            print(f"       {len(items)}건")  
        except Exception as e:  
            print(f"       실패: {e}")  
            items = []  
        results[slug] = items  
        all_items.extend(items)  

    print("[수집] 추가 카테고리 탐색...")  
    for slug, label in EXTRA_CATS:  
        try:  
            items = fetch_tag(session, slug)  
            if items:  
                print(f"       {label}: {len(items)}건")  
                results[slug] = items  
                all_items.extend(items)  
                cat_meta.append({"slug": slug, "label": label, "count": len(items)})  
            else:  
                print(f"       {label}: 0건 (제외)")  
        except Exception as e:  
            print(f"       {label}: 실패({e}) (제외)")  

    # 상세 정보 수집 대상 분류 (신규이거나 재확인 횟수가 부족한 경우)
    need_detail_items = []
    for item in all_items:
        eid = item.get("id")
        if not eid: continue

        if eid in existing_map:
            old_item = existing_map[eid]
            item["checkCount"] = old_item.get("checkCount", TARGET_CHECK_COUNT)
            item["description"] = old_item.get("description", "")
            item["year"] = old_item.get("year", 0)
            item["talents"] = old_item.get("talents", [])
            
            if item["checkCount"] < TARGET_CHECK_COUNT:
                need_detail_items.append(item)
        else:
            item["checkCount"] = 0
            need_detail_items.append(item)

    print(f"[상세] 수집 대상 ({len(need_detail_items)}건 / 전체 {len(all_items)}건)...")  
    done = 0  
    if need_detail_items:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:  
            futures = {exe.submit(fetch_detail, session, item): item for item in need_detail_items}  
            for f in as_completed(futures):  
                f.result()  
                done += 1  
                if done % 20 == 0 or done == len(futures):  
                    print(f"       {done}/{len(futures)}", end="\r")  
        print()  

    # 최종 결과물은 원본 목록(results)을 기반으로 하되, 수집된 상세 정보와 기존 데이터를 병합
    final_results = {}
    for slug, items in results.items():
        merged_list = []
        for item in items:
            eid = item.get("id")
            if not eid: continue
            # 만약 방금 상세 수집을 안 거쳤고(기존 캐시 사용) 기존 데이터에 내용이 있다면 보존
            if eid in existing_map and not item.get("description"):
                old_item = existing_map[eid]
                item["description"] = old_item.get("description", "")
                item["year"] = old_item.get("year", 0)
                item["talents"] = old_item.get("talents", [])
                item["checkCount"] = old_item.get("checkCount", TARGET_CHECK_COUNT)
            merged_list.append(item)
        final_results[slug] = merged_list

    return final_results, cat_meta

HTML = r"""<!DOCTYPE html>
<html lang="ja">  
<head>  
<meta charset="UTF-8">  
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">  
<title>TVer Manager</title>  
<style>  
:root{--bg:#0f0f0f;--card:#1a1a1a;--border:#2a2a2a;--accent:#e6002d;--t1:#f0f0f0;--t2:#bbb;--t3:#777;}  
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}  
body{background:var(--bg);color:var(--t1);font-family:-apple-system,'Hiragino Sans',sans-serif;padding-bottom:40px;}  
.cmgr-item{display:flex;align-items:center;gap:8px;padding:10px 16px;border-bottom:1px solid #222;}  
.cmgr-item.active{background:#001a3a;}  
.cmgr-name{flex:1;font-size:0.85rem;color:#ccc;cursor:pointer;}  
.cmgr-name:hover{color:#4af;}  
.cmgr-latest{font-size:0.72rem;color:#555;white-space:nowrap;}  
.cmgr-del{background:none;border:none;color:#555;font-size:1.1rem;cursor:pointer;padding:0 4px;}  
.cmgr-del:hover{color:var(--accent);}  
.search-bar{background:#000;padding:8px 12px;border-bottom:1px solid var(--border);position:sticky;top:0;z-index:200;}  
.search-inner{display:flex;align-items:center;background:#1e1e1e;border:1px solid #333;border-radius:20px;padding:6px 14px;gap:8px;}  
.search-inner input{flex:1;background:none;border:none;color:var(--t1);font-size:0.9rem;outline:none;}  
.search-inner input::placeholder{color:#555;}  
.s-clear{color:#555;cursor:pointer;font-size:1.1rem;line-height:1;}  
.search-results{position:absolute;top:100%;left:0;right:0;background:#111;border-bottom:1px solid var(--border);z-index:199;max-height:60vh;overflow-y:auto;}  
.s-result-item{padding:10px 16px;border-bottom:1px solid #1e1e1e;cursor:pointer;}  
.s-result-item:hover{background:#1a1a1a;}  
.s-result-title{font-size:0.85rem;font-weight:600;}  
.s-result-sub{font-size:0.72rem;color:var(--t3);margin-top:2px;}  
.s-result-match{font-size:0.7rem;color:#4af;margin-top:2px;}  header{background:#000;padding:10px 14px;position:sticky;top:49px;z-index:100;border-bottom:1px solid var(--border);}
.logo-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;}
.logo{font-size:1.5rem;font-weight:900;color:var(--accent);}
.hd-btns{display:flex;gap:6px;}
.hd-btn{background:#222;border:1px solid #444;color:#aaa;padding:6px 11px;cursor:pointer;border-radius:16px;font-size:0.72rem;}
.nav{display:flex;gap:6px;}
.nav button{flex:1;padding:10px 4px;background:#181818;border:1px solid var(--border);color:#666;cursor:pointer;font-weight:bold;border-radius:8px;font-size:0.8rem;}
.nav button.active{background:var(--accent);border-color:var(--accent);color:#fff;}

.toolbar{display:flex;justify-content:space-between;align-items:center;padding:8px 14px;background:#141414;border-bottom:1px solid var(--border);}
.left-btns{display:flex;align-items:center;gap:6px;}
.filt-btn{background:#222;border:1px solid #444;color:#777;padding:7px 11px;border-radius:14px;font-size:0.73rem;cursor:pointer;transition:.15s;}
.filt-btn.on{background:#3a3000;border-color:#ffcc00;color:#ffcc00;}
.kw-toggle{background:#222;border:1px solid #444;color:#777;padding:7px 11px;border-radius:16px;font-size:0.78rem;cursor:pointer;transition:.15s;}
.kw-toggle.on{background:#001a3a;border-color:#4488ff;color:#4af;}
.badge{font-size:0.72rem;color:#555;}
.sort-btn{background:#222;border:1px solid #444;color:#eee;padding:7px 11px;border-radius:8px;font-size:0.78rem;cursor:pointer;}

.kw-sec{padding:10px 14px;background:#141414;border-bottom:1px solid var(--border);}
.kw-row{display:flex;gap:8px;margin-bottom:8px;}
.kw-inp{flex:1;background:#000;border:1px solid #333;color:#fff;padding:9px;border-radius:8px;font-size:0.85rem;outline:none;}
.kw-add{background:var(--accent);border:none;color:#fff;padding:0 14px;border-radius:8px;font-weight:bold;cursor:pointer;}
.kw-tags{display:flex;flex-wrap:wrap;gap:6px;}
.kw-tag{background:#2a2a2a;border:1px solid #333;border-radius:20px;padding:4px 10px;font-size:0.76rem;display:flex;align-items:center;gap:5px;}
.kw-rm{color:var(--accent);cursor:pointer;font-size:0.95rem;line-height:1;}

.cat-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:16px;}
.cat-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px 16px;cursor:pointer;text-align:center;transition:.15s;}
.cat-card:active{background:#222;}
.cat-card-label{font-size:1rem;font-weight:700;margin-bottom:6px;}
.cat-card-count{font-size:0.75rem;color:var(--t3);}
.cat-back-bar{display:flex;align-items:center;gap:10px;padding:10px 14px;background:#141414;border-bottom:1px solid var(--border);}
.cat-back-btn{background:none;border:none;color:#4af;font-size:0.85rem;cursor:pointer;padding:0;}
.cat-back-title{font-size:0.88rem;font-weight:700;color:var(--t1);}

#list-container{display:flex;flex-direction:column;gap:14px;padding:10px;}

.card{display:block;background:var(--card);border:1px solid var(--border);border-radius:12px;text-decoration:none;color:inherit;overflow:hidden;box-shadow:0 3px 6px rgba(0,0,0,0.3);}
.card:active{background:#222;}
.card-top{display:flex;width:100%;border-bottom:1px solid #222;}
.thumb-wrap{flex:7.5;aspect-ratio:16/9;background:#000;overflow:hidden;}
.thumb{width:100%;height:100%;object-fit:contain;display:block;}

.meta-right{flex:0 0 90px;background:#1a1a1a;padding:8px 6px;display:flex;flex-direction:column;align-items:center;gap:7px;border-left:1px solid #2a2a2a;}
.side-btns{display:flex;flex-direction:column;gap:4px;width:100%;}
.btn-star,.btn-block{width:100%;height:34px;background:#262626;border:1px solid #3a3a3a;border-radius:4px;color:#888;font-size:1rem;cursor:pointer;}
.btn-select{width:100%;height:34px;background:#262626;border:1px solid #3a3a3a;border-radius:4px;color:#555;font-size:0.7rem;cursor:pointer;margin-bottom:4px;}
.btn-select.on{background:#4af;border-color:#4af;color:#fff;}
.btn-star.on{color:#ffcc00;border-color:#ffcc00;}
.btn-block:hover{color:var(--accent);}
.meta-info{width:100%;display:flex;flex-direction:column;align-items:center;text-align:center;gap:4px;}
.m-station{font-size:0.62rem;color:#555;font-weight:bold;}
.m-label-row{width:90%;display:flex;flex-direction:column;align-items:center;}
.m-label-hd{font-size:0.52rem;color:#666;margin-bottom:1px;}
.m-label-val{font-size:0.6rem;color:#ddd;font-weight:bold;line-height:1.25;word-break:keep-all;}
.m-end-row{opacity:.4;text-align:center;}
.m-end-main{font-size:0.58rem;color:#f88;}
.m-end-sub{font-size:0.52rem;color:#bbb;}

.card-info{padding:10px 12px;}
.series-title{font-size:0.95rem;font-weight:700;margin-bottom:3px;color:var(--t1);}
.ep-title{font-size:0.82rem;color:var(--t2);margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.desc{font-size:0.73rem;color:#888;line-height:1.5;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;margin-bottom:6px;}

.cast-list{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;}
.cast-chip{background:#222;border:1px solid #333;border-radius:12px;padding:3px 9px;font-size:0.7rem;color:#aaa;cursor:pointer;transition:.1s;}
.cast-chip:hover{border-color:#4af;color:#4af;}
.cast-chip.fav-cast{border-color:#ffaa00;color:#ffaa00;background:#2a2000;}
.match-badge{display:inline-block;background:#2a1500;border:1px solid #ff6600;color:#ff8822;border-radius:10px;padding:2px 8px;font-size:0.68rem;margin-bottom:5px;}

.cast-tab-header{padding:12px 14px;background:#141414;border-bottom:1px solid var(--border);}
.fav-cast-tags{display:flex;flex-wrap:wrap;gap:6px;max-height:84px;overflow:hidden;transition:max-height 0.3s ease;margin-bottom:5px;}
.fav-cast-tags.expanded{max-height:2000px;}
.fav-cast-tag{background:#2a2000;border:1px solid #ffaa00;border-radius:20px;padding:5px 12px;font-size:0.78rem;color:#ffaa00;display:flex;align-items:center;gap:6px;cursor:pointer;}
.fav-cast-tag.active{background:#4af;border-color:#4af;color:#fff;}
.cast-empty{padding:24px;text-align:center;color:#555;font-size:0.85rem;}
.expand-btn{width:100%;background:#1a1a1a;border:1px solid #333;color:#4af;font-size:0.75rem;padding:4px;margin-top:6px;border-radius:4px;cursor:pointer;}

.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:300;align-items:flex-end;justify-content:center;}
.modal.open{display:flex;}
.modal-box{background:#1a1a1a;width:100%;max-width:500px;border-radius:12px 12px 0 0;max-height:80vh;display:flex;flex-direction:column;border-top:2px solid var(--accent);}
.modal-hd{padding:14px 16px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;gap:10px;}
.modal-hd h3{font-size:0.95rem;white-space:nowrap;}
.modal-close{background:none;border:none;color:#888;font-size:1.2rem;cursor:pointer;}
.modal-body{overflow-y:auto;flex:1;}
.modal-item{padding:11px 16px;border-bottom:1px solid #222;display:flex;justify-content:space-between;align-items:center;gap:10px;}
.modal-item span{flex:1;font-size:0.85rem;color:#ccc;}
.modal-item button{background:none;border:1px solid #444;border-radius:6px;color:#aaa;padding:4px 10px;font-size:0.72rem;cursor:pointer;}
.modal-empty{padding:28px;text-align:center;color:#555;font-size:0.85rem;}
.modal-ft{padding:12px;display:flex;gap:8px;border-top:1px solid var(--border);}
.modal-ft button{flex:1;padding:10px;border-radius:6px;border:none;cursor:pointer;font-size:0.8rem;}
.cast-tab-header div::-webkit-scrollbar{display:none;}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,0.92);color:#fff;padding:8px 18px;border-radius:20px;font-size:0.78rem;border:1px solid #333;opacity:0;transition:opacity .2s;z-index:999;pointer-events:none;white-space:nowrap;}
#toast.show{opacity:1;}
</style>
</head>  
<body>  
<div class="search-bar">  
  <div class="search-inner">  
    <span style="color:#555;font-size:1rem;">🔍</span>  
    <input id="search-input" type="search" placeholder="전체 통합 검색 (제목·설명·출연자)">  
    <span class="s-clear" id="search-clear" onclick="clearSearch()" style="display:none;">✕</span>  
  </div>  
  <div class="search-results" id="search-results" style="display:none;"></div>  
</div>  
<header>  
  <div class="logo-row">  
    <div style="display:flex;align-items:baseline;gap:8px;">  
      <span class="logo">TVer</span>  
      <span style="font-size:0.65rem;color:#555;">@GENERATED_AT@ 기준</span>  
    </div>  
    <div class="hd-btns">  
      <button class="hd-btn" onclick="openModal('block-modal')">🚫 차단</button>  
      <button class="hd-btn" onclick="openModal('fav-modal')">★ 즐겨찾기</button>  
      <button class="hd-btn" onclick="openModal('cast-modal')">👤 출연자</button>  
    </div>  
  </div>  
  <div class="nav">  
    <button id="btn-drama"    class="active" onclick="setTab('drama')">드라마</button>  
    <button id="btn-variety"              onclick="setTab('variety')">예능</button>  
    <button id="btn-category"             onclick="setTab('category')">카테고리</button>  
    <button id="btn-cast"                 onclick="setTab('cast')">출연자</button>  
  </div>  
</header>  
<div class="toolbar" id="main-toolbar">  
  <div class="left-btns">  
    <button id="fav-btn"  class="filt-btn"  onclick="toggleFav()">★ 즐겨찾기</button>  
    <button id="kw-btn"   class="kw-toggle" onclick="toggleKw()">🔍 키워드</button>  
    <button id="exkw-btn" class="kw-toggle" onclick="toggleExKw()">🚫 제외</button>  
    <span   id="badge"    class="badge">0건</span>  
  </div>  
  <button id="sort-btn" class="sort-btn" onclick="toggleSort()">업데이트순 ⇅</button>  
</div>  
<div id="kw-sec" class="kw-sec" style="display:none;">  
  <div class="kw-row">  
    <input id="kw-inp" class="kw-inp" type="text" placeholder="키워드 추가">  
    <button class="kw-add" onclick="addKw()">추가</button>  
  </div>  
  <div id="kw-tags" class="kw-tags"></div>  
</div>  
<div id="exkw-sec" class="kw-sec" style="display:none;">  
  <div class="kw-row">  
    <input id="exkw-inp" class="kw-inp" type="text" placeholder="제외 키워드 추가">  
    <button class="kw-add" onclick="addExKw()">추가</button>  
  </div>  
  <div id="exkw-tags" class="kw-tags"></div>  
</div>  
<div id="list-container"></div>  

<div id="block-modal" class="modal" onclick="if(event.target===this)closeModal('block-modal')">  
  <div class="modal-box">  
    <div class="modal-hd"><h3>🚫 차단 목록</h3><button class="modal-close" onclick="closeModal('block-modal')">✕</button></div>  
    <div class="modal-body" id="block-list"></div>  
    <div class="modal-ft">  
      <button style="background:#1a2a1a;color:#5d9;" onclick="exportList('block')">내보내기</button>  
      <button style="background:#2a1010;color:#e66;" onclick="clearAll('block')">전체 해제</button>  
    </div>  
  </div>  
</div>  
<div id="fav-modal" class="modal" onclick="if(event.target===this)closeModal('fav-modal')">  
  <div class="modal-box">  
    <div class="modal-hd"><h3>★ 즐겨찾기</h3><button class="modal-close" onclick="closeModal('fav-modal')">✕</button></div>  
    <div class="modal-body" id="fav-list"></div>  
    <div class="modal-ft">  
      <button style="background:#1a2a1a;color:#5d9;" onclick="exportList('fav')">내보내기</button>  
      <button style="background:#2a1010;color:#e66;" onclick="clearAll('fav')">전체 해제</button>  
    </div>  
  </div>  
</div>  
<div id="cast-modal" class="modal" onclick="if(event.target===this)closeModal('cast-modal')">  
  <div class="modal-box">  
    <div class="modal-hd">  
      <h3 id="cast-modal-title">👤 출연자 관리</h3>  
      <input type="text" id="cast-m-search" placeholder="이름 검색..."  
             style="flex:1;max-width:140px;padding:6px;font-size:0.8rem;background:#000;border:1px solid #444;color:#fff;border-radius:4px;outline:none;">  
      <button class="modal-close" onclick="closeModal('cast-modal')">✕</button>  
    </div>  
    <div class="modal-body"><div id="cast-list-modal"></div></div>  
    <div class="modal-ft">  
      <button style="background:#1a2a1a;color:#5d9;" onclick="exportTotalConfig()">설정 내보내기</button>  
      <button style="background:#1a1a2a;color:#9bf;" onclick="importTotalConfig()">설정 불러오기</button>  
      <button style="background:#2a1010;color:#e66;" onclick="clearAll('cast')">전체 삭제</button>  
    </div>  
  </div>  
</div>  
<div id="cast-mgr-modal" class="modal" onclick="if(event.target===this)closeCastMgr()">  
  <div class="modal-box">  
    <div class="modal-hd">  
      <h3>👤 출연자 관리</h3>  
      <input id="cast-mgr-search" type="text" placeholder="이름 검색..."  
        style="flex:1;max-width:140px;padding:6px;font-size:0.8rem;background:#000;border:1px solid #444;color:#fff;border-radius:4px;outline:none;">  
      <button class="modal-close" onclick="closeCastMgr()">✕</button>  
    </div>  
    <div class="kw-row" style="padding:10px 16px;border-bottom:1px solid #222;">  
      <input id="cast-inp" class="kw-inp" type="text" placeholder="이름 또는 ID 등록">  
      <button class="kw-add" onclick="addFavCastByName()">등록</button>  
    </div>  
    <div class="modal-body" id="cast-mgr-list"></div>  
    <div class="modal-ft">  
      <button style="background:#1a2a1a;color:#5d9;" onclick="exportTotalConfig()">설정 내보내기</button>  
      <button style="background:#1a1a2a;color:#9bf;" onclick="importTotalConfig()">설정 불러오기</button>  
      <button style="background:#2a1010;color:#e66;" onclick="clearAll('cast')">전체 삭제</button>  
    </div>  
  </div>  
</div>  
<div id="toast"></div>  

<script>  
const DATA     = @DATA_JSON@;  
const CAT_META = @CAT_META_JSON@;  
const ALL_ITEMS = Object.values(DATA).flat();  
let castYear = new Date().getFullYear();  
let tab            = 'drama';  
let selectedCat    = null;  
let filterCastId   = null;  
let sortMode       = 'update';  
let scrollPos      = { drama:0, variety:0, category:0, cast:0 };  
let favState       = { drama:false, variety:false, category:false, cast:false };  
let kwState        = { drama:false, variety:false, category:false, cast:false };  
let exKwState      = { drama:false, variety:false, category:false, cast:false };  
let selectionQueues = {};  
ALL_ITEMS.forEach(i => { if(!selectionQueues[i.category]) selectionQueues[i.category] = new Set(); });  
  
let S = {  
  blocked:   JSON.parse(localStorage.getItem('tver_block')   || '{}'),  
  favorites: JSON.parse(localStorage.getItem('tver_fav')     || '{}'),  
  favCasts:  JSON.parse(localStorage.getItem('tver_favcast') || '{}'),  
  keywords:  JSON.parse(localStorage.getItem('tver_kw')      || '[]'),  
  excludeKw: JSON.parse(localStorage.getItem('tver_exkw')    || '{}'),  
};  
  
function normalizeKana(str) {  
  if (!str) return "";  
  return str.replace(/[\u30a1-\u30f6]/g, s => String.fromCharCode(s.charCodeAt(0) - 0x60))  
            .replace(/\s+/g, '').toLowerCase();  
}  
function save() {  
  localStorage.setItem('tver_block',   JSON.stringify(S.blocked));  
  localStorage.setItem('tver_fav',     JSON.stringify(S.favorites));  
  localStorage.setItem('tver_kw',      JSON.stringify(S.keywords));  
  localStorage.setItem('tver_exkw',    JSON.stringify(S.excludeKw));  
  localStorage.setItem('tver_favcast', JSON.stringify(S.favCasts));  
}  
function isFavCast(id) { return !!S.favCasts[id]; }  
function toggleFavCast(id, name) {  
  if (S.favCasts[id]) { delete S.favCasts[id]; toast('출연자 해제: '+name); }  
  else {  
    let kana = "";  
    for (const item of ALL_ITEMS) { const t = item.talents.find(x=>x.id===id); if(t){kana=t.kana||"";break;} }  
    S.favCasts[id] = {name, kana};  
    toast('⭐ 출연자 등록: '+name);  
  }  
  save();  
  if (tab==='cast') renderCast();  
}  
function toggleSort() {  
  sortMode = sortMode==='update' ? 'bkey' : 'update';  
  document.getElementById('sort-btn').textContent = sortMode==='update' ? '업데이트순 ⇅' : '방송분순 ⇅';  
  render();  
}  
function toggleAndCopy(url, btn, category) {  
  const queue = selectionQueues[category] || (selectionQueues[category]=new Set());  
  if (queue.has(url)) { queue.delete(url); btn.classList.remove('on'); btn.textContent='선택 복사'; }  
  else { queue.add(url); btn.classList.add('on'); btn.textContent='완료'; }  
  if (queue.size > 0) { copyToClipboard(Array.from(queue).join(' ')); toast(queue.size+'개 복사됨'); }  
  else toast('선택 해제');  
}  
  
function setTab(t) {  
  if (tab !== t) scrollPos[tab] = window.scrollY;  
  tab = t;  
  document.querySelectorAll('.nav button').forEach(b=>b.classList.remove('active'));  
  document.getElementById('btn-'+t)?.classList.add('active');  
  
  const isCast = (t==='cast');  
  const isCat  = (t==='category');  
  const showToolbar = !isCast && !(isCat && !selectedCat);  
  document.getElementById('main-toolbar').style.display = showToolbar ? '' : 'none';  
  
  document.getElementById('fav-btn').classList.toggle('on', !!favState[tab]);  
  const isKwOn = !!kwState[tab];  
  document.getElementById('kw-btn').classList.toggle('on', isKwOn);  
  document.getElementById('kw-sec').style.display = isKwOn ? 'block' : 'none';  
  const isExOn = !!exKwState[tab];  
  document.getElementById('exkw-btn').classList.toggle('on', isExOn);  
  document.getElementById('exkw-sec').style.display = isExOn ? 'block' : 'none';  
  
  render();  
  setTimeout(()=>window.scrollTo(0, scrollPos[t]||0), 0);  
}  
  
function selectCategory(slug) {  
  selectedCat = slug;  
  document.getElementById('main-toolbar').style.display = '';  
  render();  
}  
function backToCategories() {  
  selectedCat = null;  
  document.getElementById('main-toolbar').style.display = 'none';  
  document.getElementById('kw-sec').style.display = 'none';  
  document.getElementById('exkw-sec').style.display = 'none';  
  render();  
}  
  
function toggleFav() { favState[tab]=!favState[tab]; document.getElementById('fav-btn').classList.toggle('on',favState[tab]); render(); }  
function toggleKw() {  
  kwState[tab]=!kwState[tab]; const on=kwState[tab];  
  document.getElementById('kw-btn').classList.toggle('on',on);  
  document.getElementById('kw-sec').style.display=on?'block':'none';  
  render();  
}  
function toggleExKw() {  
  exKwState[tab]=!exKwState[tab]; const on=exKwState[tab];  
  document.getElementById('exkw-btn').classList.toggle('on',on);  
  document.getElementById('exkw-sec').style.display=on?'block':'none';  
  render();  
}  
  
function render() {  
  if (tab==='cast')     { renderCast(); return; }  
  if (tab==='category') { renderCategory(); return; }  
  
  const slug = tab;  
  let list = (DATA[slug]||[]).filter(item => {  
    if (S.blocked[item.seriesID]) return false;  
    if (item.year > 0 && item.year < new Date().getFullYear()) return false;  
    if (favState[tab] && !S.favorites[item.seriesID]) return false;  
    const exList = S.excludeKw[tab] || [];  
    if (exList.length > 0) {  
      const txt = (item.seriesTitle+item.title+item.description+item.talents.map(t=>t.name).join(' ')).toLowerCase();  
      if (exList.some(k=>txt.includes(k.toLowerCase()))) return false;  
    }  
    if (kwState[tab] && S.keywords.length > 0) {  
      const txt = (item.seriesTitle+item.title+item.description+item.talents.map(t=>t.name).join(' ')).toLowerCase();  
      if (!S.keywords.some(k=>txt.includes(k.toLowerCase()))) return false;  
    }  
    return true;  
  });  
  
  list.sort((a,b)=> sortMode==='bkey'  
  ? ((b.year||0)-(a.year||0) || b.bkey-a.bkey || b.startAt-a.startAt)  
  : (b.startAt-a.startAt));  
  
  document.getElementById('kw-tags').innerHTML = S.keywords.map(k=>  
    `<div class="kw-tag">${esc(k)}<span class="kw-rm" onclick="removeKw('${k.replace(/'/g,"\\'")}')">×</span></div>`).join('');  
  document.getElementById('exkw-tags').innerHTML = (S.excludeKw[tab]||[]).map(k=>  
    `<div class="kw-tag">${esc(k)}<span class="kw-rm" onclick="removeExKw('${k.replace(/'/g,"\\'")}')">×</span></div>`).join('');  
  
  document.getElementById('list-container').innerHTML = list.map(item=>cardHTML(item,[])).join('');  
  document.getElementById('badge').textContent = list.length+'건';  
}  
  
function renderCategory() {  
  if (selectedCat) {  
    const meta = CAT_META.find(c=>c.slug===selectedCat) || {label:selectedCat, count:0};  
    const backBar = `<div class="cat-back-bar">  
      <button class="cat-back-btn" onclick="backToCategories()">← 카테고리</button>  
      <span class="cat-back-title">${esc(meta.label)}</span>  
    </div>`;  
  
    let list = (DATA[selectedCat]||[]).filter(item => {  
      if (S.blocked[item.seriesID]) return false;  
      if (favState[tab] && !S.favorites[item.seriesID]) return false;  
      const exList = S.excludeKw[tab] || [];  
      if (exList.length > 0) {  
        const txt = (item.seriesTitle+item.title+item.description+item.talents.map(t=>t.name).join(' ')).toLowerCase();  
        if (exList.some(k=>txt.includes(k.toLowerCase()))) return false;  
      }  
      if (kwState[tab] && S.keywords.length > 0) {  
        const txt = (item.seriesTitle+item.title+item.description+item.talents.map(t=>t.name).join(' ')).toLowerCase();  
        if (!S.keywords.some(k=>txt.includes(k.toLowerCase()))) return false;  
      }  
      return true;  
    });  
    list.sort((a,b) => sortMode==='bkey'  
  ? ((b.year||0) - (a.year||0) || b.bkey - a.bkey)  
  : (b.startAt - a.startAt));  
  
    document.getElementById('kw-tags').innerHTML = S.keywords.map(k=>  
      `<div class="kw-tag">${esc(k)}<span class="kw-rm" onclick="removeKw('${k.replace(/'/g,"\\'")}')">×</span></div>`).join('');  
    document.getElementById('exkw-tags').innerHTML = (S.excludeKw[tab]||[]).map(k=>  
      `<div class="kw-tag">${esc(k)}<span class="kw-rm" onclick="removeExKw('${k.replace(/'/g,"\\'")}')">×</span></div>`).join('');  
  
    document.getElementById('list-container').innerHTML = backBar + list.map(item=>cardHTML(item,[])).join('');  
    document.getElementById('badge').textContent = list.length+'건';  
  } else {  
    document.getElementById('main-toolbar').style.display = 'none';  
    document.getElementById('kw-sec').style.display = 'none';  
    document.getElementById('exkw-sec').style.display = 'none';  
    document.getElementById('list-container').innerHTML = `  
      <div class="cat-grid">  
        ${CAT_META.map(c=>`  
          <div class="cat-card" onclick="selectCategory('${c.slug}')">  
            <div class="cat-card-label">${esc(c.label)}</div>  
            <div class="cat-card-count">${c.count}건</div>  
          </div>`).join('')}  
      </div>`;  
    document.getElementById('badge').textContent = CAT_META.length+'개';  
  }  
}  
  
let castCatFilter = null;  
  
function openCastMgr() {  
  const castLatest = {};  
  for(const item of ALL_ITEMS){  
    for(const t of item.talents){  
      if(S.favCasts[t.id]){  
        if(!castLatest[t.id] || item.startAt > castLatest[t.id])  
          castLatest[t.id] = item.startAt;  
      }  
    }  
  }  
  
  const ids = Object.keys(S.favCasts).sort((a,b)=>  
    (castLatest[b]||0) - (castLatest[a]||0)  
  );  
  
  const modal = document.getElementById('cast-mgr-modal');  
  const list  = document.getElementById('cast-mgr-list');  
  
  list.innerHTML = ids.length ? ids.map(id=>{  
    const c = S.favCasts[id]||{};  
    const name = typeof c==='object' ? c.name : c;  
    const kana = typeof c==='object' ? (c.kana||"") : "";  
    const isActive = filterCastId === id;  
    const latest = castLatest[id] ? new Date(castLatest[id]*1000).toLocaleDateString('ja-JP',{month:'numeric',day:'numeric'}) : '-';  
    return `<div class="cmgr-item ${isActive?'active':''}" data-name="${esc(name)}" data-kana="${esc(kana)}">  
      <span class="cmgr-name" onclick="filterByCast('${id}');closeCastMgr()">${esc(name)||id}</span>  
      <span class="cmgr-latest">${latest}</span>  
      <button class="cmgr-del" onclick="removeFavCast('${id}')">×</button>  
    </div>`;  
  }).join('') : '<p class="modal-empty">등록된 출연자 없음</p>';  
  
  const si = document.getElementById('cast-mgr-search');  
  si.value = '';  
  si.oninput = e => {  
    const kw = normalizeKana(e.target.value);  
    document.querySelectorAll('.cmgr-item').forEach(el=>{  
      const n = normalizeKana(el.getAttribute('data-name')||"");  
      const k = normalizeKana(el.getAttribute('data-kana')||"");  
      el.style.display = (!kw||n.includes(kw)||k.includes(kw)) ? '' : 'none';  
    });  
  };  
  
  modal.classList.add('open');  
}  
  
function closeCastMgr(){ document.getElementById('cast-mgr-modal').classList.remove('open'); }  
  
function removeFavCast(id){  
  if(!confirm('삭제하시겠습니까?')) return;  
  delete S.favCasts[id]; save();  
  if(filterCastId===id) filterCastId=null;  
  openCastMgr(); renderCast();  
}  
  
function renderCast() {  
  for(const id of Object.keys(S.favCasts)){  
    const c = S.favCasts[id];  
    const name = typeof c==='object' ? c.name : c;  
    if(!name || name===id){  
      for(const item of ALL_ITEMS){  
        const t = item.talents.find(x=>x.id===id);  
        if(t){ S.favCasts[id]={name:t.name,kana:t.kana||""}; save(); break; }  
      }  
    }  
  }  
  
  const favCastIds = Object.keys(S.favCasts);  
  
  let allMatched = [];  
  const seenSeries = new Set();  
  for(const item of ALL_ITEMS){  
    if(S.blocked[item.seriesID]) continue;  
    if(seenSeries.has(item.seriesID+item.category)) continue;  
    if(castCatFilter && item.category !== castCatFilter) continue;  
    const mc = item.talents.filter(t=>filterCastId ? t.id===filterCastId : S.favCasts[t.id]);  
    if(mc.length>0){  
      seenSeries.add(item.seriesID+item.category);  
      allMatched.push({item, matchedCasts:mc, year:item.year||2026});  
    }  
  }  
  
  const years = [...new Set(allMatched.map(x=>x.year).filter(y=>y>0))].sort((a,b)=>b-a);  
  if(typeof castYear==='undefined'||!years.includes(castYear)) castYear=years[0]||new Date().getFullYear();  
  
  let matched = years.length ? allMatched.filter(x=>x.year===castYear) : [];  
  matched.sort((a,b)=>b.item.startAt-a.item.startAt);  
  
  const availCats = [...new Set(ALL_ITEMS.map(i=>i.category))];  
  const catLabels = {drama:'드라마',variety:'예능',news_documentary:'다큐',anime:'애니',sports:'스포츠',music:'음악',other:'기타'};  
  
  const header = `  
  <div class="cast-tab-header">  
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">  
      <button onclick="openCastMgr()" style="background:#222;border:1px solid #444;color:#aaa;padding:5px 10px;border-radius:16px;font-size:0.75rem;cursor:pointer;">출연자 목록👤 ${favCastIds.length}명 ▼</button>  
      <div style="display:flex;align-items:center;gap:6px;">  
        ${filterCastId ? `<button onclick="filterByCast(null)" style="background:#2a1500;border:1px solid #ff6600;color:#ff8822;padding:4px 9px;border-radius:16px;font-size:0.75rem;cursor:pointer;">${esc((S.favCasts[filterCastId]||{}).name||filterCastId)} ×</button>` : ''}  
        <select onchange="castYear=+this.value;renderCast()"  
          style="background:#222;border:1px solid #444;color:#fff;padding:5px 8px;border-radius:8px;font-size:0.75rem;outline:none;">  
          ${years.map(y=>`<option value="${y}" ${y===castYear?'selected':''}>${y}년</option>`).join('')}  
        </select>  
      </div>  
    </div>  
    <div style="display:flex;gap:5px;overflow-x:auto;padding-bottom:4px;-webkit-overflow-scrolling:touch;scrollbar-width:none;">  
      <button onclick="castCatFilter=null;renderCast()"  
        style="flex-shrink:0;padding:4px 10px;border-radius:12px;font-size:0.72rem;cursor:pointer;border:1px solid ${!castCatFilter?'#4af':'#444'};background:${!castCatFilter?'#001a3a':'#222'};color:${!castCatFilter?'#4af':'#777'};">전체</button>  
      ${availCats.map(cat=>`  
        <button onclick="castCatFilter='${cat}';renderCast()"  
          style="flex-shrink:0;padding:4px 10px;border-radius:12px;font-size:0.72rem;cursor:pointer;border:1px solid ${castCatFilter===cat?'#4af':'#444'};background:${castCatFilter===cat?'#001a3a':'#222'};color:${castCatFilter===cat?'#4af':'#777'};">  
          ${catLabels[cat]||cat}</button>`).join('')}  
    </div>  
  </div>`;  
  
  const cards = matched.map(({item,matchedCasts})=>cardHTML(item,matchedCasts)).join('');  
  document.getElementById('list-container').innerHTML = header + (matched.length ? cards : '<p class="cast-empty">출연 방송이 없습니다</p>');  
  document.getElementById('badge').textContent = matched.length+'건';  
}  
  
function filterByCast(id){ filterCastId=(filterCastId===id)?null:id; renderCast(); }  
function addFavCastByName(){  
  const inp=document.getElementById('cast-inp'), name=inp.value.trim();  
  if(!name) return;  
  let found=null;  
  for(const item of ALL_ITEMS){ found=item.talents.find(t=>t.name.replace(/\s+/g,'')===name.replace(/\s+/g,'')); if(found) break; }  
  if(found){ S.favCasts[found.id]={name:found.name,kana:found.kana||""}; toast('등록: '+found.name); }  
  else { S.favCasts["temp_"+Date.now()]={name,kana:""}; toast('임시 등록: '+name); }  
  save(); renderCast(); inp.value='';  
}  
  
function cardHTML(item, matchedCasts) {  
  const isFav=!!S.favorites[item.seriesID];  
  const url=`https://tver.jp/episodes/${item.id}`;  
  const sid=item.seriesID;  
  const titleSafe=item.seriesTitle.replace(/\\/g,'\\\\').replace(/'/g,"\\'");  
  const endTxt=item.end_sub?`${item.end_sub.split(')')[0]})\n${item.end_sub.split(')')[1]} まで`:'';  
  const matchBadge=matchedCasts.length?`<div class="match-badge">⭐ ${matchedCasts.map(t=>esc(t.name)).join(' · ')}</div>`:'';  
  const castChips=item.talents.length?`<div class="cast-list">${item.talents.map(t=>{  
    const fc=isFavCast(t.id);  
    return `<span class="cast-chip ${fc?'fav-cast':''}"  
      onclick="event.preventDefault();event.stopPropagation();toggleFavCast('${t.id}','${t.name.replace(/'/g,"\\'")}');this.classList.toggle('fav-cast')"  
      >${esc(t.name)}</span>`;  
  }).join('')}</div>`:'';  
  
  return `  
  <a href="${url}" class="card" target="_blank">  
    <div class="card-top">  
      <div class="thumb-wrap">  
        <img class="thumb" src="https://statics.tver.jp/images/content/thumbnail/episode/large/${item.id}.jpg"  
             loading="lazy" onerror="this.style.display='none'">  
      </div>  
      <div class="meta-right">  
        <div class="side-btns">  
          <button class="btn-select ${(selectionQueues[item.category]||new Set()).has(url)?'on':''}"  
            onclick="event.preventDefault();event.stopPropagation();toggleAndCopy('${url}',this,'${item.category}')">선택 복사</button>  
          <button class="btn-star ${isFav?'on':''}"  
            onclick="event.preventDefault();event.stopPropagation();toggleStar('${sid}','${titleSafe}',this)">★</button>  
          <button class="btn-block"  
            onclick="event.preventDefault();event.stopPropagation();block('${sid}','${titleSafe}')">🚫</button>  
        </div>  
        <div class="meta-info">  
          <div class="m-station">${esc(item.broadcaster)}</div>  
          <div class="m-label-row">  
            <div class="m-label-hd">방송일</div>  
            <div class="m-label-val">${esc(item.label)}</div>  
            ${item.isSubtitle?'<div style="font-size:0.58rem;color:#4af;margin-top:2px;">字幕</div>':''}  
          </div>  
          ${item.end_main?`<div class="m-end-row"><div class="m-end-main">${esc(item.end_main)}</div><div class="m-end-sub">${esc(endTxt)}</div></div>`:''}  
        </div>  
      </div>  
    </div>  
    <div class="card-info">  
      ${matchBadge}  
      <div class="series-title">${esc(item.seriesTitle)}</div>  
      <div class="ep-title">${esc(item.title)}</div>  
      <div class="desc">${esc(item.description)}</div>  
      ${castChips}  
    </div>  
  </a>`;  
}  
  
let searchTimer;  
document.getElementById('search-input').addEventListener('input', function(){  
  clearTimeout(searchTimer);  
  const q=this.value.trim();  
  document.getElementById('search-clear').style.display=q?'block':'none';  
  if(!q){closeSearch();return;}  
  searchTimer=setTimeout(()=>doSearch(q),250);  
});  
document.getElementById('search-input').addEventListener('keydown',e=>{if(e.key==='Escape')clearSearch();});  
  
function doSearch(q){  
  const kw=q.toLowerCase(), results=[];  
  for(const item of ALL_ITEMS){  
    if(S.blocked[item.seriesID]) continue;  
    const titleMatch=item.seriesTitle.toLowerCase().includes(kw)||item.title.toLowerCase().includes(kw);  
    const descMatch=item.description.toLowerCase().includes(kw);  
    const castMatch=item.talents.filter(t=>t.name.toLowerCase().includes(kw)||normalizeKana(t.kana).includes(normalizeKana(kw)));  
    if(!titleMatch&&!descMatch&&!castMatch.length) continue;  
    const labels=[];  
    if(titleMatch) labels.push('제목');  
    if(descMatch)  labels.push('설명');  
    if(castMatch.length) labels.push('출연: '+castMatch.map(t=>t.name).join(', '));  
    results.push({item,matchLabel:labels.join(' · ')});  
    if(results.length>=30) break;  
  }  
  const el=document.getElementById('search-results');  
  el.innerHTML=results.length  
    ?results.map(({item,matchLabel})=>`  
      <div class="s-result-item" onclick="openEpisode('${item.id}')">  
        <div class="s-result-title">${esc(item.seriesTitle)}</div>  
        <div class="s-result-sub">${esc(item.broadcaster)} · ${esc(item.label)} · ${esc(item.title)}</div>  
        <div class="s-result-match">${esc(matchLabel)}</div>  
      </div>`).join('')  
    :'<div style="padding:16px;text-align:center;color:#555;font-size:0.82rem;">결과 없음</div>';  
  el.style.display='block';  
}  
function openEpisode(id){ const url=`https://tver.jp/episodes/${id}`; window.open(url,'_blank'); clearSearch(); }  
function closeSearch(){ document.getElementById('search-results').style.display='none'; }  
function clearSearch(){ document.getElementById('search-input').value=''; document.getElementById('search-clear').style.display='none'; closeSearch(); }  
document.addEventListener('click',e=>{if(!e.target.closest('.search-bar'))closeSearch();});  
  
function toggleStar(sid,title,btn){ if(S.favorites[sid]){delete S.favorites[sid];btn.classList.remove('on');toast('즐겨찾기 해제');}else{S.favorites[sid]=title;btn.classList.add('on');toast('★ '+title);} save(); }  
function block(sid,title){ if(!confirm(title+'\n차단하시겠습니까?')) return; S.blocked[sid]=title; save(); render(); toast('차단: '+title); }  
function unblock(sid){ delete S.blocked[sid]; save(); openModal('block-modal'); render(); }  
function unfav(sid){ delete S.favorites[sid]; save(); openModal('fav-modal'); render(); }  
function addKw(){ const i=document.getElementById('kw-inp'),v=i.value.trim(); if(v&&!S.keywords.includes(v)){S.keywords.push(v);save();render();} i.value=''; }  
function removeKw(k){ S.keywords=S.keywords.filter(x=>x!==k); save(); render(); }  
function addExKw(){ const i=document.getElementById('exkw-inp'),v=i.value.trim(); if(!v) return; if(!S.excludeKw[tab]) S.excludeKw[tab]=[]; if(!S.excludeKw[tab].includes(v)){S.excludeKw[tab].push(v);save();render();} i.value=''; }  
function removeExKw(k){ S.excludeKw[tab]=(S.excludeKw[tab]||[]).filter(x=>x!==k); save(); render(); }  
  
function openModal(id){  
  if(id==='cast-modal'){  
    const ids=Object.keys(S.favCasts);  
    document.getElementById('cast-modal-title').textContent=`👤 출연자 (${ids.length})`;  
    document.getElementById('cast-list-modal').innerHTML=ids.length  
      ?ids.map(cid=>{const c=S.favCasts[cid]||{};const cName=typeof c==='object'?c.name:c;const cKana=typeof c==='object'?(c.kana||""):"";  
        return `<div class="modal-item cast-row-item" data-name="${esc(cName)}" data-kana="${esc(cKana)}">  
          <span style="cursor:pointer;color:#4af;" onclick="setTab('cast');filterCastId='${cid}';renderCast();closeModal('cast-modal')">${esc(cName)}</span>  
          <button onclick="removeCast('${cid}')">삭제</button></div>`;}).join('')  
      :'<p class="modal-empty">등록된 출연자가 없습니다</p>';  
    const sInp=document.getElementById('cast-m-search'); sInp.value='';  
    sInp.onclick=e=>e.stopPropagation();  
    sInp.oninput=e=>{const kw=normalizeKana(e.target.value);document.querySelectorAll('.cast-row-item').forEach(row=>{const name=normalizeKana(row.getAttribute('data-name')||"");const kana=normalizeKana(row.getAttribute('data-kana')||"");row.style.display=(!kw||name.includes(kw)||kana.includes(kw))?'flex':'none';});};  
  } else {  
    const map={'block-modal':['block-list',S.blocked,'unblock'],'fav-modal':['fav-list',S.favorites,'unfav']};  
    const [elId,data,fn]=map[id];  
    const sids=Object.keys(data);  
    document.getElementById(elId).innerHTML=sids.length  
      ?sids.map(sid=>`<div class="modal-item"><span>${esc(data[sid])}</span><button onclick="${fn}('${sid}')">해제</button></div>`).join('')  
      :'<p class="modal-empty">비어있음</p>';  
  }  
  document.getElementById(id).classList.add('open');  
}  
function closeModal(id){ document.getElementById(id).classList.remove('open'); }  
function removeCast(id){ if(!confirm('삭제하시겠습니까?')) return; delete S.favCasts[id]; save(); openModal('cast-modal'); if(tab==='cast') renderCast(); }  
function exportList(type){ const obj=type==='block'?S.blocked:S.favorites; const blob=new Blob([Object.entries(obj).map(([s,t])=>s+'\t'+t).join('\n')],{type:'text/plain'}); const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=`tver_${type}.txt`; a.click(); }  
function clearAll(type){ if(!confirm('전체 해제/삭제?')) return; if(type==='block') S.blocked={}; else if(type==='fav') S.favorites={}; else S.favCasts={}; save(); if(type==='cast'){openModal('cast-modal');if(tab==='cast')renderCast();}else{render();closeModal(type==='block'?'block-modal':'fav-modal');} }  
function exportTotalConfig(){ const blob=new Blob([JSON.stringify({blocked:S.blocked,favorites:S.favorites,favCasts:S.favCasts,excludeKw: S.excludeKw,keywords:S.keywords},null,2)],{type:'application/json'}); const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=`tver_config_${Date.now()}.json`; a.click(); toast('설정 내보내기 완료'); }  
function importTotalConfig(){ const input=document.createElement('input'); input.type='file'; input.accept='.json'; input.onchange=e=>{const reader=new FileReader(); reader.onload=ev=>{try{const d=JSON.parse(ev.target.result); if(!confirm('기존 설정을 덮어씌우시겠습니까?')) return; if(d.blocked) S.blocked=d.blocked; if(d.favorites) S.favorites=d.favorites; if(d.favCasts) S.favCasts=d.favCasts; if(d.keywords) S.keywords=d.keywords; if(d.excludeKw) S.excludeKw=d.excludeKw; save(); location.reload();}catch{alert('올바른 JSON 파일이 아닙니다.');}}; reader.readAsText(e.target.files[0]);}; input.click(); }  
function copyToClipboard(text) {  
  if (navigator.clipboard) {  
    navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));  
  } else {  
    fallbackCopy(text);  
  }  
}  
function fallbackCopy(text) {  
  const el = document.createElement('textarea');  
  el.value = text;  
  el.style.cssText = 'position:fixed;opacity:0';  
  document.body.appendChild(el);  
  el.select();  
  document.execCommand('copy');  
  document.body.removeChild(el);  
}  
function esc(s){ return String(s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":"&#39;"}[m])); }  
let _tt;  
function toast(m){ const t=document.getElementById('toast'); t.textContent=m; t.classList.add('show'); clearTimeout(_tt); _tt=setTimeout(()=>t.classList.remove('show'),2200); }  
  
render();  
</script>  
</body>  
</html>  
"""

if __name__ == "__main__":
    data, cat_meta = fetch_all()
    now_str = datetime.now().strftime("%m/%d %H:%M")
    html = HTML.replace("@DATA_JSON@", json.dumps(data, ensure_ascii=False))
    html = html.replace("@CAT_META_JSON@", json.dumps(cat_meta, ensure_ascii=False))
    html = html.replace("@GENERATED_AT@", now_str)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    for slug, items in data.items():
        cast_cnt = sum(1 for i in items if i["talents"])
        print(f"  {slug}: {len(items)}건 (출연자 있음: {cast_cnt}건)")
    print(f"저장: {OUT_PATH}")
