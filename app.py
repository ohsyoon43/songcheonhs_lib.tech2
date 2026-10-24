import streamlit as st
import pandas as pd
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from io import BytesIO
import sqlite3
from pathlib import Path
import tempfile
import os

# ------------------------------------------------------------
# Page configuration
# ------------------------------------------------------------
st.set_page_config(page_title="송천고 행사 관리 시스템", page_icon="📚", layout="wide", initial_sidebar_state="collapsed")
SEOUL = ZoneInfo("Asia/Seoul")
def now_seoul():
    return datetime.now(SEOUL)
DB_PATH = Path(__file__).with_name("library_system.db")

# ------------------------------------------------------------
# Database: existing table names and columns are preserved
# ------------------------------------------------------------
def get_db_connection():
    return sqlite3.connect(str(DB_PATH), check_same_thread=False)

def init_db():
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_name TEXT NOT NULL,
            student_id TEXT NOT NULL, grade INTEGER NOT NULL, class INTEGER NOT NULL,
            number INTEGER NOT NULL, name TEXT NOT NULL, timestamp TEXT NOT NULL,
            operator TEXT NOT NULL)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            category TEXT NOT NULL, detail TEXT NOT NULL, operator TEXT NOT NULL)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS deleted_participants (
            archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_id INTEGER NOT NULL, event_name TEXT NOT NULL,
            student_id TEXT NOT NULL, grade INTEGER NOT NULL, class INTEGER NOT NULL,
            number INTEGER NOT NULL, name TEXT NOT NULL, timestamp TEXT NOT NULL,
            operator TEXT NOT NULL, deleted_at TEXT NOT NULL, deleted_by TEXT NOT NULL)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS deleted_logs (
            archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_id INTEGER NOT NULL, timestamp TEXT NOT NULL,
            category TEXT NOT NULL, detail TEXT NOT NULL, operator TEXT NOT NULL,
            deleted_at TEXT NOT NULL, deleted_by TEXT NOT NULL)''')
        # 기존 DB를 유지하면서 행사 종류와 설명 컬럼만 필요한 경우 추가
        event_cols = {row[1] for row in cur.execute("PRAGMA table_info(events)").fetchall()}
        if 'event_type' not in event_cols:
            cur.execute("ALTER TABLE events ADD COLUMN event_type TEXT NOT NULL DEFAULT '기타'")
        if 'description' not in event_cols:
            cur.execute("ALTER TABLE events ADD COLUMN description TEXT NOT NULL DEFAULT ''")

def read_table(query):
    with get_db_connection() as conn:
        return pd.read_sql_query(query, conn)

def db_get_events(): return read_table("SELECT * FROM events ORDER BY id DESC")
def db_get_participants(): return read_table("SELECT * FROM participants ORDER BY id DESC")
def db_get_logs(): return read_table("SELECT * FROM logs ORDER BY id DESC")

def db_add_event(name, status, created_at, event_type="기타", description=""):
    with get_db_connection() as conn:
        conn.execute("INSERT INTO events (name,status,created_at,event_type,description) VALUES (?,?,?,?,?)", (name,status,created_at,event_type,description))

def db_update_event_status(event_id, new_status):
    with get_db_connection() as conn:
        conn.execute("UPDATE events SET status=? WHERE id=?", (new_status,event_id))

def db_update_event_details(event_id, old_name, new_name, event_type, description):
    """행사 정보를 수정하고, 행사명 변경 시 연결된 참여/보관 기록도 함께 갱신한다."""
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE events SET name=?, event_type=?, description=? WHERE id=?",
            (new_name, event_type, description, event_id),
        )
        if old_name != new_name:
            conn.execute("UPDATE participants SET event_name=? WHERE event_name=?", (new_name, old_name))
            conn.execute("UPDATE deleted_participants SET event_name=? WHERE event_name=?", (new_name, old_name))

def db_delete_event_and_records(event_id, event_name, deleted_by="관리자"):
    # 행사 삭제 시 연결된 참여 기록도 복구 보관함에 먼저 보관한다.
    with get_db_connection() as conn:
        participant_ids = [row[0] for row in conn.execute(
            "SELECT id FROM participants WHERE event_name=?", (event_name,)
        ).fetchall()]
    db_archive_and_delete_participants(participant_ids, deleted_by)
    with get_db_connection() as conn:
        conn.execute("DELETE FROM events WHERE id=?", (event_id,))

def db_add_participant(data):
    with get_db_connection() as conn:
        conn.execute('''INSERT INTO participants
            (event_name,student_id,grade,class,number,name,timestamp,operator)
            VALUES (?,?,?,?,?,?,?,?)''', (data['event_name'],data['student_id'],data['grade'],data['class'],data['number'],data['name'],data['timestamp'],data['operator']))

def db_participant_exists(event_name, student_id, exclude_id=None):
    """기존 호환용: 행사와 학번이 일치하는 참여 기록이 있는지 확인한다."""
    query = "SELECT 1 FROM participants WHERE event_name=? AND student_id=?"
    params = [str(event_name).strip(), str(student_id).strip()]
    if exclude_id is not None:
        query += " AND id<>?"
        params.append(int(exclude_id))
    query += " LIMIT 1"
    with get_db_connection() as conn:
        return conn.execute(query, params).fetchone() is not None

def db_count_participant_on_date(event_name, student_id, day):
    """해당 날짜에 같은 학생이 같은 행사에 등록한 횟수."""
    day_text = day.strftime("%Y-%m-%d") if hasattr(day, "strftime") else str(day)[:10]
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM participants WHERE event_name=? AND student_id=? AND timestamp LIKE ?",
            (str(event_name).strip(), str(student_id).strip(), f"{day_text}%"),
        ).fetchone()
    return int(row[0] or 0)

def db_archive_and_delete_participants(part_ids, deleted_by):
    """Move selected participation rows to a recovery table before deleting them."""
    ids = sorted({int(x) for x in part_ids})
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    deleted_at = now_seoul().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        rows = conn.execute(
            f"SELECT id,event_name,student_id,grade,class,number,name,timestamp,operator FROM participants WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        conn.executemany("""INSERT INTO deleted_participants
            (original_id,event_name,student_id,grade,class,number,name,timestamp,operator,deleted_at,deleted_by)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [(r[0], *r[1:], deleted_at, deleted_by) for r in rows])
        conn.executemany("DELETE FROM participants WHERE id=?", [(r[0],) for r in rows])
    return len(rows)

def db_restore_archived_participant(archive_id, restored_by):
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM deleted_participants WHERE archive_id=?", (int(archive_id),)).fetchone()
        if row is None:
            return False, "복구할 기록을 찾을 수 없습니다."
        # row: archive_id, original_id, event_name, student_id, grade, class, number, name, timestamp, operator, deleted_at, deleted_by
        conn.execute("""INSERT INTO participants (event_name,student_id,grade,class,number,name,timestamp,operator)
            VALUES (?,?,?,?,?,?,?,?)""", (row[2],row[3],row[4],row[5],row[6],row[7],row[8],row[9]))
        conn.execute("DELETE FROM deleted_participants WHERE archive_id=?", (int(archive_id),))
    db_add_log("참여 기록 복구", f"보관 기록 #{archive_id} 복구 완료", restored_by)
    return True, "기록을 복구했습니다."

def db_update_participant(part_id, event_name, student_id, grade, class_num, number, name):
    with get_db_connection() as conn:
        conn.execute('''UPDATE participants SET event_name=?,student_id=?,grade=?,class=?,number=?,name=? WHERE id=?''',
                     (event_name,student_id,grade,class_num,number,name,part_id))

def db_add_log(category, detail, operator):
    with get_db_connection() as conn:
        conn.execute("INSERT INTO logs (timestamp,category,detail,operator) VALUES (?,?,?,?)",
                     (now_seoul().strftime("%Y-%m-%d %H:%M:%S"),category,detail,operator))

def db_clear_logs():
    with get_db_connection() as conn:
        conn.execute("DELETE FROM logs")

def db_archive_and_delete_logs(log_ids, deleted_by):
    """Archive selected log rows before removing them from the active log list."""
    ids = sorted({int(x) for x in log_ids})
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    deleted_at = now_seoul().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        rows = conn.execute(
            f"SELECT id,timestamp,category,detail,operator FROM logs WHERE id IN ({placeholders})", ids
        ).fetchall()
        conn.executemany("""INSERT INTO deleted_logs
            (original_id,timestamp,category,detail,operator,deleted_at,deleted_by)
            VALUES (?,?,?,?,?,?,?)""",
            [(r[0], r[1], r[2], r[3], r[4], deleted_at, deleted_by) for r in rows])
        conn.executemany("DELETE FROM logs WHERE id=?", [(r[0],) for r in rows])
    return len(rows)

def db_restore_archived_log(archive_id, restored_by):
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM deleted_logs WHERE archive_id=?", (int(archive_id),)).fetchone()
        if row is None:
            return False, "복구할 처리 이력을 찾을 수 없습니다."
        conn.execute("INSERT INTO logs (timestamp,category,detail,operator) VALUES (?,?,?,?)", (row[2], row[3], row[4], row[5]))
        conn.execute("DELETE FROM deleted_logs WHERE archive_id=?", (int(archive_id),))
    db_add_log("처리 이력 복구", f"보관 이력 #{archive_id} 복구 완료", restored_by)
    return True, "처리 이력을 복구했습니다."

init_db()

# ------------------------------------------------------------
# Jaenact-inspired glassmorphism design, embedded in this file
# ------------------------------------------------------------
st.markdown(r'''<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css');
:root { --ink:#20232b; --muted:#737b8c; --blue:#3182F6; --blue2:#1B64DA; --line:rgba(133,145,169,.20); }
html, body, [class*="css"], [data-testid="stAppViewContainer"] { font-family: Pretendard, -apple-system, BlinkMacSystemFont, sans-serif; }
.stApp { color:var(--ink); background: radial-gradient(ellipse at 15% 28%,rgba(151,164,255,.19),transparent 34%), radial-gradient(ellipse at 82% 42%,rgba(164,205,255,.22),transparent 34%), linear-gradient(135deg,#f4f5fb 0%,#f9fafc 48%,#edf1f9 100%); }
[data-testid="stHeader"] { background:rgba(248,249,253,.78); backdrop-filter:blur(18px); }
[data-testid="stToolbar"] { right:1rem; }
.block-container { max-width:1320px; padding-top:1.4rem; padding-bottom:3rem; }
/* Top brand panel */
.brandbar { display:flex; justify-content:space-between; align-items:center; gap:1rem; padding:17px 24px; margin:0 0 26px; border:1px solid rgba(255,255,255,.88); border-radius:28px; background:rgba(255,255,255,.68); backdrop-filter:blur(20px); box-shadow:0 12px 34px rgba(61,73,104,.08); }
.brandname { font-size:19px; font-weight:800; letter-spacing:-.5px; color:#20232b; }
.branddesc { color:#737b8c; font-size:13px; }
.eyebrow { display:inline-block; color:#3182F6; font-size:12px; letter-spacing:2px; font-weight:800; text-transform:uppercase; padding-bottom:9px; border-bottom:2px solid #1B64DA; margin-bottom:15px; }
.hero-card, .glass-card { border:1px solid rgba(255,255,255,.92); border-radius:28px; background:rgba(255,255,255,.74); backdrop-filter:blur(18px); box-shadow:0 18px 50px rgba(51,65,97,.09); }
.hero-card { padding:42px 36px; text-align:center; }
.hero-title { font-size:clamp(30px,4vw,44px); line-height:1.15; letter-spacing:-1.7px; font-weight:850; margin:8px 0 12px; color:#20232b; }
.hero-subtitle { color:#687184; font-size:16px; line-height:1.7; margin:0 0 18px; }
.glass-card { padding:25px 27px; margin-bottom:18px; }
.section-title { font-size:27px; letter-spacing:-.8px; font-weight:820; margin:4px 0 22px; color:#20232b; }
.muted { color:#7b8291; }
.pill { display:inline-block; border:1px solid rgba(104,133,245,.18); color:#1B64DA; background:rgba(224,233,255,.72); padding:7px 13px; border-radius:999px; font-size:12px; font-weight:750; }
/* Streamlit buttons */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button { width:100%; min-height:46px; border-radius:15px; border:1px solid rgba(255,255,255,.9); background:linear-gradient(135deg,#3182F6,#1B64DA); color:#fff; font-weight:750; box-shadow:0 8px 20px rgba(49,130,246,.22); transition:transform .18s ease,box-shadow .18s ease,filter .18s ease; }
.stButton > button p, .stButton > button span, .stDownloadButton > button p, .stFormSubmitButton > button p, .stFormSubmitButton > button span { color:#fff !important; font-weight:750; }
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover { transform:translateY(-1px); box-shadow:0 12px 26px rgba(49,130,246,.30); border-color:rgba(255,255,255,.95); filter:saturate(1.08); }
.stButton > button:focus, .stDownloadButton > button:focus, .stFormSubmitButton > button:focus { outline:2px solid rgba(49,130,246,.25); outline-offset:2px; }
/* Inputs */
[data-testid="stTextInput"] input, [data-testid="stNumberInput"] input, [data-testid="stDateInput"] input, [data-testid="stTimeInput"] input, [data-testid="stSelectbox"] [data-baseweb="select"] > div, [data-testid="stMultiSelect"] [data-baseweb="select"] > div { border-radius:13px !important; border-color:#dfe4ef !important; background:rgba(255,255,255,.82) !important; min-height:43px; }
[data-testid="stTextInput"] input:focus, [data-testid="stNumberInput"] input:focus { border-color:#3182F6 !important; box-shadow:0 0 0 2px rgba(49,130,246,.12) !important; }
label, [data-testid="stWidgetLabel"] p { color:#424a5b !important; font-weight:650 !important; }
/* Tabs */
[data-testid="stTabs"] [data-baseweb="tab-list"] { gap:7px; padding:7px; border:1px solid rgba(255,255,255,.95); border-radius:20px; background:rgba(255,255,255,.58); }
[data-testid="stTabs"] button[role="tab"] { border-radius:14px; padding:11px 15px; color:#626b7b; }
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] { background:#fff; box-shadow:0 5px 15px rgba(70,84,120,.12); color:#1B64DA; }
[data-testid="stTabs"] [data-baseweb="tab-highlight"] { background:transparent; }
/* Data tables and alerts */
[data-testid="stDataFrame"], [data-testid="stTable"] { border:1px solid #e6eaf2; border-radius:16px; overflow:hidden; }
[data-testid="stAlert"] { border-radius:14px; }
[data-testid="stExpander"] { border:1px solid #e2e7f0; border-radius:15px; background:rgba(255,255,255,.56); }
hr { border-color:rgba(125,139,165,.18) !important; }
h1,h2,h3,h4 { color:#20232b; letter-spacing:-.65px; }
[data-testid="stTextInput"] input { min-height:54px !important; font-size:17px !important; }
[data-testid="stSelectbox"] [data-baseweb="select"] > div { min-height:54px !important; font-size:16px !important; }
.stMetric { background:rgba(255,255,255,.78); border:1px solid rgba(255,255,255,.95); border-radius:20px; padding:18px 20px; box-shadow:0 10px 26px rgba(51,65,97,.06); }
@media (max-width: 760px) { .block-container { padding-left:.65rem !important; padding-right:.65rem !important; padding-top:.65rem; } .brandbar { padding:15px 17px; border-radius:20px; align-items:flex-start; flex-direction:column; } .hero-card { padding:28px 19px; } .glass-card { padding:19px; } .section-title { font-size:24px; } [data-testid="stTabs"] [data-baseweb="tab-list"] { flex-wrap:wrap; } [data-testid="stDataFrame"] { max-width:100%; } [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) { display:grid !important; grid-template-columns:repeat(4,minmax(0,1fr)) !important; grid-auto-flow:row !important; column-gap:3px !important; row-gap:3px !important; align-items:stretch !important; width:100% !important; max-width:100% !important; margin-left:0 !important; margin-right:0 !important; } [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) > [data-testid="column"] { min-width:0 !important; width:auto !important; max-width:none !important; flex:none !important; padding:0 !important; margin:0 !important; } [data-testid="stMetric"] { box-sizing:border-box !important; min-width:0 !important; width:100% !important; max-width:none !important; aspect-ratio:auto !important; min-height:82px !important; padding:9px 3px !important; margin:0 !important; border-radius:11px !important; box-shadow:0 3px 10px rgba(51,65,97,.05) !important; display:flex !important; flex-direction:column !important; justify-content:center !important; overflow:hidden !important; } [data-testid="stMetricLabel"] { font-size:clamp(9px, 2.9vw, 11px) !important; line-height:1.25 !important; font-weight:650 !important; white-space:nowrap !important; overflow:visible !important; text-overflow:clip !important; word-break:keep-all !important; letter-spacing:-.35px !important; } [data-testid="stMetricValue"] { font-size:clamp(13px, 3.8vw, 16px) !important; line-height:1.25 !important; font-weight:800 !important; white-space:nowrap !important; overflow:visible !important; text-overflow:clip !important; letter-spacing:-.4px !important; } [data-testid="stMetricValue"] > div { font-weight:800 !important; } [data-testid="stMetricDelta"] { font-size:9px !important; } }
@media (hover: none) and (pointer: coarse) { .stButton > button, .stDownloadButton > button, .stFormSubmitButton > button { min-height:52px; } [data-testid="stTextInput"] input { min-height:58px !important; font-size:18px !important; } }
/* Consistent responsive behavior for iOS Safari, Android browsers, and desktop */
*, *::before, *::after { box-sizing:border-box; }
img, video, iframe { max-width:100%; }
[data-testid="stAppViewContainer"] { overflow-x:hidden; }
[data-testid="stHorizontalBlock"] { max-width:100%; }

/* Subtle transitions for layout changes and interaction feedback.
   Streamlit reruns still rebuild widgets, so keep motion short and lightweight. */
[data-testid="stHorizontalBlock"],
[data-testid="column"],
[data-testid="stVerticalBlockBorderWrapper"],
[data-testid="stTextInput"],
[data-testid="stButton"],
[data-testid="stFormSubmitButton"],
[data-testid="stSelectbox"],
[data-testid="stDateInput"],
[data-testid="stTimeInput"] {
  transition: opacity .18s ease, transform .18s ease, box-shadow .18s ease;
}
.glass-card { animation: softEnter .2s ease both; }
@keyframes softEnter {
  from { opacity: .72; transform: translateY(3px); }
  to { opacity: 1; transform: translateY(0); }
}
.stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
  transition: transform .14s ease, box-shadow .14s ease, filter .14s ease;
  -webkit-tap-highlight-color: transparent;
}
[data-testid="stTextInput"] input,
[data-testid="stSelectbox"] [data-baseweb="select"] > div,
[data-testid="stDateInput"] input,
[data-testid="stTimeInput"] input {
  transition: border-color .14s ease, box-shadow .14s ease, background-color .14s ease;
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; scroll-behavior: auto !important; }
}
[data-testid="stButton"] button, [data-testid="stDownloadButton"] button { touch-action:manipulation; }
@supports (-webkit-touch-callout: none) { input, textarea, select { font-size:16px !important; } }
@media (max-width:760px) {
  .block-container { width:100% !important; max-width:100% !important; padding-left:1rem !important; padding-right:1rem !important; }
  .section-title { overflow-wrap:anywhere; }
  [data-testid="stHorizontalBlock"] { gap:.5rem !important; }
  [data-testid="stTabs"] [data-baseweb="tab-list"] { overflow-x:auto !important; flex-wrap:nowrap !important; scrollbar-width:thin; }
  [data-testid="stTabs"] button[role="tab"] { flex:0 0 auto; white-space:nowrap; }
}

/* Apple-inspired visual layer: restrained SF typography, neutral surfaces, crisp controls */
:root { --ink:#1d1d1f; --muted:#6e6e73; --blue:#0071e3; --blue2:#0066cc; --line:#d2d2d7; }
html, body, [class*="css"], [data-testid="stAppViewContainer"] { font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text",Pretendard,sans-serif !important; }
.stApp { color:#1d1d1f !important; background:#f5f5f7 !important; }
[data-testid="stHeader"] { background:rgba(245,245,247,.82) !important; backdrop-filter:saturate(180%) blur(20px); }
.brandbar { border:1px solid rgba(0,0,0,.04) !important; border-radius:20px !important; background:rgba(255,255,255,.86) !important; backdrop-filter:saturate(180%) blur(20px); box-shadow:0 2px 10px rgba(0,0,0,.035) !important; }
.brandname,.hero-title,.section-title,h1,h2,h3,h4 { color:#1d1d1f !important; letter-spacing:-.55px !important; }
.brandname { font-weight:750 !important; }
.branddesc,.hero-subtitle,.muted { color:#6e6e73 !important; }
.eyebrow { color:#0066cc !important; border-bottom:1px solid #0071e3 !important; letter-spacing:1.4px !important; }
.hero-card,.glass-card { background:rgba(255,255,255,.96) !important; border:1px solid rgba(0,0,0,.045) !important; border-radius:24px !important; box-shadow:0 3px 14px rgba(0,0,0,.035) !important; backdrop-filter:none !important; }
.hero-title { font-weight:760 !important; }
.pill { color:#0066cc !important; background:#f0f7ff !important; border-color:rgba(0,113,227,.13) !important; }
.stButton > button,.stDownloadButton > button,.stFormSubmitButton > button { background:#0071e3 !important; border:1px solid #0071e3 !important; color:#fff !important; border-radius:12px !important; box-shadow:none !important; font-weight:650 !important; transition:background-color .16s ease,transform .16s ease,box-shadow .16s ease !important; }
.stButton > button p,.stButton > button span,.stDownloadButton > button p,.stFormSubmitButton > button p,.stFormSubmitButton > button span { color:#fff !important; font-weight:650 !important; }
.stButton > button:hover,.stDownloadButton > button:hover,.stFormSubmitButton > button:hover { background:#0066cc !important; border-color:#0066cc !important; box-shadow:0 2px 7px rgba(0,113,227,.18) !important; }
.stButton > button:active,.stDownloadButton > button:active,.stFormSubmitButton > button:active { transform:scale(.99); }
[data-testid="stTextInput"] input,[data-testid="stNumberInput"] input,[data-testid="stDateInput"] input,[data-testid="stTimeInput"] input,[data-testid="stSelectbox"] [data-baseweb="select"] > div,[data-testid="stMultiSelect"] [data-baseweb="select"] > div { border-radius:12px !important; border-color:#d2d2d7 !important; background:#fff !important; }
[data-testid="stTextInput"] input:focus,[data-testid="stNumberInput"] input:focus { border-color:#0071e3 !important; box-shadow:0 0 0 3px rgba(0,113,227,.14) !important; }
label,[data-testid="stWidgetLabel"] p { color:#424245 !important; font-weight:600 !important; }
[data-testid="stTabs"] [data-baseweb="tab-list"] { gap:5px !important; padding:5px !important; border:1px solid rgba(0,0,0,.04) !important; border-radius:14px !important; background:#e8e8ed !important; }
[data-testid="stTabs"] button[role="tab"] { border-radius:10px !important; color:#515154 !important; }
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] { background:#fff !important; color:#1d1d1f !important; box-shadow:0 1px 4px rgba(0,0,0,.08) !important; }
[data-testid="stDataFrame"],[data-testid="stTable"] { border:1px solid #e5e5ea !important; border-radius:14px !important; overflow:hidden; }
[data-testid="stExpander"] { border:1px solid #e5e5ea !important; border-radius:14px !important; background:#fff !important; }
.stMetric { background:#fff !important; border:1px solid rgba(0,0,0,.045) !important; border-radius:18px !important; box-shadow:0 2px 10px rgba(0,0,0,.03) !important; }
hr { border-color:#e5e5ea !important; }
@media(max-width:760px){.brandbar{border-radius:16px !important}.hero-card,.glass-card{border-radius:18px !important}}
</style>''', unsafe_allow_html=True)

# 접근성·기기 호환 설정: Streamlit 위젯 기반 테마 전환
if "dark_mode" not in st.session_state:
    st.session_state.dark_mode = False

theme_spacer, theme_control = st.columns([7, 1.5], gap="small")
with theme_control:
    st.toggle("🌙 다크 모드", key="dark_mode", help="밝은 화면과 어두운 화면을 전환합니다.")

if st.session_state.dark_mode:
    st.markdown(r'''<style>
    :root { color-scheme: dark; --ink:#edf2fa; --muted:#a8b3c5; --line:rgba(170,185,210,.20); }
    html, body, [data-testid="stAppViewContainer"], .stApp {
      color:#edf2fa !important;
      background:radial-gradient(ellipse at 15% 28%,rgba(52,75,135,.20),transparent 34%),
                 radial-gradient(ellipse at 82% 42%,rgba(38,83,132,.18),transparent 34%),
                 linear-gradient(135deg,#111722 0%,#151c28 48%,#101722 100%) !important;
    }
    [data-testid="stHeader"] { background:rgba(17,23,34,.88) !important; }
    [data-testid="stAppViewContainer"] section, [data-testid="stMain"] { color:#edf2fa !important; }
    .brandbar,.hero-card,.glass-card,[data-testid="stExpander"],
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
      background:rgba(31,41,57,.90) !important; border-color:rgba(170,185,210,.18) !important;
      box-shadow:0 12px 34px rgba(0,0,0,.20) !important;
    }
    .brandname,.hero-title,.section-title,h1,h2,h3,h4,p,li,label,
    [data-testid="stWidgetLabel"] p,[data-testid="stMarkdownContainer"] { color:#edf2fa; }
    .branddesc,.hero-subtitle,.muted { color:#a8b3c5 !important; }
    .pill { color:#a9caff !important; background:rgba(49,130,246,.16) !important; border-color:rgba(115,164,255,.30) !important; }
    [data-testid="stTextInput"] input,[data-testid="stNumberInput"] input,
    [data-testid="stDateInput"] input,[data-testid="stTimeInput"] input,
    [data-testid="stSelectbox"] [data-baseweb="select"] > div,
    [data-testid="stMultiSelect"] [data-baseweb="select"] > div,
    [data-testid="stTextArea"] textarea {
      color:#edf2fa !important; background:#202a3a !important; border-color:#3a4960 !important;
      -webkit-text-fill-color:#edf2fa !important;
    }
    input::placeholder,textarea::placeholder { color:#a8b3c5 !important; -webkit-text-fill-color:#a8b3c5 !important; }
    [data-testid="stTabs"] button[role="tab"] { color:#c1cada !important; }
    [data-testid="stTabs"] button[role="tab"][aria-selected="true"] { background:#29374b !important; color:#a9caff !important; }
    [data-testid="stMetric"] { background:#202a3a !important; border-color:#354258 !important; }
    [data-testid="stMetricLabel"],[data-testid="stMetricValue"],[data-testid="stMetricDelta"] { color:#edf2fa !important; }
    [data-testid="stDataFrame"],[data-testid="stTable"] { border-color:#354258 !important; }
    [data-testid="stAlert"] p,[data-testid="stExpander"] summary,[data-testid="stExpander"] summary p { color:#edf2fa !important; }
    [data-baseweb="popover"] > div,[data-baseweb="menu"] { background:#202a3a !important; }
    [role="option"],[role="listbox"] { color:#edf2fa !important; }
    hr { border-color:rgba(170,185,210,.20) !important; } a { color:#8dbaff !important; }
    @media (max-width:760px) {
      .block-container { padding-left:1rem !important; padding-right:1rem !important; }
      .brandbar,.hero-card,.glass-card { border-radius:18px !important; }
      [data-testid="stTabs"] [data-baseweb="tab-list"] { gap:4px !important; }
      [data-testid="stTabs"] button[role="tab"] { padding:9px 10px !important; }
    }
    
/* Apple-inspired dark appearance */
:root { color-scheme:dark; --ink:#f5f5f7; --muted:#a1a1a6; --line:#424245; }
html,body,[data-testid="stAppViewContainer"],.stApp { color:#f5f5f7 !important; background:#000 !important; }
[data-testid="stHeader"] { background:rgba(0,0,0,.78) !important; backdrop-filter:saturate(180%) blur(20px); }
.brandbar,.hero-card,.glass-card,[data-testid="stExpander"],[data-testid="stTabs"] [data-baseweb="tab-list"] { background:rgba(28,28,30,.97) !important; border-color:rgba(255,255,255,.09) !important; box-shadow:0 2px 12px rgba(0,0,0,.18) !important; }
.brandname,.hero-title,.section-title,h1,h2,h3,h4,p,li,label,[data-testid="stWidgetLabel"] p,[data-testid="stMarkdownContainer"] { color:#f5f5f7 !important; }
.branddesc,.hero-subtitle,.muted { color:#a1a1a6 !important; }
[data-testid="stTextInput"] input,[data-testid="stNumberInput"] input,[data-testid="stDateInput"] input,[data-testid="stTimeInput"] input,[data-testid="stSelectbox"] [data-baseweb="select"] > div,[data-testid="stMultiSelect"] [data-baseweb="select"] > div,[data-testid="stTextArea"] textarea { color:#f5f5f7 !important; background:#1c1c1e !important; border-color:#48484a !important; -webkit-text-fill-color:#f5f5f7 !important; }
input::placeholder,textarea::placeholder { color:#a1a1a6 !important; -webkit-text-fill-color:#a1a1a6 !important; }
label,[data-testid="stWidgetLabel"] p { color:#d1d1d6 !important; }
.pill { color:#64b5ff !important; background:rgba(0,113,227,.16) !important; border-color:rgba(100,181,255,.2) !important; }
[data-testid="stTabs"] button[role="tab"] { color:#d1d1d6 !important; }
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] { background:#3a3a3c !important; color:#fff !important; }
[data-testid="stMetric"] { background:#1c1c1e !important; border-color:#38383a !important; }
[data-testid="stMetricLabel"],[data-testid="stMetricValue"],[data-testid="stMetricDelta"] { color:#f5f5f7 !important; }
[data-testid="stDataFrame"],[data-testid="stTable"] { border-color:#38383a !important; }
[data-testid="stAlert"] p,[data-testid="stExpander"] summary,[data-testid="stExpander"] summary p { color:#f5f5f7 !important; }
[data-baseweb="popover"] > div,[data-baseweb="menu"] { background:#1c1c1e !important; }
[role="option"],[role="listbox"] { color:#f5f5f7 !important; }
hr { border-color:#38383a !important; } a { color:#64b5ff !important; }
.stButton > button,.stDownloadButton > button,.stFormSubmitButton > button { background:#0071e3 !important; border-color:#0071e3 !important; }
.stButton > button:hover,.stDownloadButton > button:hover,.stFormSubmitButton > button:hover { background:#2997ff !important; border-color:#2997ff !important; }
</style>''', unsafe_allow_html=True)

st.markdown('''<div class="brandbar"><div class="brandname">Songcheon <span style="color:#3182F6">Lib Tech</span></div><div class="branddesc">인천송천고등학교 · 행사 관리 시스템</div></div>''', unsafe_allow_html=True)

def parse_student_id(std_id):
    value = str(std_id).strip()
    if len(value) == 5 and value.isdigit():
        return True, int(value[0]), int(value[1:3]), int(value[3:5])
    return False, None, None, None

def find_student_name(student_id):
    sid = str(student_id).strip()
    if not sid:
        return ""
    df = db_get_participants()
    if df.empty:
        return ""
    matches = df[df["student_id"].astype(str).str.strip() == sid]
    if matches.empty:
        return ""
    names = matches["name"].dropna().astype(str).str.strip()
    names = names[names != ""]
    return names.iloc[0] if not names.empty else ""

def fill_participant_name():
    sid = st.session_state.get("participant_sid", "").strip()
    matched_name = find_student_name(sid) if len(sid) == 5 and sid.isdigit() else ""
    # 학번이 바뀌었는데 이전 학생 이름이 남아 잘못 등록되는 것을 방지
    st.session_state["participant_name"] = matched_name

def desktop_quick_sid_changed():
    sid = str(st.session_state.get("desktop_quick_sid", "")).strip()
    if len(sid) == 5 and sid.isdigit():
        st.session_state["desktop_quick_name"] = find_student_name(sid)
        st.session_state["desktop_focus_name"] = True
    else:
        st.session_state["desktop_quick_name"] = ""
        st.session_state["desktop_focus_name"] = False

def desktop_quick_name_entered():
    st.session_state["desktop_quick_submit_pending"] = True

def fill_staff_student_name():
    sid = st.session_state.get("staff_search_id", "").strip()
    matched_name = find_student_name(sid) if len(sid) == 5 and sid.isdigit() else ""
    if matched_name:
        st.session_state["staff_search_name"] = matched_name

def event_options(df):
    return [f"[{r['status']}] {r['name']}" for _, r in df.iterrows()] if not df.empty else ["등록된 행사 없음"]
def clean_event(value):
    return value.split("] ", 1)[-1] if "] " in value else value

if 'user_role' not in st.session_state: st.session_state.user_role = None
if 'staff_info' not in st.session_state: st.session_state.staff_info = None

# Keep the selected role across refreshes with a query parameter.
# This is convenience persistence only, not an authentication/security mechanism.
try:
    saved_role = st.query_params.get("role", "")
except Exception:
    saved_role = ""
if st.session_state.user_role is None and saved_role in ("staff", "participant"):
    st.session_state.user_role = saved_role
    # Staff identity is kept in session state only; after a refresh use a generic label.
    if saved_role == "staff" and not st.session_state.staff_info:
        st.session_state.staff_info = "관리자"

def persist_login(role, staff_info=None):
    try:
        st.query_params["role"] = role
    except Exception:
        pass

def clear_persisted_login():
    st.session_state.logout_requested = True
    st.session_state.user_role = None
    st.session_state.staff_info = None
    try:
        st.query_params.clear()
    except Exception:
        try:
            del st.query_params["role"]
        except Exception:
            pass

# ------------------------------------------------------------
# Main landing screen
# ------------------------------------------------------------
if st.session_state.user_role is None:
    spacer1, main, spacer2 = st.columns([1, 1.35, 1])
    with main:
        st.markdown('''<div class="hero-card"><div class="eyebrow">EVENT MANAGEMENT</div><div class="hero-title">인천송천고 도서관</div><p class="hero-subtitle">행사 참여 등록부터 통합 데이터 관리까지<br>한곳에서 간편하게 관리하세요.</p><span class="pill">SONGCHEON EVENT SYSTEM</span></div>''', unsafe_allow_html=True)
        st.write("")
        with st.expander("📘 사용 설명 보기", expanded=False):
            st.markdown("""
            **1. 학생 · 빠른 등록**
            - `빠른 등록`을 선택합니다.
            - 학년, 반, 번호를 버튼으로 선택합니다. 이름은 기존 등록 기록에 있으면 선택할 수 있으며, 몰라도 건너뛸 수 있습니다.
            - 참여할 행사를 고른 뒤 등록합니다. 같은 날 같은 행사에 이미 등록한 경우 경고가 표시됩니다.

            **2. 사서교사 · 도서부원**
            - 관리자 로그인을 선택하고 직위와 담당자 정보를 입력합니다.
            - **참여 등록** 탭에서 학생 정보를 확인하고 행사 참여를 등록할 수 있습니다.
            - **행사 관리**에서 행사를 개설·수정·마감할 수 있고, **통계 대시보드**에서 참여 현황을 확인할 수 있습니다.

            **3. 유의 사항**
            - 행사 목록에 등록할 행사가 없거나 행사가 마감된 경우 관리자에게 문의하세요.
            - 학번과 이름을 정확히 확인한 뒤 등록해 주세요.
            """)
        if st.button("빠른 등록", use_container_width=True):
            st.session_state.logout_requested = False; st.session_state.user_role = 'participant'; persist_login('participant'); st.rerun()
        if st.button("사서교사 · 도서부원 관리자 로그인", use_container_width=True):
            st.session_state.logout_requested = False; st.session_state.user_role = 'staff_login'; st.rerun()

# ------------------------------------------------------------
# Staff login
# ------------------------------------------------------------
elif st.session_state.user_role == 'staff_login':
    _, main, _ = st.columns([1, 1.25, 1])
    with main:
        st.markdown('<div class="eyebrow">STAFF LOGIN</div><div class="section-title">관리자 로그인</div><p class="muted">담당자 정보를 입력해 로그인해 주세요.</p>', unsafe_allow_html=True)
        staff_type = st.selectbox("직위 구분", ["도서부원", "사서교사"])
        if staff_type == "도서부원":
            staff_std_id = st.text_input("학번 (5자리)", placeholder="예: 10101", max_chars=5)
            staff_name = st.text_input("이름", placeholder="이름을 입력하세요")
        else:
            staff_std_id = "교직원"
            staff_name = st.text_input("이름", placeholder="담당 선생님 이름")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("관리자 접속"):
                valid, *_ = parse_student_id(staff_std_id) if staff_type == "도서부원" else (True, None, None, None)
                if not valid or not staff_name.strip(): st.error("올바른 학번과 이름을 입력해 주세요.")
                else:
                    st.session_state.staff_info = f"{staff_type} {staff_std_id} {staff_name.strip()}" if staff_type == "도서부원" else f"사서교사 {staff_name.strip()}"
                    st.session_state.logout_requested = False
                    st.session_state.user_role = 'staff'
                    persist_login('staff', st.session_state.staff_info)
                    db_add_log("시스템 접속", f"{st.session_state.staff_info} 접속함", st.session_state.staff_info)
                    st.rerun()
        with c2:
            if st.button("메인으로", key="login_home"):
                st.session_state.user_role = None; clear_persisted_login(); st.rerun()

# ------------------------------------------------------------
# Participant quick registration: same typing form on desktop and mobile
# ------------------------------------------------------------
elif st.session_state.user_role == 'participant':
    top1, top2 = st.columns([5, 1])
    with top1:
        st.markdown('<div class="eyebrow">QUICK REGISTRATION</div><div class="section-title">빠른 등록</div>', unsafe_allow_html=True)
    with top2:
        if st.button("메인으로", key="participant_home"):
            st.session_state.user_role = None
            clear_persisted_login()
            st.rerun()

    df_events = db_get_events()
    selected = st.selectbox("참여할 행사", event_options(df_events), key="participant_event")
    st.session_state.setdefault("quick_sid_input", "")
    st.session_state.setdefault("quick_name_input", "")

    # Handle registration and duplicate-confirm actions before creating input widgets,
    # so successful registration can safely clear their session-state values.
    if st.session_state.pop("quick_registration_submit_pending", False):
        sid = str(st.session_state.get("quick_sid_input", "")).strip()
        name = str(st.session_state.get("quick_name_input", "")).strip() or "-"
        valid, grade, class_num, number = parse_student_id(sid)
        if "행사 없음" in selected:
            st.session_state["participant_registration_flash"] = "등록 가능한 행사가 없습니다."
        elif "[마감]" in selected:
            st.session_state["participant_registration_flash"] = "마감된 행사입니다. 도서부원에게 문의해 주세요."
        elif not valid:
            st.session_state["participant_registration_flash"] = "학번 5자리를 정확히 입력해 주세요."
        else:
            event = clean_event(selected)
            participant_day = now_seoul().date()
            if db_count_participant_on_date(event, sid, participant_day) > 0:
                st.session_state["pending_participant_duplicate"] = (event, sid, participant_day.isoformat(), name)
            else:
                db_add_participant({"event_name":event,"student_id":sid,"grade":grade,"class":class_num,"number":number,"name":name,"timestamp":now_seoul().strftime("%Y-%m-%d %H:%M:%S"),"operator":"참여자 본인"})
                db_add_log("참여자 등록", f"[{event}] {sid} {name} 등록 완료", "참여자 본인")
                st.session_state["participant_registration_flash"] = f"학번 {sid} 등록이 완료되었습니다!"
                st.session_state["quick_sid_input"] = ""
                st.session_state["quick_name_input"] = ""

    if st.session_state.pop("quick_duplicate_confirm_pending", False):
        pending = st.session_state.get("pending_participant_duplicate")
        if pending and pending[0] == clean_event(selected):
            event, pending_sid, pending_day, pending_name = pending
            valid, grade, class_num, number = parse_student_id(pending_sid)
            registered_name = str(pending_name or "-").strip() or "-"
            db_add_participant({"event_name":event,"student_id":pending_sid,"grade":grade,"class":class_num,"number":number,"name":registered_name,"timestamp":now_seoul().strftime("%Y-%m-%d %H:%M:%S"),"operator":"참여자 본인"})
            db_add_log("참여자 등록", f"[{event}] {pending_sid} {registered_name} 중복 등록 완료", "참여자 본인")
            st.session_state.pop("pending_participant_duplicate", None)
            st.session_state["participant_registration_flash"] = f"학번 {pending_sid} 등록이 완료되었습니다!"
            st.session_state["quick_sid_input"] = ""
            st.session_state["quick_name_input"] = ""

    if st.session_state.pop("quick_duplicate_cancel_pending", False):
        st.session_state.pop("pending_participant_duplicate", None)

    st.markdown('<div class="glass-card"><b>참여자 정보</b></div>', unsafe_allow_html=True)
    with st.form("participant_quick_registration_form", clear_on_submit=False):
        st.text_input("학번 (5자리)", max_chars=5, placeholder="예: 10101", key="quick_sid_input")
        st.text_input("이름 (선택)", placeholder="이름을 입력하세요. 비워 두면 - 로 등록됩니다.", key="quick_name_input")
        submitted = st.form_submit_button("등록", type="primary", use_container_width=True)
    if submitted:
        st.session_state["quick_registration_submit_pending"] = True
        st.rerun()

    pending = st.session_state.get("pending_participant_duplicate")
    is_pending_participant = bool(pending and pending[0] == clean_event(selected))
    if is_pending_participant:
        st.warning("오늘 이 행사에 이미 등록한 기록이 있습니다. 중복 등록하시겠습니까?")
        confirm_col, cancel_col = st.columns(2)
        with confirm_col:
            if st.button("네, 중복 등록", key="participant_confirm_duplicate", use_container_width=True, type="primary"):
                st.session_state["quick_duplicate_confirm_pending"] = True
                st.rerun()
        with cancel_col:
            if st.button("아니요, 취소", key="participant_cancel_duplicate", use_container_width=True):
                st.session_state["quick_duplicate_cancel_pending"] = True
                st.rerun()

    if st.session_state.get("participant_registration_flash"):
        st.success(st.session_state.pop("participant_registration_flash"))

# ------------------------------------------------------------
# Staff administration dashboard
# ------------------------------------------------------------
elif st.session_state.user_role == 'staff':
    h1, h2 = st.columns([5, 1])
    with h1:
        st.markdown(f'<div class="eyebrow">ADMIN WORKSPACE</div><div class="section-title">행사 관리 시스템</div><p class="muted">접속 계정: {st.session_state.staff_info}</p>', unsafe_allow_html=True)
    with h2:
        if st.button("로그아웃"):
            st.session_state.user_role = None; st.session_state.staff_info = None; clear_persisted_login(); st.rerun()
    # 관리자 첫 화면에서 핵심 현황을 바로 확인
    overview_p = db_get_participants()
    overview_e = db_get_events()
    today_key = now_seoul().strftime("%Y-%m-%d")
    today_count = int(overview_p["timestamp"].astype(str).str.startswith(today_key).sum()) if not overview_p.empty else 0
    active_events = int((overview_e["status"] == "진행 중").sum()) if not overview_e.empty else 0
    overview_cols = st.columns([1, 1, 1, 1], gap="small")
    overview_cols[0].metric("오늘 등록", f"{today_count:,}건")
    overview_cols[1].metric("전체 기록", f"{len(overview_p):,}건")
    overview_cols[2].metric("진행 행사", f"{active_events:,}개")
    overview_cols[3].metric("참여 학생", f"{overview_p['student_id'].nunique():,}명" if not overview_p.empty else "0명")

    tab1, tab2, tab3, tab4, tab5 = st.tabs(["참여 등록", "행사 관리", "통계 대시보드", "처리 이력", "백업/복원"])

    with tab1:
        # 검색 성공 후에는 다음 실행에서 위젯이 생성되기 전에 입력값을 비운다.
        if st.session_state.pop("clear_staff_search_inputs", False):
            st.session_state["staff_search_id"] = ""
            st.session_state["staff_search_name"] = ""

        df_p = db_get_participants(); df_e = db_get_events()

        # 검색 결과는 검색 버튼을 눌렀을 때 고정해 우측 이력 영역에 표시
        if "staff_history_search" not in st.session_state:
            st.session_state.staff_history_search = None

        def reset_staff_student_search():
            st.session_state["staff_search_id"] = ""
            st.session_state["staff_search_name"] = ""
            st.session_state["staff_history_search"] = None
            st.session_state.pop("pending_admin_duplicate", None)

        left, right = st.columns([0.82, 2.18], gap="large")
        with left:
            st.markdown('<div class="eyebrow">STUDENT LOOKUP</div><h3>참여자 정보</h3>', unsafe_allow_html=True)
            saved_student = st.session_state.get("staff_history_search")
            if saved_student:
                st.markdown(f'<div class="glass-card"><b>학번</b>　{saved_student[0]}<br><b>이름</b>　{saved_student[1]}</div>', unsafe_allow_html=True)
            search_id = st.text_input("학번 입력 (5자리)", key="staff_search_id", placeholder="예: 10101", max_chars=5, on_change=fill_staff_student_name)
            search_name = st.text_input("이름 입력", key="staff_search_name", placeholder="학생 이름")
            search_btn_col, reset_btn_col = st.columns(2, gap="small")
            with search_btn_col:
                search_clicked = st.button("학생 검색", key="staff_student_search_button", use_container_width=True, type="primary")
            with reset_btn_col:
                st.button("초기화", key="staff_student_reset_button", use_container_width=True, on_click=reset_staff_student_search)
            if search_clicked:
                if not search_id.strip() or not search_name.strip():
                    st.warning("학번과 이름을 모두 입력해 주세요.")
                    st.session_state.staff_history_search = None
                else:
                    st.session_state.staff_history_search = (search_id.strip(), search_name.strip())
                    st.session_state["clear_staff_search_inputs"] = True
                    st.rerun()

        # 검색 입력칸은 비워도 선택된 학생 정보는 등록 처리에 계속 사용한다.
        selected_student = st.session_state.get("staff_history_search")
        active_search_id = selected_student[0] if selected_student else search_id.strip()
        active_search_name = selected_student[1] if selected_student else search_name.strip()

        with right:
            st.markdown('<div class="eyebrow">PARTICIPATION</div><h3>행사 참여 등록</h3>', unsafe_allow_html=True)
            # 한 행에 행사, 수동 지정 일시, 등록 버튼을 배치한다.
            with st.form("staff_participation_registration_form", clear_on_submit=False):
                event_col, date_col, time_col, submit_col = st.columns([1.45, 1.0, 0.9, 0.85], gap="small", vertical_alignment="bottom")
                with event_col:
                    selected = st.selectbox("행사 입력", event_options(df_e), key="staff_event")
                with date_col:
                    cdate = st.date_input("등록 날짜", value=now_seoul().date(), key="retro_date")
                with time_col:
                    ctime = st.time_input("등록 시간", value=now_seoul().time().replace(tzinfo=None, microsecond=0), key="retro_time")
                with submit_col:
                    submit_admin = st.form_submit_button("등록", use_container_width=True, type="primary")

            admin_day = cdate
            pending_admin = (clean_event(selected), active_search_id, admin_day.isoformat())
            is_pending_admin = st.session_state.get("pending_admin_duplicate") == pending_admin
            confirm_admin = False
            cancel_admin = False
            if is_pending_admin:
                st.warning("⚠️ 선택한 날짜에 해당 학생의 행사 참여 기록이 이미 있습니다. 중복 등록하시겠습니까?")
                confirm_col, cancel_col = st.columns(2)
                with confirm_col:
                    confirm_admin = st.button("네, 중복 등록", key="staff_attendance_confirm_duplicate", use_container_width=True)
                with cancel_col:
                    cancel_admin = st.button("아니요, 취소", key="staff_attendance_cancel_duplicate", use_container_width=True)
            if cancel_admin:
                st.session_state.pop("pending_admin_duplicate", None)
                st.info("중복 등록을 취소했습니다.")
                st.rerun()
            if submit_admin or confirm_admin:
                valid, grade, class_num, number = parse_student_id(active_search_id)
                if "행사 없음" in selected: st.error("행사를 선택해 주세요.")
                elif not valid: st.error("왼쪽에서 올바른 5자리 학번을 입력하고 학생 검색을 해 주세요.")
                elif not active_search_name and not find_student_name(active_search_id): st.error("왼쪽에서 학생을 검색하고 이름을 확인해 주세요.")
                elif "[마감]" in selected: st.error("마감된 행사입니다. 행사 상태를 확인해 주세요.")
                else:
                    event = clean_event(selected)
                    same_day_count = db_count_participant_on_date(event, active_search_id, admin_day)
                    if same_day_count > 0 and not confirm_admin:
                        st.session_state["pending_admin_duplicate"] = (event, active_search_id, admin_day.isoformat())
                        st.rerun()
                    else:
                        registered_name = active_search_name or find_student_name(active_search_id) or "이름 미입력"
                        db_add_participant({"event_name":event,"student_id":active_search_id,"grade":grade,"class":class_num,"number":number,"name":registered_name,"timestamp":f"{cdate} {ctime.strftime('%H:%M:%S')}","operator":st.session_state.staff_info})
                        db_add_log("참여자 등록", f"[{event}] {active_search_id} {registered_name} 등록 완료", st.session_state.staff_info)
                        st.session_state.pop("pending_admin_duplicate", None)
                        st.success(f"학번 {active_search_id} 참여 등록 완료!")
                        st.rerun()

            st.markdown('<h3>행사 참여 이력</h3>', unsafe_allow_html=True)
            history_search = st.session_state.get("staff_history_search")
            if history_search:
                history_id, history_name = history_search
                history = df_p[(df_p["student_id"].astype(str).str.strip() == history_id) & (df_p["name"].astype(str).str.strip() == history_name)] if not df_p.empty else pd.DataFrame()
                if not history.empty:
                    st.success(f"학번과 이름이 모두 일치합니다 · 참여 이력 {len(history)}건")
                    st.dataframe(history[["event_name", "timestamp", "operator"]].rename(columns={"event_name":"행사명", "timestamp":"참여 일시", "operator":"등록자"}), use_container_width=True, hide_index=True)
                else:
                    st.info("일치하는 참여 이력이 없습니다.")
        st.divider()
        st.markdown('<h3>참여 기록 관리</h3><p class="muted">오늘 기록뿐 아니라 과거 기록도 검색해 여러 건을 한 번에 삭제할 수 있습니다.</p>', unsafe_allow_html=True)
        all_records = db_get_participants()
        if all_records.empty:
            st.info("삭제할 참여 기록이 없습니다.")
        else:
            r1, r2, r3 = st.columns([1.2, 1.2, 1.6])
            with r1:
                record_scope = st.selectbox("기록 범위", ["오늘", "전체 기록"], key="record_scope")
            with r2:
                record_event_options = ["전체 행사"] + sorted(all_records["event_name"].dropna().astype(str).unique().tolist())
                record_event = st.selectbox("행사", record_event_options, key="record_event_filter")
            with r3:
                record_query = st.text_input("학번 또는 이름 검색", key="record_search", placeholder="학번/이름 일부 입력")
            record_view = all_records.copy()
            if record_scope == "오늘":
                record_view = record_view[record_view["timestamp"].astype(str).str.startswith(now_seoul().strftime("%Y-%m-%d"))]
            if record_event != "전체 행사":
                record_view = record_view[record_view["event_name"].astype(str) == record_event]
            if record_query.strip():
                q = record_query.strip()
                record_view = record_view[record_view["student_id"].astype(str).str.contains(q, case=False, na=False) | record_view["name"].astype(str).str.contains(q, case=False, na=False)]
            st.caption(f"검색 결과 {len(record_view):,}건")
            if record_view.empty:
                st.info("조건에 맞는 기록이 없습니다.")
            else:
                st.dataframe(record_view[["id", "event_name", "student_id", "name", "timestamp", "operator"]], use_container_width=True, hide_index=True)
                record_labels = {f"#{int(row['id'])} · {row['event_name']} · {row['student_id']} {row['name']} · {row['timestamp']}": int(row['id']) for _, row in record_view.iterrows()}
                selected_record_labels = st.multiselect("삭제할 기록 선택", list(record_labels.keys()), key="bulk_delete_records", placeholder="삭제할 기록을 선택하세요")
                confirm_bulk_delete = st.checkbox("선택한 기록을 삭제하고 복구 보관함으로 이동하는 것에 동의합니다.", key="confirm_bulk_delete_records", disabled=not selected_record_labels)
                if st.button("선택한 참여 기록 삭제", type="primary", disabled=(not selected_record_labels or not confirm_bulk_delete), key="bulk_delete_records_btn"):
                    ids_to_delete = [record_labels[label] for label in selected_record_labels]
                    deleted_count = db_archive_and_delete_participants(ids_to_delete, st.session_state.staff_info)
                    db_add_log("데이터 삭제", f"관리자가 참여 기록 {deleted_count}건을 삭제하고 복구 보관함에 보관함", st.session_state.staff_info)
                    st.session_state.pop("bulk_delete_records", None)
                    st.session_state.pop("confirm_bulk_delete_records", None)
                    st.success(f"참여 기록 {deleted_count}건을 복구 보관함으로 이동했습니다.")
                    st.rerun()
                with st.expander("선택한 기록 수정"):
                    edit_label = st.selectbox("수정할 기록", list(record_labels.keys()), key="edit_record_choice")
                    edit_id = record_labels[edit_label]
                    edit_row = record_view[record_view["id"] == edit_id].iloc[0]
                    mod_name = st.text_input("이름", value=str(edit_row["name"]), key="bulk_edit_name")
                    mod_sid = st.text_input("학번", value=str(edit_row["student_id"]), max_chars=5, key="bulk_edit_sid")
                    if st.button("수정 내용 저장", key="bulk_edit_save"):
                        ok, g, cl, num = parse_student_id(mod_sid)
                        if ok and mod_name.strip():
                            db_update_participant(int(edit_id), str(edit_row["event_name"]), mod_sid.strip(), g, cl, num, mod_name.strip())
                            db_add_log("데이터 수정", f"참여 기록 ID {edit_id} 수정 완료", st.session_state.staff_info)
                            st.success("기록을 수정했습니다.")
                            st.rerun()
                        else:
                            st.error("올바른 5자리 학번과 이름을 입력해 주세요.")

        with st.expander("삭제한 참여 기록 복구"):
            with get_db_connection() as conn:
                archived_rows = conn.execute("SELECT archive_id,original_id,event_name,student_id,name,timestamp,deleted_at,deleted_by FROM deleted_participants ORDER BY archive_id DESC").fetchall()
            if not archived_rows:
                st.caption("복구할 기록이 없습니다.")
            else:
                archive_df = pd.DataFrame(archived_rows, columns=["보관 ID","원래 기록 ID","행사","학번","이름","참여 시각","삭제 시각","삭제 담당자"])
                st.dataframe(archive_df, use_container_width=True, hide_index=True)
                archive_choices = {f"#{r[0]} · {r[2]} · {r[3]} {r[4]} · 삭제 {r[6]}": int(r[0]) for r in archived_rows}
                chosen_archive = st.selectbox("복구할 기록", list(archive_choices.keys()), key="restore_archive_choice")
                if st.button("선택한 기록 복구", key="restore_archive_btn"):
                    ok, message = db_restore_archived_participant(archive_choices[chosen_archive], st.session_state.staff_info)
                    (st.success if ok else st.warning)(message)
                    st.rerun()

    with tab2:
        st.markdown('<div class="eyebrow">EVENT MANAGEMENT</div><h3>신규 행사 개설</h3>', unsafe_allow_html=True)
        create_col, list_col = st.columns([1, 1.7], gap="large")
        with create_col:
            with st.container(border=True):
                new_name = st.text_input("행사명", placeholder="예: 독서의 달 행사", key="new_event_name")
                new_type = st.selectbox("행사 종류", ["독서·독서토론", "도서관 이용 교육", "독서 문화 행사", "전시·캠페인", "대회·공모전", "기타"], key="new_event_type")
                new_description = st.text_area("행사 설명", placeholder="행사 내용, 참여 대상, 안내 사항 등을 적어 주세요.", key="new_event_description", height=110)
                if st.button("행사 만들기"):
                    if new_name.strip():
                        db_add_event(new_name.strip(), "진행 중", now_seoul().strftime("%Y-%m-%d %H:%M:%S"), new_type, new_description.strip())
                        db_add_log("행사 생성", f"신규 행사 [{new_name.strip()}] 생성", st.session_state.staff_info); st.success("행사가 개설되었습니다."); st.rerun()
                    else: st.warning("행사명을 입력해 주세요.")
        with list_col:
            st.markdown("### 개설된 행사 목록")
            events = db_get_events()
            if events.empty: st.info("개설된 행사가 없습니다.")
            for _, ev in events.iterrows():
                with st.container(border=True):
                    name_col, status_col, action_col, delete_col = st.columns([3, 1.2, 1.2, 1.1])
                    name_col.markdown(f"**{ev['name']}**")
                    name_col.caption(f"{ev.get('event_type', '기타')} · {ev.get('description', '')}")
                    with st.expander("행사 정보 수정", expanded=st.session_state.get(f"edit_event_open_{ev['id']}", False)):
                        event_types = ["독서·독서토론", "도서관 이용 교육", "독서 문화 행사", "전시·캠페인", "대회·공모전", "기타"]
                        current_type = str(ev.get("event_type", "기타"))
                        if current_type not in event_types:
                            event_types.append(current_type)
                        with st.form(key=f"edit_event_form_{ev['id']}"):
                            edited_name = st.text_input("행사명", value=str(ev["name"]), key=f"edit_event_name_{ev['id']}")
                            edited_type = st.selectbox("행사 종류", event_types, index=event_types.index(current_type), key=f"edit_event_type_{ev['id']}")
                            edited_description = st.text_area("행사 설명", value=str(ev.get("description", "") or ""), height=100, key=f"edit_event_description_{ev['id']}")
                            save_event = st.form_submit_button("수정 내용 저장")
                        if save_event:
                            if not edited_name.strip():
                                st.warning("행사명을 입력해 주세요.")
                            else:
                                old_name = str(ev["name"])
                                db_update_event_details(int(ev["id"]), old_name, edited_name.strip(), edited_type, edited_description.strip())
                                db_add_log("행사 정보 수정", f"행사 [{old_name}] 정보 수정" + (f" → [{edited_name.strip()}]" if old_name != edited_name.strip() else ""), st.session_state.staff_info)
                                st.success("행사 정보가 수정되었습니다.")
                                st.rerun()
                    status_col.markdown("🟢 진행 중" if ev['status'] == "진행 중" else "🔒 마감")
                    label = "마감하기" if ev['status'] == "진행 중" else "재개하기"
                    if action_col.button(label, key=f"toggle_{ev['id']}"):
                        new_status = "마감" if ev['status'] == "진행 중" else "진행 중"
                        db_update_event_status(int(ev['id']), new_status); db_add_log("행사 상태 변경", f"[{ev['name']}] -> {new_status}", st.session_state.staff_info); st.rerun()
                    if ev['status'] == "마감":
                        if delete_col.button("영구 삭제", key=f"delete_event_{ev['id']}"):
                            st.session_state[f"confirm_event_delete_{ev['id']}"] = True
                        if st.session_state.get(f"confirm_event_delete_{ev['id']}", False):
                            st.warning("행사와 연결된 모든 참여 기록이 삭제됩니다.")
                            yes, no = st.columns(2)
                            if yes.button("삭제 확인", key=f"confirm_yes_{ev['id']}"):
                                db_delete_event_and_records(int(ev['id']), ev['name'], st.session_state.staff_info); db_add_log("행사 삭제", f"행사 [{ev['name']}] 영구 삭제 완료", st.session_state.staff_info); st.session_state.pop(f"confirm_event_delete_{ev['id']}", None); st.rerun()
                            if no.button("취소", key=f"confirm_no_{ev['id']}"):
                                st.session_state.pop(f"confirm_event_delete_{ev['id']}", None); st.rerun()

    with tab3:
        st.markdown('<div class="eyebrow">STATISTICS</div><h3>참여 통계</h3><p class="muted">행사와 기간을 선택해 필요한 참여 기록을 확인하고 엑셀로 내려받을 수 있습니다.</p>', unsafe_allow_html=True)
        df_all = db_get_participants()
        if df_all.empty:
            st.info("통계를 표시할 참여 기록이 없습니다.")
        else:
            # 필터 옵션
            sf1, sf2, sf3, sf4 = st.columns([1.4, 1, 1, 1])
            event_names = sorted(df_all["event_name"].dropna().astype(str).unique().tolist())
            grade_values = sorted(pd.to_numeric(df_all["grade"], errors="coerce").dropna().astype(int).unique().tolist())
            with sf1:
                stat_events = st.multiselect("행사 필터", event_names, default=[], key="stat_events")
            with sf2:
                stat_grades = st.multiselect("학년 필터", grade_values, default=[], format_func=lambda x: f"{x}학년", key="stat_grades")
            class_values = sorted(pd.to_numeric(df_all["class"], errors="coerce").dropna().astype(int).unique().tolist())
            with sf3:
                stat_classes = st.multiselect("반 필터", class_values, default=[], format_func=lambda x: f"{x}반", key="stat_classes")
            with sf4:
                stat_operator = st.selectbox("등록 경로", ["전체", "참여자 본인"] + sorted([x for x in df_all["operator"].dropna().astype(str).unique().tolist() if x != "참여자 본인"]), key="stat_operator")
            stat_date_filter = st.checkbox("기간 필터 사용", key="stat_date_filter")
            stat_from = stat_to = None
            if stat_date_filter:
                valid_dates = pd.to_datetime(df_all["timestamp"], errors="coerce").dropna()
                min_date = valid_dates.min().date() if not valid_dates.empty else now_seoul().date()
                max_date = valid_dates.max().date() if not valid_dates.empty else now_seoul().date()
                sd1, sd2 = st.columns(2)
                with sd1: stat_from = st.date_input("시작 날짜", value=min_date, key="stat_from")
                with sd2: stat_to = st.date_input("종료 날짜", value=max_date, key="stat_to")
            filtered = df_all.copy()
            if stat_events: filtered = filtered[filtered["event_name"].astype(str).isin(stat_events)]
            if stat_grades: filtered = filtered[pd.to_numeric(filtered["grade"], errors="coerce").isin(stat_grades)]
            if stat_classes: filtered = filtered[pd.to_numeric(filtered["class"], errors="coerce").isin(stat_classes)]
            if stat_operator != "전체": filtered = filtered[filtered["operator"].astype(str) == stat_operator]
            timestamps = pd.to_datetime(filtered["timestamp"], errors="coerce")
            if stat_from: filtered = filtered[timestamps.dt.date >= stat_from]
            if stat_to:
                timestamps = pd.to_datetime(filtered["timestamp"], errors="coerce")
                filtered = filtered[timestamps.dt.date <= stat_to]
            if stat_from and stat_to and stat_from > stat_to:
                st.error("시작 날짜는 종료 날짜보다 늦을 수 없습니다.")
            else:
                total_records = len(filtered)
                unique_students = filtered["student_id"].nunique() if not filtered.empty else 0
                unique_events = filtered["event_name"].nunique() if not filtered.empty else 0
                m1, m2, m3 = st.columns(3)
                m1.metric("필터 적용 참여 기록", f"{total_records:,}건")
                m2.metric("참여 학생 수", f"{unique_students:,}명")
                m3.metric("참여한 행사 수", f"{unique_events:,}개")
                if filtered.empty:
                    st.info("선택한 조건에 해당하는 참여 기록이 없습니다.")
                else:
                    event_counts = filtered["event_name"].value_counts().rename_axis("행사명").reset_index(name="참여 건수")
                    grade_counts = filtered["grade"].value_counts().sort_index().rename_axis("학년").reset_index(name="참여 건수")
                    monthly = filtered.copy()
                    monthly["월"] = monthly["timestamp"].astype(str).str[:7]
                    monthly_counts = monthly.groupby("월").size().reset_index(name="참여 건수").sort_values("월")
                    class_counts = (filtered.assign(학급=pd.to_numeric(filtered["grade"], errors="coerce").fillna(0).astype(int).astype(str) + "학년 " + pd.to_numeric(filtered["class"], errors="coerce").fillna(0).astype(int).astype(str) + "반")
                                    .groupby("학급").agg(참여_건수=("student_id", "size"), 참여_학생_수=("student_id", "nunique"))
                                    .sort_values(["참여_건수", "참여_학생_수"], ascending=False).reset_index()
                                    .rename(columns={"참여_건수": "참여 건수", "참여_학생_수": "참여 학생 수"}))
                    st.markdown("#### 행사별 참여 현황")
                    st.bar_chart(event_counts.set_index("행사명"), use_container_width=True)
                    c1, c2 = st.columns(2)
                    with c1:
                        st.markdown("#### 학년별 참여 현황")
                        st.bar_chart(grade_counts.set_index("학년"), use_container_width=True)
                    with c2:
                        st.markdown("#### 월별 참여 현황")
                        st.line_chart(monthly_counts.set_index("월"), use_container_width=True)
                    st.markdown("#### 학급별 참여 현황")
                    if not class_counts.empty:
                        top_count = int(class_counts["참여 건수"].max())
                        top_classes = class_counts[class_counts["참여 건수"] == top_count]["학급"].astype(str).tolist()
                        st.success(f"참여 건수 최다 학급: {', '.join(top_classes)} · {top_count:,}건")
                        st.bar_chart(class_counts.set_index("학급")[["참여 건수", "참여 학생 수"]], use_container_width=True)
                        st.dataframe(class_counts, use_container_width=True, hide_index=True)
                    st.markdown("#### 필터 적용 기록")
                    st.dataframe(filtered, use_container_width=True, hide_index=True)
                    output = BytesIO()
                    with pd.ExcelWriter(output, engine="openpyxl") as writer:
                        filtered.to_excel(writer, sheet_name="필터 적용 기록", index=False)
                        event_counts.to_excel(writer, sheet_name="행사별 통계", index=False)
                        grade_counts.to_excel(writer, sheet_name="학년별 통계", index=False)
                        class_counts.to_excel(writer, sheet_name="학급별 통계", index=False)
                        monthly_counts.to_excel(writer, sheet_name="월별 통계", index=False)
                    st.download_button("필터 결과 엑셀 다운로드", data=output.getvalue(), file_name=f"행사_참여_통계_{now_seoul().strftime('%Y%m%d')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    with tab4:
        st.markdown('<div class="eyebrow">ACTIVITY LOG</div><h3>처리 이력</h3><p class="muted">필터로 필요한 기록만 확인하거나 선택한 기록만 삭제할 수 있습니다.</p>', unsafe_allow_html=True)
        logs = db_get_logs()
        if logs.empty:
            st.info("기록된 처리 이력이 없습니다.")
        else:
            f1, f2, f3 = st.columns(3)
            categories = sorted(logs["category"].dropna().astype(str).unique().tolist())
            operators = sorted(logs["operator"].dropna().astype(str).unique().tolist())
            with f1: chosen_categories = st.multiselect("분류", categories, default=[], key="log_categories")
            with f2: chosen_operators = st.multiselect("담당자", operators, default=[], key="log_operators")
            with f3: log_query = st.text_input("내용 검색", placeholder="내용이나 담당자 검색", key="log_query")
            use_date_filter = st.checkbox("날짜 범위로 좁혀 보기", key="use_log_date_filter")
            date_from = date_to = None
            if use_date_filter:
                d1, d2 = st.columns(2)
                with d1: date_from = st.date_input("시작 날짜", value=now_seoul().date(), key="log_date_from")
                with d2: date_to = st.date_input("종료 날짜", value=now_seoul().date(), key="log_date_to")
            shown = logs.copy()
            if chosen_categories: shown = shown[shown["category"].astype(str).isin(chosen_categories)]
            if chosen_operators: shown = shown[shown["operator"].astype(str).isin(chosen_operators)]
            if log_query.strip():
                mask = shown.astype(str).apply(lambda col: col.str.contains(log_query.strip(), case=False, na=False)).any(axis=1)
                shown = shown[mask]
            if date_from: shown = shown[shown["timestamp"].astype(str).str[:10] >= date_from.strftime("%Y-%m-%d")]
            if date_to: shown = shown[shown["timestamp"].astype(str).str[:10] <= date_to.strftime("%Y-%m-%d")]
            st.caption(f"필터 결과 {len(shown):,}건 / 전체 {len(logs):,}건")
            st.dataframe(shown, use_container_width=True, hide_index=True)
            options = {f"#{int(row['id'])} · {row['timestamp']} · {row['category']} · {row['detail'][:45]}": int(row['id']) for _, row in shown.iterrows()}
            with st.expander("선택한 처리 이력 삭제"):
                if options:
                    selected_log_labels = st.multiselect("삭제할 기록 선택", list(options.keys()), key="selected_log_ids")
                    st.warning("선택한 처리 이력만 삭제됩니다. 삭제 후에는 복구할 수 없습니다.")
                    if st.button("선택한 기록 삭제", type="primary", disabled=not selected_log_labels):
                        ids = [options[label] for label in selected_log_labels]
                        deleted_count = db_archive_and_delete_logs(ids, st.session_state.staff_info)
                        db_add_log("처리 이력 삭제", f"관리자가 처리 이력 {deleted_count}건을 삭제하고 복구 보관함에 보관함", st.session_state.staff_info)
                        st.success(f"처리 이력 {deleted_count}건을 복구 보관함으로 이동했습니다.")
                        st.rerun()
                else:
                    st.caption("삭제할 기록이 없습니다.")
            with st.expander("모든 처리 이력 삭제"):
                st.warning("모든 처리 이력이 삭제됩니다. 이 작업은 되돌릴 수 없습니다.")
                confirm = st.checkbox("전체 삭제를 확인했습니다.", key="confirm_clear_logs")
                if st.button("모든 처리 이력 삭제", disabled=not confirm):
                    all_log_ids = db_get_logs()["id"].astype(int).tolist()
                    deleted_count = db_archive_and_delete_logs(all_log_ids, st.session_state.staff_info)
                    db_add_log("처리 이력 삭제", f"관리자가 전체 처리 이력 {deleted_count}건을 복구 보관함으로 이동함", st.session_state.staff_info)
                    st.success(f"처리 이력 {deleted_count}건을 복구 보관함으로 이동했습니다.")
                    st.rerun()
            with st.expander("삭제한 처리 이력 복구"):
                with get_db_connection() as conn:
                    archived_logs = conn.execute("SELECT archive_id,original_id,timestamp,category,detail,operator,deleted_at,deleted_by FROM deleted_logs ORDER BY archive_id DESC").fetchall()
                if not archived_logs:
                    st.caption("복구할 처리 이력이 없습니다.")
                else:
                    archived_log_df = pd.DataFrame(archived_logs, columns=["보관 ID", "원래 기록 ID", "처리 시각", "분류", "내용", "담당자", "삭제 시각", "삭제 담당자"])
                    st.dataframe(archived_log_df, use_container_width=True, hide_index=True)
                    archived_log_choices = {f"#{r[0]} · {r[3]} · {str(r[4])[:50]} · 삭제 {r[6]}": int(r[0]) for r in archived_logs}
                    chosen_log_archive = st.selectbox("복구할 처리 이력", list(archived_log_choices.keys()), key="restore_log_archive_choice")
                    if st.button("선택한 처리 이력 복구", key="restore_log_archive_btn"):
                        ok, message = db_restore_archived_log(archived_log_choices[chosen_log_archive], st.session_state.staff_info)
                        (st.success if ok else st.warning)(message)
                        st.rerun()

    with tab5:
        st.markdown('<div class="eyebrow">DATA MANAGEMENT</div><h3>데이터 백업 및 복원</h3><p class="muted">복원 전에 현재 데이터베이스를 백업하고, 업로드한 파일의 구조를 확인합니다.</p>', unsafe_allow_html=True)
        if DB_PATH.exists():
            # SQLite backup API로 WAL 상태를 포함한 일관된 백업 스냅샷 생성
            backup_buffer = BytesIO()
            with sqlite3.connect(str(DB_PATH)) as source_conn:
                with sqlite3.connect(":memory:") as backup_conn:
                    source_conn.backup(backup_conn)
                    backup_conn.commit()
                    temp_path = None
                    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                        temp_path = tmp.name
                    try:
                        with sqlite3.connect(temp_path) as tmp_conn:
                            backup_conn.backup(tmp_conn)
                        backup_buffer.write(Path(temp_path).read_bytes())
                    finally:
                        try: os.unlink(temp_path)
                        except OSError: pass
            st.download_button("현재 데이터베이스 백업 (.db)", data=backup_buffer.getvalue(), file_name=f"library_backup_{now_seoul().strftime('%Y%m%d_%H%M%S')}.db", mime="application/octet-stream")
        else:
            st.error("데이터베이스 파일을 찾을 수 없습니다.")
        st.warning("복원은 현재 데이터베이스를 업로드한 백업으로 교체합니다. 복원 전에 현재 백업을 반드시 다운로드하세요.")
        uploaded_backup = st.file_uploader("복원할 SQLite 백업 파일 선택", type=["db", "sqlite", "sqlite3"], key="restore_db_upload")
        restore_confirm = st.checkbox("현재 데이터를 업로드한 백업 내용으로 교체하는 것에 동의합니다.", key="restore_db_confirm", disabled=uploaded_backup is None)
        if st.button("백업 파일 검증 후 복원", key="restore_db_btn", disabled=(uploaded_backup is None or not restore_confirm)):
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                    tmp.write(uploaded_backup.getvalue())
                    temp_path = tmp.name
                with sqlite3.connect(temp_path) as check_conn:
                    table_names = {r[0] for r in check_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                    required = {"events", "participants", "logs"}
                    if not required.issubset(table_names):
                        raise ValueError("백업 파일에 필수 테이블(events, participants, logs)이 없습니다.")
                    integrity = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
                    if integrity != "ok":
                        raise ValueError("SQLite 무결성 검사에 실패했습니다.")
                # 안전망: 현재 DB의 별도 스냅샷을 생성한 뒤에만 교체
                pre_restore_path = DB_PATH.with_name(f"{DB_PATH.stem}_before_restore_{now_seoul().strftime('%Y%m%d_%H%M%S')}{DB_PATH.suffix}")
                with sqlite3.connect(str(DB_PATH)) as current_conn, sqlite3.connect(str(pre_restore_path)) as save_conn:
                    current_conn.backup(save_conn)
                os.replace(temp_path, DB_PATH)
                temp_path = None
                init_db()
                db_add_log("데이터 복원", f"백업 파일 복원 완료. 이전 DB 안전 사본: {pre_restore_path.name}", st.session_state.staff_info)
                st.success("복원이 완료되었습니다. 기존 데이터의 안전 사본도 서버에 남겼습니다.")
                st.rerun()
            except Exception as exc:
                st.error(f"복원하지 못했습니다: {exc}")
            finally:
                if temp_path:
                    try: os.unlink(temp_path)
                    except OSError: pass
        st.info("중요: Streamlit Cloud의 로컬 파일은 영구 저장소가 아닐 수 있습니다. 장기 보존이 필요하면 외부 데이터베이스나 영구 스토리지를 사용하고 정기적으로 백업 파일을 내려받으세요.")

st.markdown('<div style="text-align:center;color:#a0a8b8;font-size:12px;padding:24px 0 4px;letter-spacing:1px">SONGCHEON EVENT SYSTEM · GLASS UI</div>', unsafe_allow_html=True)
