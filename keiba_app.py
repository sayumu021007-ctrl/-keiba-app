# ============================================================
#  競馬予想分析アプリ  v2（Streamlit + SQLite）
# ============================================================
#
#  【v2での変更点】
#   ・馬ID / 騎手ID を自動採番（H0001, J0001 …）名前はIDと別管理
#   ・馬・騎手・レースを自分で登録 → 出走表を作って予想する流れを整備
#   ・検索機能を追加（馬・騎手を名前で絞り込み）
#   ・起動を簡単にする start.bat を別途同梱
#
#  【起動方法】
#   かんたん：start.bat をダブルクリック
#   手動    ：python -m streamlit run keiba_app.py
# ============================================================

import streamlit as st
import pandas as pd
import re  # JRA結果テキストを解析するために使用

# ============================================================
# データベース接続(SQLite / PostgreSQL(Supabase) 両対応)
# ============================================================
# st.secrets に "db_url" があれば PostgreSQL(Supabase)に接続。
# なければローカルの keiba.db (SQLite) を使う。
# これでPCでもクラウドでも同じコードで動く。

DB_FILE = "keiba.db"


def _get_db_url():
    try:
        return st.secrets["db_url"]
    except Exception:
        return None


USE_POSTGRES = _get_db_url() is not None

# pandas read_sql 用のプレースホルダ(?か%s)
PH = "%s" if USE_POSTGRES else "?"

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3


class _CursorWrapper:
    """SQLiteとPostgreSQLの違いを吸収するカーソルラッパー。
    既存コードの ? プレースホルダを、PostgreSQLでは %s に自動変換する。
    """
    def __init__(self, real_cursor, is_pg):
        self._cur = real_cursor
        self._is_pg = is_pg

    def execute(self, sql, params=()):
        if self._is_pg:
            sql = sql.replace("?", "%s")
            sql = self._convert_upsert(sql)
        self._cur.execute(sql, params)
        return self

    def _convert_upsert(self, sql):
        """SQLiteの INSERT OR REPLACE を PostgreSQL の
        INSERT ... ON CONFLICT (主キー) DO UPDATE に変換する。
        """
        if "INSERT OR REPLACE" not in sql:
            return sql
        # テーブルごとの主キー
        pk_map = {"races": "id", "settings": "key", "horses": "id",
                  "jockeys": "id"}
        # INSERT OR REPLACE INTO <table> (col1, col2, ...) VALUES ...
        m = re.search(r"INSERT OR REPLACE INTO\s+(\w+)\s*\(([^)]+)\)", sql, re.IGNORECASE)
        if not m:
            return sql.replace("INSERT OR REPLACE", "INSERT")
        table = m.group(1)
        cols = [c.strip() for c in m.group(2).split(",")]
        pk = pk_map.get(table, "id")
        # 主キー以外の列を「DO UPDATE SET col=EXCLUDED.col」にする
        update_cols = [c for c in cols if c != pk]
        set_clause = ", ".join(f"{c}=EXCLUDED.{c}" for c in update_cols)
        sql = sql.replace("INSERT OR REPLACE", "INSERT")
        # VALUES(...) の後ろに ON CONFLICT を足す
        sql = sql.rstrip().rstrip(";")
        sql += f" ON CONFLICT ({pk}) DO UPDATE SET {set_clause}"
        return sql

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        return getattr(self._cur, "lastrowid", None)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _ConnWrapper:
    """接続のラッパー。SQLite/PostgreSQLの違いを吸収する。"""
    def __init__(self, real_conn, is_pg):
        self._conn = real_conn
        self._is_pg = is_pg

    def cursor(self):
        if self._is_pg:
            real = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            real = self._conn.cursor()
        return _CursorWrapper(real, self._is_pg)

    def execute(self, sql, params=()):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ============================================================
# 【2】データベース処理
# ============================================================
def get_connection():
    if USE_POSTGRES:
        conn = psycopg2.connect(_get_db_url())
        return _ConnWrapper(conn, True)
    else:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return _ConnWrapper(conn, False)


def init_db():
    # PostgreSQL(Supabase)の場合、テーブルはSupabase側で作成済みなのでスキップ
    if USE_POSTGRES:
        # 念のため、auto_rating / class 列が無ければ追加(エラーは無視)
        conn = get_connection()
        cur = conn.cursor()
        for sql in [
            "ALTER TABLE horses ADD COLUMN IF NOT EXISTS auto_rating INTEGER",
            "ALTER TABLE races ADD COLUMN IF NOT EXISTS class TEXT",
        ]:
            try:
                cur.execute(sql)
                conn.commit()
            except Exception:
                conn.rollback()
        conn.close()
        return

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS horses (
            id TEXT PRIMARY KEY, name TEXT, sex TEXT, age INTEGER,
            sire TEXT, dam TEXT, dam_sire TEXT, running_style TEXT,
            best_distance TEXT, best_track TEXT, surface_pref TEXT,
            turn_pref TEXT, rating INTEGER, comment TEXT
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS jockeys (
            id TEXT PRIMARY KEY, name TEXT, best_distance TEXT,
            best_track TEXT, surface_pref TEXT, turn_pref TEXT,
            rating INTEGER, comment TEXT
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS races (
            id TEXT PRIMARY KEY, date TEXT, name TEXT, course TEXT,
            surface TEXT, distance INTEGER, turn TEXT,
            track_condition TEXT, weather TEXT
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, race_id TEXT,
            horse_id TEXT, jockey_id TEXT, frame INTEGER, number INTEGER,
            weight REAL, odds REAL, popularity INTEGER
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, race_id TEXT,
            entry_id INTEGER, finish INTEGER, final_odds REAL,
            hit INTEGER, memo TEXT,
            time TEXT, margin TEXT, corner TEXT, agari TEXT,
            body_weight TEXT, trainer TEXT
        )""")
    # 既存DBに新しい列が無ければ追加(マイグレーション)
    for col, typ in [("time", "TEXT"), ("margin", "TEXT"),
                     ("corner", "TEXT"), ("agari", "TEXT"),
                     ("body_weight", "TEXT"), ("trainer", "TEXT")]:
        try:
            cur.execute(f"ALTER TABLE results ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    try:
        cur.execute("ALTER TABLE horses ADD COLUMN auto_rating INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE races ADD COLUMN class TEXT")
    except sqlite3.OperationalError:
        pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, race_id TEXT,
            bet_type TEXT, selection TEXT, amount INTEGER, payout INTEGER,
            hit INTEGER, memo TEXT
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    """設定値を読み込む。無ければデフォルト値を返す。"""
    conn = get_connection()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    """設定値を保存する(上書き)。"""
    run_sql("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value)))


def load_table(table_name):
    conn = get_connection()
    df = pd.read_sql(f"SELECT * FROM {table_name}", conn._conn)
    conn.close()
    return df


# 列名の英語→日本語変換マップ
COLUMN_LABELS = {
    "id": "ID", "name": "名前",
    "sex": "性別", "age": "年齢",
    "sire": "父", "dam": "母", "dam_sire": "母父",
    "running_style": "脚質",
    "best_distance": "得意距離", "best_track": "得意馬場",
    "surface_pref": "芝ダ適性", "turn_pref": "回り適性",
    "rating": "手動評価", "auto_rating": "自動評価",
    "comment": "コメント",
    "date": "日付", "course": "競馬場",
    "surface": "芝/ダ", "distance": "距離",
    "turn": "回り", "track_condition": "馬場", "weather": "天気",
    "class": "クラス",
}


def jp(df):
    """データフレームの列名を日本語に変換して返す。"""
    return df.rename(columns=COLUMN_LABELS)


def run_sql(sql, params=()):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit()
    conn.close()


def run_sql_returning_id(sql, params=()):
    """INSERTを実行して、その行のidを返す。
    SQLiteは lastrowid、PostgreSQLは RETURNING id を使う。
    """
    conn = get_connection()
    cur = conn.cursor()
    if USE_POSTGRES:
        # PostgreSQLは末尾に RETURNING id を付けてidを取得
        sql_ret = sql.rstrip().rstrip(";") + " RETURNING id"
        cur.execute(sql_ret, params)
        row = cur.fetchone()
        new_id = row["id"] if row else None
    else:
        cur.execute(sql, params)
        new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id


# ----- ID自動採番 -----
# 例：horsesテーブルなら H0001, H0002... と自動で次の番号を作る。
def next_id(table, prefix):
    """そのテーブルで次に使う連番ID（例 H0005）を作って返す。"""
    conn = get_connection()
    # 既存IDのうち prefix で始まるものを全部取得
    rows = conn.execute(f"SELECT id FROM {table} WHERE id LIKE ?", (prefix + "%",)).fetchall()
    conn.close()
    max_num = 0
    for r in rows:
        tail = r["id"][len(prefix):]   # 例 "H0003" → "0003"
        if tail.isdigit():
            max_num = max(max_num, int(tail))
    return f"{prefix}{max_num + 1:04d}"   # 4桁ゼロ埋め（H0001）


# ============================================================
# 【3】点数計算ロジック
# ============================================================
def categorize_distance(distance):
    # 距離が数字に変換できない場合(空・文字混じり等)も落ちないようにする
    try:
        d = int(distance) if distance not in (None, "") else 0
    except (ValueError, TypeError):
        # "2400m" のように単位付きでも数字だけ拾う
        import re as _re
        m = _re.search(r"\d+", str(distance))
        d = int(m.group()) if m else 0
    if d == 0:
        return ""
    if d <= 1400:
        return "短距離"
    if d <= 1700:
        return "マイル"
    if d <= 2200:
        return "中距離"
    return "長距離"


def calculate_score(horse, jockey, race):
    # 馬評価: 手動評価(rating)が入っていればそれを優先、なければ自動評価(auto_rating)
    # ※「自動評価ベース・手動上書き可能」の仕様
    h_manual = horse["rating"] if "rating" in horse.index and horse["rating"] is not None else None
    h_auto = horse["auto_rating"] if "auto_rating" in horse.index and horse["auto_rating"] is not None else None
    # 手動が50(初期値)で、かつ自動評価がある場合は自動評価を使う
    if h_manual is not None and h_manual != 50:
        horse_rating = h_manual  # 手動で意図的に変えた値を尊重
    elif h_auto is not None:
        horse_rating = h_auto    # 自動評価を使う
    else:
        horse_rating = h_manual if h_manual is not None else 0

    jockey_rating = jockey["rating"] if jockey["rating"] else 0
    score = horse_rating * 0.5 + jockey_rating * 0.3
    bonuses = []
    race_dist = categorize_distance(race["distance"])
    if horse["best_distance"] and horse["best_distance"] == race_dist:
        score += 6; bonuses.append("馬◎距離+6")
    if horse["best_track"] and horse["best_track"] == race["track_condition"]:
        score += 4; bonuses.append("馬◎馬場+4")
    if horse["surface_pref"] and horse["surface_pref"] == race["surface"]:
        score += 5; bonuses.append("馬◎芝ダ+5")
    if horse["turn_pref"] and horse["turn_pref"] == race["turn"]:
        score += 3; bonuses.append("馬◎回り+3")
    if jockey["best_distance"] and jockey["best_distance"] == race_dist:
        score += 2; bonuses.append("騎◎距離+2")
    if jockey["surface_pref"] and jockey["surface_pref"] == race["surface"]:
        score += 2; bonuses.append("騎◎芝ダ+2")
    return round(score, 1), bonuses


# ============================================================
# 【3-2】JRA結果テキストのパーサー(読み取り)
# ============================================================
# JRA公式の「レース結果ページ」をコピペしたテキストを読み取って
# レース情報 + 各馬の結果 を辞書形式で返す。

def detect_race_class(text):
    """テキストからレースのクラス(G1/G2/G3/L/オープン/3勝/2勝/1勝/未勝利/新馬)を判定する。"""
    if re.search(r'(GⅠ|G1|Ｇ１)', text):
        return "G1"
    if re.search(r'(GⅡ|G2|Ｇ２)', text):
        return "G2"
    if re.search(r'(GⅢ|G3|Ｇ３)', text):
        return "G3"
    if re.search(r'リステッド', text):
        return "L"
    if re.search(r'オープン', text):
        return "OP"
    if re.search(r'3勝クラス|1600万下', text):
        return "3勝"
    if re.search(r'2勝クラス|1000万下', text):
        return "2勝"
    if re.search(r'1勝クラス|500万下', text):
        return "1勝"
    if re.search(r'未勝利', text):
        return "未勝利"
    if re.search(r'新馬', text):
        return "新馬"
    return ""


def parse_jra_full(text):
    """JRA結果テキストを丸ごと解析。
    戻り値: {"race": レース情報の辞書, "results": 各馬の結果のリスト}
    """
    race = {}
    lines = text.split('\n')

    # 日付と競馬場 (例: "2026年5月24日（日曜） 2回東京10日")
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日.*?\d+回(\S+?)\d+日', text)
    if m:
        race["date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        race["course"] = m.group(4)

    # 天気
    m = re.search(r'天候\s+(\S+)', text)
    if m and m.group(1) in ["晴", "曇", "雨", "雪", "小雨", "小雪"]:
        race["weather"] = m.group(1)

    # 距離・芝ダ・回り (例: "2,400メートル（芝・左）")
    m = re.search(r'(\d[\d,]*)\s*メートル\s*[\(（](芝|ダート|ダ)\s*[・\u30FB](右|左|直線?)', text)
    if m:
        race["distance"] = int(m.group(1).replace(',', ''))
        race["surface"] = "芝" if m.group(2) == "芝" else "ダート"
        race["turn"] = "直線" if m.group(3) == "直" else m.group(3)

    # 馬場(芝orダートの後ろの良/稍重/重/不良)
    m = re.search(r'(芝|ダート|ダ)\s+(良|稍重|重|不良)', text)
    if m:
        race["track_condition"] = m.group(2)

    # レース名(「第〇回」「賞」「杯」「記念」「ステークス」「GⅠ」などを含む行)
    for line in lines:
        line = line.strip()
        if (re.search(r'^第\d+回', line) or
            re.search(r'(賞|杯|記念|ステークス|GⅠ|GⅡ|GⅢ)$', line) or
            re.search(r'(賞|杯|記念|ステークス)GⅠ', line)):
            if "お知らせ" not in line and "情報" not in line and "メニュー" not in line:
                race["name"] = line
                break

    # クラス判定: レース名や周辺テキストから G1/G2/G3/L/オープン/3勝/2勝/1勝/未勝利/新馬 を判定
    race["class"] = detect_race_class(race.get("name", "") + " " + text[:500])

    # 各馬の結果
    results = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        parts = line.split('\t')

        if len(parts) >= 8 and parts[0].isdigit() and parts[1].startswith('枠'):
            try:
                finish = int(parts[0])
                number = int(parts[2])
                horse_name = parts[3]
                sex_age = parts[4]
                weight = float(parts[5])
                jockey = parts[6].replace(' ', '')
                time_str = parts[7]
                margin = parts[8] if len(parts) > 8 else ""
            except (ValueError, IndexError):
                i += 1
                continue

            # 次の行: コーナー通過順位
            corner = ""
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if re.match(r'^[\d ]+$', next_line) and next_line:
                    corner = next_line
                    i += 1

            # その次の行: 上がり、馬体重、調教師、人気
            agari = popularity = trainer = body_weight = ""
            if i + 1 < len(lines):
                detail_parts = lines[i + 1].strip().split('\t')
                if len(detail_parts) >= 5:
                    agari = detail_parts[0]
                    body_weight = detail_parts[1]
                    trainer = detail_parts[2].replace(' ', '')
                    popularity = detail_parts[3]
                    i += 1

            # 性齢を分解(例: "牝3" → 牝, 3)
            sex_match = re.match(r'^([牡牝セ]+)(\d+)$', sex_age)
            sex = sex_match.group(1) if sex_match else ""
            age = int(sex_match.group(2)) if sex_match else None

            # 枠番を取り出す(例: "枠8桃" → 8)
            frame_match = re.match(r'^枠(\d+)', parts[1])
            frame = int(frame_match.group(1)) if frame_match else None

            results.append({
                "着順": finish, "枠": frame, "馬番": number,
                "馬名": horse_name, "性別": sex, "年齢": age,
                "斤量": weight, "騎手": jockey,
                "タイム": time_str, "着差": margin, "通過順位": corner,
                "上がり": agari, "馬体重": body_weight,
                "調教師": trainer, "人気": popularity,
            })
        i += 1

    return {"race": race, "results": results}


def import_race_from_text(text):
    """JRA結果テキストから、レース・馬・騎手・出走・結果を全部DBに登録。
    戻り値: (成功フラグ, メッセージ)
    """
    parsed = parse_jra_full(text)
    race_info = parsed["race"]
    horse_results = parsed["results"]

    # 必須情報チェック
    if not race_info.get("date") or not race_info.get("name"):
        return False, "レースの日付か名前が読み取れませんでした。テキスト全体を貼り付けてください。"
    if not horse_results:
        return False, "出走馬の結果が1頭も読み取れませんでした。"

    # レースIDを「RACE-YYYYMMDD-場名」の形で作る
    race_id = f"RACE-{race_info['date'].replace('-','')}-{race_info.get('course','X')}"

    # レース登録(同じIDがあれば置き換え)
    run_sql("""INSERT OR REPLACE INTO races
               (id, date, name, course, surface, distance, turn, track_condition, weather, class)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (race_id, race_info.get("date"), race_info.get("name", ""),
             race_info.get("course", ""), race_info.get("surface", ""),
             race_info.get("distance"), race_info.get("turn", ""),
             race_info.get("track_condition", ""), race_info.get("weather", ""),
             race_info.get("class", "")))

    # 既存の出走・結果を消す(重複登録防止)
    run_sql("DELETE FROM entries WHERE race_id=?", (race_id,))
    run_sql("DELETE FROM results WHERE race_id=?", (race_id,))

    # 各馬を登録
    new_horses = 0
    new_jockeys = 0
    for r in horse_results:
        # 馬を取得 or 新規作成
        conn = get_connection()
        row = conn.execute("SELECT id FROM horses WHERE name=?", (r["馬名"],)).fetchone()
        conn.close()
        if row:
            horse_id = row["id"]
        else:
            horse_id = next_id("horses", "H")
            run_sql("""INSERT INTO horses (id,name,sex,age,running_style,best_distance,
                       best_track,surface_pref,turn_pref,rating,comment)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (horse_id, r["馬名"], r["性別"], r["年齢"],
                     "", "", "", race_info.get("surface", ""),
                     race_info.get("turn", ""), 50, ""))
            new_horses += 1

        # 騎手を取得 or 新規作成
        conn = get_connection()
        row = conn.execute("SELECT id FROM jockeys WHERE name=?", (r["騎手"],)).fetchone()
        conn.close()
        if row:
            jockey_id = row["id"]
        else:
            jockey_id = next_id("jockeys", "J")
            run_sql("""INSERT INTO jockeys (id,name,best_distance,surface_pref,turn_pref,rating,comment)
                       VALUES (?,?,?,?,?,?,?)""",
                    (jockey_id, r["騎手"], "", race_info.get("surface", ""),
                     race_info.get("turn", ""), 50, ""))
            new_jockeys += 1

        # 出走情報を登録 (同じ接続でINSERTとID取得をするため run_sql_returning_id を使う)
        popularity = int(r["人気"]) if r["人気"].isdigit() else None
        entry_id = run_sql_returning_id(
            """INSERT INTO entries (race_id,horse_id,jockey_id,frame,number,weight,odds,popularity)
               VALUES (?,?,?,?,?,?,?,?)""",
            (race_id, horse_id, jockey_id, r["枠"], r["馬番"],
             r["斤量"], None, popularity))

        # 結果を登録
        run_sql("""INSERT INTO results
                   (race_id, entry_id, finish, hit,
                    time, margin, corner, agari, body_weight, trainer)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (race_id, entry_id, r["着順"], 1 if r["着順"] <= 3 else 0,
                 r["タイム"], r["着差"], r["通過順位"], r["上がり"],
                 r["馬体重"], r["調教師"]))

    return True, (f"✅ レース「{race_info.get('name')}」を登録しました。\n"
                  f"・出走 {len(horse_results)} 頭\n"
                  f"・新規登録された馬 {new_horses} 頭\n"
                  f"・新規登録された騎手 {new_jockeys} 名")


# ============================================================
# 【3-2b】出馬表(これから走るレース)のパーサーと取り込み
# ============================================================
def parse_jra_shutuba(text):
    """JRA出馬表(オッズ入り)テキストを解析。
    結果ページと違い、着順・タイムが無い(まだレース前)。
    戻り値: {"race": レース情報, "entries": 出走馬リスト}
    """
    race = {}
    lines = text.split('\n')

    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日.*?\d+回(\S+?)\d+日', text)
    if m:
        race["date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        race["course"] = m.group(4)

    m = re.search(r'(\d[\d,]*)\s*メートル\s*[\(（](芝|ダート|ダ)\s*[・\u30FB](右|左|直線?)', text)
    if m:
        race["distance"] = int(m.group(1).replace(',', ''))
        race["surface"] = "芝" if m.group(2) == "芝" else "ダート"
        race["turn"] = "直線" if m.group(3) == "直" else m.group(3)

    for line in lines:
        line = line.strip()
        if (re.search(r'^第\d+回', line) or
            re.search(r'(賞|杯|記念|ステークス|ダービー|優駿|GⅠ|GⅡ|GⅢ)', line)):
            if all(x not in line for x in ["お知らせ", "情報", "メニュー", "オッズ", "出馬表"]):
                race["name"] = line
                break

    if re.search(r'(GⅠ|G1|Ｇ１)', text): race["class"] = "G1"
    elif re.search(r'(GⅡ|G2|Ｇ２)', text): race["class"] = "G2"
    elif re.search(r'(GⅢ|G3|Ｇ３)', text): race["class"] = "G3"
    race.setdefault("track_condition", "良")
    race.setdefault("weather", "晴")

    entries = []
    current_frame = None
    i = 0
    while i < len(lines):
        parts = lines[i].rstrip().split('\t')
        m_frame = re.match(r'^枠(\d+)', parts[0]) if parts else None
        umaban = horse_name = tansho = None

        if m_frame and len(parts) >= 4:
            current_frame = int(m_frame.group(1))
            if parts[1].isdigit():
                umaban = int(parts[1]); horse_name = parts[2]; tansho = parts[3]
        elif parts and parts[0].isdigit() and len(parts) >= 3:
            umaban = int(parts[0]); horse_name = parts[1]; tansho = parts[2]

        if umaban and horse_name:
            sex = age = weight = jockey = trainer = None
            for j in range(i + 1, min(i + 5, len(lines))):
                p2 = lines[j].split('\t')
                sa = re.match(r'^([牡牝セ]+)(\d+)$', p2[0].strip())
                if sa:
                    sex = sa.group(1); age = int(sa.group(2))
                    for val in p2:
                        v = val.strip()
                        if re.match(r'^\d+\.\d+$', v) and weight is None:
                            weight = float(v)
                        elif weight is not None and jockey is None and v and not re.match(r'^\d', v):
                            jockey = v.replace(' ', '')
                        elif jockey is not None and trainer is None and v:
                            trainer = v.replace(' ', '')
                    break
            try:
                odds = float(tansho)
            except (ValueError, TypeError):
                odds = None
            entries.append({
                "枠": current_frame, "馬番": umaban, "馬名": horse_name,
                "性別": sex, "年齢": age, "斤量": weight,
                "騎手": jockey, "調教師": trainer, "オッズ": odds,
            })
        i += 1

    # オッズから人気を計算
    valid = [e for e in entries if e["オッズ"] is not None]
    valid.sort(key=lambda x: x["オッズ"])
    for rank, e in enumerate(valid, 1):
        e["人気"] = rank
    for e in entries:
        e.setdefault("人気", None)

    return {"race": race, "entries": entries}


def import_shutuba_from_text(text):
    """出馬表テキストから、レース・馬・騎手・出走を登録(結果は無し)。
    戻り値: (成功フラグ, メッセージ)
    """
    parsed = parse_jra_shutuba(text)
    race_info = parsed["race"]
    entries_data = parsed["entries"]

    if not race_info.get("date") or not race_info.get("name"):
        return False, "レースの日付か名前が読み取れませんでした。出馬表全体を貼り付けてください。"
    if not entries_data:
        return False, "出走馬が読み取れませんでした。"

    race_id = f"RACE-{race_info['date'].replace('-','')}-{race_info.get('course','X')}"

    run_sql("""INSERT OR REPLACE INTO races
               (id, date, name, course, surface, distance, turn, track_condition, weather, class)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (race_id, race_info.get("date"), race_info.get("name", ""),
             race_info.get("course", ""), race_info.get("surface", ""),
             race_info.get("distance"), race_info.get("turn", ""),
             race_info.get("track_condition", ""), race_info.get("weather", ""),
             race_info.get("class", "")))

    run_sql("DELETE FROM entries WHERE race_id=?", (race_id,))
    # 結果は出馬表には無いので、resultsは触らない(既存があれば残す)

    new_horses = new_jockeys = 0
    for e in entries_data:
        # 馬を取得 or 新規作成
        conn = get_connection()
        row = conn.execute("SELECT id FROM horses WHERE name=?", (e["馬名"],)).fetchone()
        conn.close()
        if row:
            horse_id = row["id"]
        else:
            horse_id = next_id("horses", "H")
            run_sql("""INSERT INTO horses (id,name,sex,age,running_style,best_distance,
                       best_track,surface_pref,turn_pref,rating,comment)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (horse_id, e["馬名"], e["性別"] or "", e["年齢"],
                     "", "", "", race_info.get("surface", ""),
                     race_info.get("turn", ""), 50, ""))
            new_horses += 1

        # 騎手を取得 or 新規作成
        jockey_name = e["騎手"] or "(未定)"
        conn = get_connection()
        row = conn.execute("SELECT id FROM jockeys WHERE name=?", (jockey_name,)).fetchone()
        conn.close()
        if row:
            jockey_id = row["id"]
        else:
            jockey_id = next_id("jockeys", "J")
            run_sql("""INSERT INTO jockeys (id,name,best_distance,surface_pref,turn_pref,rating,comment)
                       VALUES (?,?,?,?,?,?,?)""",
                    (jockey_id, jockey_name, "", race_info.get("surface", ""),
                     race_info.get("turn", ""), 50, ""))
            new_jockeys += 1

        # 出走情報を登録(オッズ・人気あり、着順なし)
        run_sql("""INSERT INTO entries (race_id,horse_id,jockey_id,frame,number,weight,odds,popularity)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (race_id, horse_id, jockey_id, e["枠"], e["馬番"],
                 e["斤量"], e["オッズ"], e["人気"]))

    return True, (f"✅ 出馬表「{race_info.get('name')}」を登録しました。\n"
                  f"・出走 {len(entries_data)} 頭\n"
                  f"・新規登録された馬 {new_horses} 頭\n"
                  f"・新規登録された騎手 {new_jockeys} 名\n\n"
                  f"「🎯 予想」でこのレースを選ぶと予想点が見られます。")


# ============================================================
# 【3-3】自動評価の計算
# ============================================================
# 過去成績から各馬の評価を0〜100点で算出する。
# 4つの要素を計算し、重みづけで合計。
# 重みは現時点では固定。後で画面から変えられるようにする予定。

# 重みの初期値(合計100%)
AUTO_RATING_WEIGHTS = {
    "win_rate": 30.0,     # 勝率・複勝率の重み
    "fit": 25.0,          # 距離・芝ダ・回り・馬場の適性
    "class": 25.0,        # G1・重賞での実績
    "recent": 20.0,       # 直近の調子
}

# デフォルト値(初期値に戻すときに使う)
DEFAULT_WEIGHTS = dict(AUTO_RATING_WEIGHTS)


def load_weights():
    """DBから保存された重みを読み込む。無ければデフォルトを返す。"""
    w = {}
    for k, v_default in DEFAULT_WEIGHTS.items():
        saved = get_setting(f"weight_{k}", None)
        if saved is not None:
            try:
                w[k] = float(saved)
            except ValueError:
                w[k] = v_default
        else:
            w[k] = v_default
    return w


def save_weights(weights):
    """重みをDBに保存する。"""
    for k, v in weights.items():
        set_setting(f"weight_{k}", v)


def calc_win_rate_score(horse_id):
    """勝率・複勝率に基づくスコアを 0〜100 で返す。"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT r.finish FROM results r
        JOIN entries e ON r.entry_id = e.id
        WHERE e.horse_id = ? AND r.finish IS NOT NULL
    """, (horse_id,)).fetchall()
    conn.close()
    if not rows:
        return 50  # データなしは中央値
    n = len(rows)
    wins = sum(1 for r in rows if r["finish"] == 1)
    placed = sum(1 for r in rows if r["finish"] <= 3)
    win_rate = wins / n
    place_rate = placed / n
    # 勝率を重く、複勝率も加味
    # 勝率20%=平均的、勝率50%以上は上位
    score = win_rate * 200 + place_rate * 50  # 最大250点換算
    # 0〜100にスケーリング
    return min(100, score)


def calc_fit_score(horse_id, race):
    """対象レースの条件(距離・芝ダ・回り・馬場)との適性スコア 0〜100。"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT r.finish, ra.distance, ra.surface, ra.turn, ra.track_condition
        FROM results r
        JOIN entries e ON r.entry_id = e.id
        JOIN races ra ON e.race_id = ra.id
        WHERE e.horse_id = ? AND r.finish IS NOT NULL
    """, (horse_id,)).fetchall()
    conn.close()
    if not rows:
        return 50

    target_dist_cat = categorize_distance(race.get("distance", 0))
    target_surface = race.get("surface", "")
    target_turn = race.get("turn", "")
    target_track = race.get("track_condition", "")

    # 各条件で似たレースの着順平均を見る
    matched = []
    for r in rows:
        score_per = 0
        # 距離区分一致
        if categorize_distance(r["distance"]) == target_dist_cat:
            score_per += 25
        # 芝ダ一致
        if r["surface"] == target_surface:
            score_per += 35
        # 回り一致
        if r["turn"] == target_turn:
            score_per += 20
        # 馬場一致
        if r["track_condition"] == target_track:
            score_per += 20
        # この一致度を着順で重みづけ
        if r["finish"] <= 3:
            matched.append(score_per * 1.0)
        elif r["finish"] <= 5:
            matched.append(score_per * 0.5)
        else:
            matched.append(score_per * 0.2)

    if not matched:
        return 50
    return min(100, sum(matched) / len(matched))


def calc_class_score(horse_id):
    """G1・重賞での実績スコア 0〜100。"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT r.finish, ra.class FROM results r
        JOIN entries e ON r.entry_id = e.id
        JOIN races ra ON e.race_id = ra.id
        WHERE e.horse_id = ? AND r.finish IS NOT NULL
    """, (horse_id,)).fetchall()
    conn.close()
    if not rows:
        return 50

    # クラスごとの点数(高いクラスで好走するほど高い)
    class_value = {"G1": 100, "G2": 80, "G3": 65, "L": 55, "OP": 50,
                   "3勝": 40, "2勝": 30, "1勝": 20, "未勝利": 10, "新馬": 10}
    total = 0
    for r in rows:
        cls = r["class"] or "OP"
        base = class_value.get(cls, 30)
        if r["finish"] == 1:
            total += base
        elif r["finish"] <= 3:
            total += base * 0.6
        elif r["finish"] <= 5:
            total += base * 0.3
        else:
            total += base * 0.1
    # 出走数で割って平均
    return min(100, total / len(rows))


def calc_recent_score(horse_id):
    """直近3走の着順から、調子のスコア 0〜100。"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT r.finish, ra.date FROM results r
        JOIN entries e ON r.entry_id = e.id
        JOIN races ra ON e.race_id = ra.id
        WHERE e.horse_id = ? AND r.finish IS NOT NULL
        ORDER BY ra.date DESC LIMIT 3
    """, (horse_id,)).fetchall()
    conn.close()
    if not rows:
        return 50
    # 着順を点数に: 1着=100, 2着=80, 3着=65, 4-5=45, 6-8=25, それ以下=10
    def finish_to_pts(f):
        if f == 1: return 100
        if f == 2: return 80
        if f == 3: return 65
        if f <= 5: return 45
        if f <= 8: return 25
        return 10
    return sum(finish_to_pts(r["finish"]) for r in rows) / len(rows)


def calculate_auto_rating(horse_id, race=None, weights=None):
    """馬の自動評価を計算する。
    race: 対象レース(dict)。指定があれば「fit」を計算、なければ50固定。
    weights: 重み(dict)。Noneならデフォルトを使う。
    """
    if weights is None:
        weights = AUTO_RATING_WEIGHTS

    win = calc_win_rate_score(horse_id)
    fit = calc_fit_score(horse_id, race) if race else 50
    cls = calc_class_score(horse_id)
    rec = calc_recent_score(horse_id)

    # 重みづけ平均 (重みは%なので合計で割る)
    total_w = weights["win_rate"] + weights["fit"] + weights["class"] + weights["recent"]
    if total_w == 0:
        return 50, {"win": win, "fit": fit, "class": cls, "recent": rec}

    score = (win * weights["win_rate"] +
             fit * weights["fit"] +
             cls * weights["class"] +
             rec * weights["recent"]) / total_w

    return round(score), {"win": round(win, 1), "fit": round(fit, 1),
                          "class": round(cls, 1), "recent": round(rec, 1)}


def update_all_auto_ratings(weights=None):
    """全馬の自動評価を再計算してDBに保存。
    weights: 重み辞書。Noneなら DB から保存値を読み込む。
    戻り値: 更新された頭数
    """
    if weights is None:
        weights = load_weights()
    conn = get_connection()
    horses = conn.execute("SELECT id FROM horses").fetchall()
    conn.close()
    count = 0
    for h in horses:
        score, _ = calculate_auto_rating(h["id"], race=None, weights=weights)
        run_sql("UPDATE horses SET auto_rating=? WHERE id=?", (score, h["id"]))
        count += 1
    return count


# ============================================================
# 【3-4】予想点 (レースごとに計算する条件依存の評価)
# ============================================================
# 設計方針:
#  ・コア5スコアを算出 → 偏差値化 → 重みづけ → 合計
#  ・着順だけで評価せず、着差・タイム・上がり・条件適性・騎手を重視
#  ・人気/オッズは予想点に含めず、画面で比較表示するだけ
#  ・データがないスコアの扱いは設定で選べる(0点 / 除外 / 中央値50)

RACE_SCORE_WEIGHTS = {
    "zenso": 25.0,    # 前走スコア
    "speed": 25.0,    # 速さスコア(タイム差・上がり差・着差を統合)
    "fit":   20.0,    # 条件適性スコア(距離・競馬場・馬場・芝ダ)
    "klass": 15.0,    # クラススコア
    "jockey": 15.0,   # 騎手スコア
}
DEFAULT_RACE_WEIGHTS = dict(RACE_SCORE_WEIGHTS)

JOCKEY_SUB_WEIGHTS = {
    "winrate": 40.0,  # 通算勝率・複勝率
    "combi":   30.0,  # 馬とのコンビ成績
    "cond":    20.0,  # 今回条件での成績
    "recent":   0.0,  # 直近の調子(初期値0=無効、いつでも有効化可)
}
DEFAULT_JOCKEY_SUB_WEIGHTS = dict(JOCKEY_SUB_WEIGHTS)


def load_race_weights():
    w = {}
    for k, v_default in DEFAULT_RACE_WEIGHTS.items():
        saved = get_setting(f"raceweight_{k}", None)
        try:
            w[k] = float(saved) if saved is not None else v_default
        except ValueError:
            w[k] = v_default
    return w


def save_race_weights(weights):
    for k, v in weights.items():
        set_setting(f"raceweight_{k}", v)


def load_jockey_sub_weights():
    w = {}
    for k, v_default in DEFAULT_JOCKEY_SUB_WEIGHTS.items():
        saved = get_setting(f"jksub_{k}", None)
        try:
            w[k] = float(saved) if saved is not None else v_default
        except ValueError:
            w[k] = v_default
    return w


def save_jockey_sub_weights(weights):
    for k, v in weights.items():
        set_setting(f"jksub_{k}", v)


def parse_time_to_seconds(time_str):
    """'2:25.6' のようなタイム文字列を秒(float)に変換。失敗ならNone。"""
    if not time_str:
        return None
    try:
        s = str(time_str).strip()
        if ":" in s:
            m, rest = s.split(":")
            return int(m) * 60 + float(rest)
        return float(s)
    except (ValueError, AttributeError):
        return None


def parse_margin_to_seconds(margin_str):
    """着差文字列をおおよその秒数に変換(JRA着差表記の近似)。"""
    if not margin_str:
        return 0.0
    s = str(margin_str).strip()
    table = {"ハナ": 0.0, "アタマ": 0.05, "クビ": 0.1, "大差": 3.0}
    if s in table:
        return table[s]
    total = 0.0
    m = re.match(r'^(\d+(?:\.\d+)?)', s)
    if m:
        total += float(m.group(1)) * 0.2  # 1馬身≒0.2秒
    for frac, val in [("3/4", 0.15), ("３／４", 0.15),
                      ("1/2", 0.1), ("１／２", 0.1),
                      ("1/4", 0.05), ("１／４", 0.05)]:
        if frac in s:
            total += val
    return total


def to_deviation(values):
    """数値リストを偏差値(平均50・標準偏差10)に変換。Noneはそのまま残す。"""
    nums = [v for v in values if v is not None]
    if len(nums) == 0:
        return [None for _ in values]
    mean = sum(nums) / len(nums)
    var = sum((v - mean) ** 2 for v in nums) / len(nums)
    std = var ** 0.5
    result = []
    for v in values:
        if v is None:
            result.append(None)
        elif std == 0:
            result.append(50.0)
        else:
            result.append(50 + 10 * (v - mean) / std)
    return result


def raw_zenso_score(horse_id, before_date):
    """前走スコアの素点。人気差(人気-着順)+着順+着差+上がりで評価。"""
    conn = get_connection()
    row = conn.execute("""
        SELECT r.finish, r.margin, r.agari, e.popularity
        FROM results r
        JOIN entries e ON r.entry_id = e.id
        JOIN races ra ON e.race_id = ra.id
        WHERE e.horse_id = ? AND r.finish IS NOT NULL AND ra.date < ?
        ORDER BY ra.date DESC LIMIT 1
    """, (horse_id, before_date)).fetchone()
    conn.close()
    if not row:
        return None
    score = 50.0
    if row["popularity"] and row["finish"]:
        score += (row["popularity"] - row["finish"]) * 6
    if row["finish"]:
        score += max(0, (6 - row["finish"])) * 3
    score -= parse_margin_to_seconds(row["margin"]) * 8
    try:
        agari = float(row["agari"]) if row["agari"] else None
    except ValueError:
        agari = None
    if agari:
        score += (35.0 - agari) * 8
    return score


def raw_speed_score(horse_id, race, missing_mode):
    """速さスコアの素点。同条件(距離区分+芝ダ必須)での優勝馬とのタイム差。"""
    target_dist_cat = categorize_distance(race.get("distance", 0))
    target_surface = race.get("surface", "")
    conn = get_connection()
    rows = conn.execute("""
        SELECT r.time, ra.distance, ra.surface, e.race_id AS race_id
        FROM results r
        JOIN entries e ON r.entry_id = e.id
        JOIN races ra ON e.race_id = ra.id
        WHERE e.horse_id = ? AND r.finish IS NOT NULL
    """, (horse_id,)).fetchall()
    diffs = []
    for r in rows:
        if categorize_distance(r["distance"]) != target_dist_cat:
            continue
        if r["surface"] != target_surface:
            continue
        win_row = conn.execute(
            "SELECT time FROM results WHERE race_id=? AND finish=1",
            (r["race_id"],)).fetchone()
        my_time = parse_time_to_seconds(r["time"])
        win_time = parse_time_to_seconds(win_row["time"]) if win_row else None
        if my_time is not None and win_time is not None:
            diffs.append(my_time - win_time)
    conn.close()
    if not diffs:
        return None
    avg_diff = sum(diffs) / len(diffs)
    return max(0, 100 - avg_diff * 50)


def raw_fit_score(horse_id, race):
    """条件適性スコアの素点。距離・芝ダ・競馬場・回り・馬場の一致度×着順。"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT r.finish, ra.distance, ra.surface, ra.turn, ra.track_condition, ra.course
        FROM results r
        JOIN entries e ON r.entry_id = e.id
        JOIN races ra ON e.race_id = ra.id
        WHERE e.horse_id = ? AND r.finish IS NOT NULL
    """, (horse_id,)).fetchall()
    conn.close()
    if not rows:
        return None
    tdc = categorize_distance(race.get("distance", 0))
    tsf = race.get("surface", "")
    ttn = race.get("turn", "")
    ttr = race.get("track_condition", "")
    tco = race.get("course", "")
    scores = []
    for r in rows:
        match = 0
        if categorize_distance(r["distance"]) == tdc: match += 30
        if r["surface"] == tsf: match += 30
        if r["course"] == tco: match += 20
        if r["turn"] == ttn: match += 10
        if r["track_condition"] == ttr: match += 10
        if r["finish"] == 1: scores.append(match * 1.0)
        elif r["finish"] <= 3: scores.append(match * 0.7)
        elif r["finish"] <= 5: scores.append(match * 0.4)
        else: scores.append(match * 0.15)
    return sum(scores) / len(scores) if scores else None


def raw_class_score(horse_id):
    """クラススコアの素点(既存calc_class_scoreを流用)。"""
    return calc_class_score(horse_id)


def raw_jockey_score(jockey_id, horse_id, race, sub_weights=None):
    """騎手スコアの素点。通算勝率/コンビ/条件/直近の重みづけ。"""
    if sub_weights is None:
        sub_weights = load_jockey_sub_weights()
    conn = get_connection()
    all_rows = conn.execute("""
        SELECT r.finish FROM results r JOIN entries e ON r.entry_id = e.id
        WHERE e.jockey_id = ? AND r.finish IS NOT NULL""", (jockey_id,)).fetchall()
    combi_rows = conn.execute("""
        SELECT r.finish FROM results r JOIN entries e ON r.entry_id = e.id
        WHERE e.jockey_id = ? AND e.horse_id = ? AND r.finish IS NOT NULL""",
        (jockey_id, horse_id)).fetchall()
    cond_rows = conn.execute("""
        SELECT r.finish, ra.distance, ra.surface FROM results r
        JOIN entries e ON r.entry_id = e.id JOIN races ra ON e.race_id = ra.id
        WHERE e.jockey_id = ? AND r.finish IS NOT NULL""", (jockey_id,)).fetchall()
    recent_rows = conn.execute("""
        SELECT r.finish FROM results r JOIN entries e ON r.entry_id = e.id
        JOIN races ra ON e.race_id = ra.id
        WHERE e.jockey_id = ? AND r.finish IS NOT NULL
        ORDER BY ra.date DESC LIMIT 20""", (jockey_id,)).fetchall()
    conn.close()

    def wp(rows):
        if not rows:
            return None
        n = len(rows)
        win = sum(1 for r in rows if r["finish"] == 1) / n
        place = sum(1 for r in rows if r["finish"] <= 3) / n
        return min(100, win * 200 + place * 50)

    tdc = categorize_distance(race.get("distance", 0))
    tsf = race.get("surface", "")
    cond_matched = [r for r in cond_rows
                    if categorize_distance(r["distance"]) == tdc and r["surface"] == tsf]
    parts = [("winrate", wp(all_rows)), ("combi", wp(combi_rows)),
             ("cond", wp(cond_matched)), ("recent", wp(recent_rows))]
    num = den = 0.0
    for key, val in parts:
        w = sub_weights.get(key, 0)
        if val is not None and w > 0:
            num += val * w
            den += w
    return num / den if den > 0 else None


def get_horse_recent_races(horse_id, before_date=None, limit=None):
    """馬の近走履歴を新しい順に取得する。
    before_date: 指定した日付より前のレースだけ(予想対象レースを除くため)。
    limit: 取得する走数。Noneなら全部。
    戻り値: list of dict(日付・レース名・クラス・着順・距離・タイム・上がり・騎手・人気・着差)
    """
    conn = get_connection()
    sql = f"""
        SELECT ra.date AS 日付, ra.name AS レース名, ra.class AS クラス,
               r.finish AS 着順, ra.surface AS 芝ダ, ra.distance AS 距離,
               r.time AS タイム, r.agari AS 上がり, j.name AS 騎手,
               e.popularity AS 人気, r.margin AS 着差
        FROM results r
        JOIN entries e ON r.entry_id = e.id
        JOIN races ra ON e.race_id = ra.id
        LEFT JOIN jockeys j ON e.jockey_id = j.id
        WHERE e.horse_id = {PH} AND r.finish IS NOT NULL
    """
    params = [horse_id]
    if before_date:
        sql += f" AND ra.date < {PH}"
        params.append(before_date)
    sql += " ORDER BY ra.date DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, tuple(params)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def calculate_race_scores(race_id):
    """指定レースの全出走馬の予想点を計算。
    各スコア→偏差値化→重みづけ→合計→予想点。予想点もレース内偏差値化。
    """
    weights = load_race_weights()
    jk_sub = load_jockey_sub_weights()
    missing_mode = get_setting("missing_mode", "zero")

    conn = get_connection()
    race_row = conn.execute("SELECT * FROM races WHERE id=?", (race_id,)).fetchone()
    entries = conn.execute("""
        SELECT e.*, h.name AS horse_name, j.name AS jockey_name
        FROM entries e
        LEFT JOIN horses h ON e.horse_id = h.id
        LEFT JOIN jockeys j ON e.jockey_id = j.id
        WHERE e.race_id = ?
    """, (race_id,)).fetchall()
    conn.close()
    if not race_row or not entries:
        return []

    race = dict(race_row)
    before_date = race.get("date", "9999-12-31")

    horses_data = []
    for e in entries:
        hid = e["horse_id"]; jid = e["jockey_id"]
        raw = {
            "zenso": raw_zenso_score(hid, before_date),
            "speed": raw_speed_score(hid, race, missing_mode),
            "fit":   raw_fit_score(hid, race),
            "klass": raw_class_score(hid),
            "jockey": raw_jockey_score(jid, hid, race, jk_sub),
        }
        horses_data.append({
            "馬番": e["number"], "馬名": e["horse_name"],
            "騎手": e["jockey_name"], "人気": e["popularity"],
            "オッズ": e["odds"], "raw": raw,
        })

    score_keys = ["zenso", "speed", "fit", "klass", "jockey"]
    deviations = {}
    for key in score_keys:
        deviations[key] = to_deviation([h["raw"][key] for h in horses_data])

    for idx, h in enumerate(horses_data):
        total = wsum = 0.0
        detail = {}
        for key in score_keys:
            dev = deviations[key][idx]
            w = weights.get(key, 0)
            if dev is None:
                if missing_mode == "zero":
                    dev_use = 0.0
                elif missing_mode == "median":
                    dev_use = 50.0
                else:
                    dev_use = None
            else:
                dev_use = dev
            detail[key] = round(dev, 1) if dev is not None else None
            if dev_use is not None and w > 0:
                total += dev_use * w
                wsum += w
        h["_total_raw"] = (total / wsum) if wsum > 0 else 0
        h["_detail"] = detail

    totals = [h["_total_raw"] for h in horses_data]
    total_devs = to_deviation(totals)
    for idx, h in enumerate(horses_data):
        h["予想点"] = round(h["_total_raw"], 1)
        h["予想偏差値"] = round(total_devs[idx], 1) if total_devs[idx] is not None else 50.0

    horses_data.sort(key=lambda x: x["予想点"], reverse=True)
    return horses_data


# ============================================================
# 【4】サンプルデータ
# ============================================================
def seed_victoria_mile():
    def rating_from_pop(p):
        table = {1: 88, 2: 84, 3: 80, 4: 76, 5: 72}
        if p in table: return table[p]
        if p <= 7: return 68
        if p <= 10: return 62
        if p <= 13: return 56
        return 50
    data = [
        (1,"カピリナ","牝",5,"ダンカーク","ライトリーチューン","マンハッタンカフェ","横山典弘",56.0,96.2,16,17),
        (2,"ワイドラトゥール","牝",5,"カリフォルニアクローム","ワイドサファイア","アグネスタキオン","横山武史",56.0,146.8,18,15),
        (3,"マピュース","牝",4,"マインドユアビスケッツ","フィルムフランセ","シンボリクリスエス","F.ゴンサルベス",56.0,82.3,15,9),
        (4,"エリカエクスプレス","牝",4,"エピファネイア","エンタイスド","Galileo","武豊",56.0,22.2,6,4),
        (5,"ケリフレッドアスク","牝",4,"ドゥラメンテ","ディープインアスク","ディープインパクト","M.ディー",56.0,102.2,17,14),
        (6,"ラヴァンダ","牝",5,"シルバーステート","ゴッドパイレーツ","ベーカバド","岩田望来",56.0,22.7,7,7),
        (7,"クイーンズウォーク","牝",5,"キズナ","ウェイヴェルアベニュー","Harlington","西村淳也",56.0,9.0,3,3),
        (8,"カムニャック","牝",4,"ブラックタイド","ダンスアミーガ","サクラバクシンオー","川田将雅",56.0,5.8,2,2),
        (9,"ココナッツブラウン","牝",6,"キタサンブラック","ルアーズストリート","キングカメハメハ","北村友一",56.0,25.1,9,5),
        (10,"ドロップオブライト","牝",7,"トーセンラー","プレシャスドロップ","フレンチデピュティ","松若風馬",56.0,73.2,14,18),
        (11,"ボンドガール","牝",5,"ダイワメジャー","コーステッド","Tizway","丹内祐次",56.0,34.4,11,11),
        (12,"エンブロイダリー","牝",4,"アドマイヤマーズ","ロッテンマイヤー","クロフネ","C.ルメール",56.0,1.9,1,1),
        (13,"カナテープ","牝",7,"ロードカナロア","ティッカーテープ","Royal Applause","松山弘平",56.0,34.2,10,10),
        (14,"ジョスラン","牝",4,"エピファネイア","ケイティーズハート","ハーツクライ","戸崎圭太",56.0,24.4,8,8),
        (15,"アイサンサン","牝",4,"キズナ","ウアジェト","シンボリクリスエス","幸英明",56.0,41.1,13,13),
        (16,"ニシノティアモ","牝",5,"ドゥラメンテ","ニシノアモーレ","コンデュイット","津村明秀",56.0,13.6,4,6),
        (17,"パラディレーヌ","牝",4,"キズナ","パラダイスガーデン","Closing Argument","坂井瑠星",56.0,37.2,12,12),
        (18,"チェルヴィニア","牝",5,"ハービンジャー","チェッキーノ","キングカメハメハ","D.レーン",56.0,18.5,5,16),
    ]
    _seed_race("第21回 ヴィクトリアマイル(GⅠ)", "2026-05-17", "東京", "芝", 1600,
               "左", "良", "晴", data, rating_from_pop, with_result=True)


def seed_oaks_2026():
    data = [
        (1,"ミツカネベネレ","横山典弘"),(2,"レイラシック","M.デムーロ"),(3,"カランカール","横山和生"),
        (4,"シンクトゥルーサリー","津村明秀"),(5,"リアライズミナル","岩田望来"),(6,"シンギングフィール","池添謙一"),
        (7,"ジョーニングモデル","戸崎圭太"),(8,"スマートプリエール","北村宏司"),(9,"トルニーテ","西村淳也"),
        (10,"スターアニス","松山弘平"),(11,"コスティルム","鮫島克駿"),(12,"ドリームコア","C.デムーロ"),
        (13,"ロンギ","菅原明良"),(14,"ジュウリョクピエロ","今村聖奈"),(15,"アンジュドゥエル","田辺裕信"),
        (16,"シュウショウピアス","三浦皇成"),(17,"スウィートハピネス","丹内祐次"),(18,"ラフターラインズ","D.レーン"),
    ]
    race_id = "RACE-OAKS2026"
    run_sql("DELETE FROM races WHERE id=?", (race_id,))
    run_sql("""INSERT INTO races (id,date,name,course,surface,distance,turn,track_condition,weather)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (race_id, "2026-05-24", "第87回 オークス(優駿牝馬・GⅠ)", "東京", "芝", 2400, "左", "良", "晴"))
    run_sql("DELETE FROM entries WHERE race_id=?", (race_id,))
    for (num, name, jky_name) in data:
        horse_id = get_or_create_horse(name, sex="牝", age=3, best_distance="長距離",
                                       surface_pref="芝", turn_pref="左", comment="オークス2026 出走")
        jockey_id = get_or_create_jockey(jky_name, surface_pref="芝", turn_pref="左")
        run_sql("""INSERT INTO entries (race_id,horse_id,jockey_id,frame,number,weight,odds,popularity)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (race_id, horse_id, jockey_id, (num - 1) // 2 + 1, num, 55.0, None, None))


def _seed_race(name, date, course, surface, distance, turn, track, weather,
               data, rating_fn, with_result):
    """ヴィクトリアマイル形式（結果あり）のデータを入れる内部関数。"""
    race_id = "RACE-" + name
    run_sql("DELETE FROM races WHERE id=?", (race_id,))
    run_sql("""INSERT INTO races (id,date,name,course,surface,distance,turn,track_condition,weather)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (race_id, date, name, course, surface, distance, turn, track, weather))
    run_sql("DELETE FROM entries WHERE race_id=?", (race_id,))
    run_sql("DELETE FROM results WHERE race_id=?", (race_id,))
    for (num, hname, sex, age, sire, dam, dam_sire, jky, weight, odds, pop, finish) in data:
        horse_id = get_or_create_horse(hname, sex=sex, age=age, sire=sire, dam=dam,
                                       dam_sire=dam_sire, best_distance="マイル",
                                       surface_pref="芝", turn_pref="左",
                                       rating=rating_fn(pop), comment=f"{name} {pop}人気{finish}着")
        jockey_id = get_or_create_jockey(jky, surface_pref="芝", turn_pref="左", rating=75)
        eid = run_sql_returning_id(
            """INSERT INTO entries (race_id,horse_id,jockey_id,frame,number,weight,odds,popularity)
               VALUES (?,?,?,?,?,?,?,?)""",
            (race_id, horse_id, jockey_id, (num - 1) // 2 + 1, num, weight, odds, pop))
        if with_result:
            run_sql("""INSERT INTO results (race_id,entry_id,finish,final_odds,hit,memo)
                       VALUES (?,?,?,?,?,?)""",
                    (race_id, eid, finish, odds, 1 if finish <= 3 else 0,
                     "勝利!" if finish == 1 else ""))


# ----- 馬・騎手を「名前で探して、なければ作る」ヘルパー -----
# これで同じ名前の馬が二重登録されるのを防ぎつつ、IDは自動採番される。
def get_or_create_horse(name, **kw):
    conn = get_connection()
    row = conn.execute("SELECT id FROM horses WHERE name=?", (name,)).fetchone()
    conn.close()
    if row:
        return row["id"]
    new_id = next_id("horses", "H")
    run_sql("""INSERT INTO horses (id,name,sex,age,sire,dam,dam_sire,running_style,
               best_distance,best_track,surface_pref,turn_pref,rating,comment)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id, name, kw.get("sex",""), kw.get("age"), kw.get("sire",""),
             kw.get("dam",""), kw.get("dam_sire",""), kw.get("running_style",""),
             kw.get("best_distance",""), kw.get("best_track",""), kw.get("surface_pref",""),
             kw.get("turn_pref",""), kw.get("rating",50), kw.get("comment","")))
    return new_id


def get_or_create_jockey(name, **kw):
    conn = get_connection()
    row = conn.execute("SELECT id FROM jockeys WHERE name=?", (name,)).fetchone()
    conn.close()
    if row:
        return row["id"]
    new_id = next_id("jockeys", "J")
    run_sql("""INSERT INTO jockeys (id,name,best_distance,best_track,surface_pref,turn_pref,rating,comment)
               VALUES (?,?,?,?,?,?,?,?)""",
            (new_id, name, kw.get("best_distance",""), kw.get("best_track",""),
             kw.get("surface_pref",""), kw.get("turn_pref",""), kw.get("rating",50), kw.get("comment","")))
    return new_id


# ============================================================
# 【5】画面
# ============================================================
st.set_page_config(page_title="競馬予想分析", page_icon="🏇", layout="wide")
init_db()
st.title("🏇 競馬予想分析アプリ")

menu = st.sidebar.radio(
    "メニュー",
    ["📊 ダッシュボード", "🐎 馬", "👤 騎手", "📅 レース",
     "🎯 予想", "📈 ランキング",
     "📥 取り込み", "💰 収支",
     "⚙️ 設定"]
)


# ------------------------------------------------------------
# ダッシュボード
# ------------------------------------------------------------
if menu == "📊 ダッシュボード":
    st.header("ダッシュボード")
    horses = load_table("horses"); jockeys = load_table("jockeys")
    races = load_table("races"); entries = load_table("entries"); bets = load_table("bets")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🐎 馬", len(horses)); c2.metric("👤 騎手", len(jockeys))
    c3.metric("📅 レース", len(races)); c4.metric("🎯 出走情報", len(entries))
    spent = bets["amount"].sum() if len(bets) else 0
    payout = bets["payout"].sum() if len(bets) else 0
    roi = (payout / spent * 100) if spent > 0 else 0
    st.metric("💹 回収率", f"{roi:.1f}%", f"投資 {spent:,}円 → 払戻 {payout:,}円")

    st.divider()
    st.subheader("総合点の計算式")
    st.code("基礎点 = 馬評価 × 0.5 + 騎手評価 × 0.3\n"
            "＋6 馬の得意距離一致 / ＋5 芝ダ一致 / ＋4 馬場一致 / ＋3 回り一致\n"
            "＋2 騎手の得意距離 / ＋2 騎手の芝ダ")

    st.divider()
    st.subheader("🎯 自動評価")
    st.write("過去の取り込み済みレースから、各馬の評価(0〜100)を自動計算します。")
    # 現在の重みを表示(設定画面で変更可)
    _w = load_weights()
    st.caption(f"現在の計算式: 勝率{_w['win_rate']:.1f}% / "
               f"距離・馬場適性{_w['fit']:.1f}% / "
               f"G1・重賞実績{_w['class']:.1f}% / "
               f"直近の調子{_w['recent']:.1f}% "
               f"(変更は ⚙️ 設定 から)")
    if st.button("🔄 全馬の自動評価を更新", type="primary"):
        with st.spinner("計算中..."):
            n = update_all_auto_ratings()
        st.success(f"✅ {n} 頭の自動評価を更新しました。「🐎 馬」画面で確認できます。")

    st.divider()
    st.subheader("サンプルデータ")
    col1, col2 = st.columns(2)
    if col1.button("🏆 ヴィクトリアマイル2026を投入"):
        seed_victoria_mile(); st.success("投入しました！")
    if col2.button("🌸 オークス2026を投入"):
        seed_oaks_2026(); st.success("投入しました！「🐎 馬」で評価をつけてください。")

    st.divider()
    st.subheader("データ削除")
    if st.checkbox("本当に全部消す（チェックしてからボタン）"):
        if st.button("🗑 全データ削除"):
            for t in ["horses", "jockeys", "races", "entries", "results", "bets"]:
                run_sql(f"DELETE FROM {t}")
            st.success("全データを削除しました。")


# ------------------------------------------------------------
# 馬
# ------------------------------------------------------------
elif menu == "🐎 馬":
    st.header("馬データベース")
    horses = load_table("horses")

    if len(horses) == 0:
        st.info("まだ馬が登録されていません。下の「馬を追加」から登録できます。")
    else:
        # --- 1. 検索(一番上) ---
        q = st.text_input("🔍 馬名・IDで検索", key="horse_search")
        view = horses
        if q:
            view = horses[horses["name"].str.contains(q, case=False, na=False) |
                          horses["id"].str.contains(q, case=False, na=False)]
        # 自動評価優先でソート
        view = view.copy()
        view["_sort_key"] = view["auto_rating"].fillna(view["rating"]).fillna(0)
        view = view.sort_values("_sort_key", ascending=False).drop(columns=["_sort_key"])

        # --- 2. 一覧(スッキリ表示。手で変えた馬には🖊印) ---
        st.subheader("登録済みの馬")
        st.caption(f"{len(view)} 件表示")
        list_rows = []
        for _, r in view.iterrows():
            # 手動評価が入っていて、かつ50(初期値)でない＝手で変えた印
            manual = r["rating"]
            edited = (manual is not None and manual != 50)
            list_rows.append({
                "ID": r["id"], "名前": r["name"],
                "自動評価": r["auto_rating"],
                "手動評価": (f"{int(manual)} 🖊" if edited else ""),
                "性別": r["sex"], "年齢": r["age"],
                "得意距離": r["best_distance"], "脚質": r["running_style"],
            })
        st.dataframe(pd.DataFrame(list_rows), use_container_width=True, hide_index=True)
        st.caption("🖊 = 手動評価で上書きした馬。予想ではその値が優先されます。")

        # --- 3. 詳細・評価編集(馬を選ぶと内訳が見える) ---
        st.divider()
        st.subheader("✏️ 馬の詳細・評価編集")
        opt = {f'{r["name"]}（{r["id"]}）': r["id"] for _, r in view.iterrows()}
        sel = st.selectbox("馬を選ぶ", list(opt.keys()), key="edit_horse_sel")
        if sel:
            hid = opt[sel]
            target = horses[horses["id"] == hid].iloc[0]

            # 自動評価の内訳を計算して表示
            win = calc_win_rate_score(hid)
            cls = calc_class_score(hid)
            rec = calc_recent_score(hid)
            cc1, cc2, cc3, cc4 = st.columns(4)
            cc1.metric("勝率スコア", f"{win:.0f}")
            cc2.metric("クラススコア", f"{cls:.0f}")
            cc3.metric("直近スコア", f"{rec:.0f}")
            cc4.metric("自動評価", f"{target['auto_rating']}" if target['auto_rating'] is not None else "未計算")
            st.caption("※内訳スコアは、データが増えるほど正確になります")

            # 手動評価の編集
            st.write("**手動評価**(入れると予想で自動評価より優先されます)")
            cur_manual = int(target["rating"]) if target["rating"] is not None else 50
            ec1, ec2, ec3 = st.columns([2, 1, 1])
            with ec1:
                new_rating = st.number_input("手動評価(0〜100)", 0, 100, cur_manual,
                                             step=1, key="edit_horse_rating")
            with ec2:
                st.write("")  # 縦位置調整
                st.write("")
                if st.button("💾 保存", key="save_horse_rating"):
                    run_sql("UPDATE horses SET rating=? WHERE id=?", (new_rating, hid))
                    st.success("手動評価を保存しました。"); st.rerun()
            with ec3:
                st.write("")
                st.write("")
                if st.button("🔄 リセット", key="reset_horse_rating",
                             help="この馬の手動評価を消して、自動評価ベースに戻す"):
                    # 手動評価を初期値50に戻す = 「手で変えてない」状態
                    run_sql("UPDATE horses SET rating=50 WHERE id=?", (hid,))
                    st.success("この馬の手動評価をリセットしました。"); st.rerun()

            # 脚質の編集(リスト選択)
            styles = ["", "逃げ", "先行", "差し", "追込", "自在"]
            cur_style = target["running_style"] if target["running_style"] in styles else ""
            new_style = st.selectbox("脚質", styles, index=styles.index(cur_style),
                                     key="edit_horse_style")
            if st.button("脚質を保存", key="save_horse_style"):
                run_sql("UPDATE horses SET running_style=? WHERE id=?", (new_style, hid))
                st.success("脚質を保存しました。"); st.rerun()

            # 削除
            with st.expander("🗑 この馬を削除"):
                if st.button("削除する", key="del_this_horse"):
                    run_sql("DELETE FROM horses WHERE id=?", (hid,))
                    st.success("削除しました。"); st.rerun()

    # --- 4. 追加フォーム(一番下) ---
    st.divider()
    with st.expander("➕ 馬を追加（IDは自動で振られます）"):
        with st.form("add_horse"):
            col1, col2 = st.columns(2)
            with col1:
                h_name = st.text_input("馬名 *")
                h_sex = st.selectbox("性別", ["", "牡", "牝", "セン"])
                h_age = st.number_input("年齢", 0, 30, 4)
                h_style = st.selectbox("脚質", ["", "逃げ", "先行", "差し", "追込", "自在"])
            with col2:
                h_dist = st.selectbox("得意距離", ["", "短距離", "マイル", "中距離", "長距離"])
                h_track = st.selectbox("得意馬場", ["", "良", "稍重", "重", "不良"])
                h_surface = st.selectbox("芝ダート適性", ["", "芝", "ダート"])
                h_turn = st.selectbox("左右回り適性", ["", "右", "左"])
                h_rating = st.slider("手動評価", 0, 100, 50)
            h_comment = st.text_input("コメント")
            if st.form_submit_button("保存"):
                if h_name:
                    new_id = next_id("horses", "H")
                    run_sql("""INSERT INTO horses (id,name,sex,age,running_style,best_distance,
                               best_track,surface_pref,turn_pref,rating,comment)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (new_id, h_name, h_sex, h_age, h_style, h_dist, h_track,
                             h_surface, h_turn, h_rating, h_comment))
                    st.success(f"「{h_name}」を {new_id} として保存しました。"); st.rerun()
                else:
                    st.error("馬名は必須です。")


# ------------------------------------------------------------
# 騎手
# ------------------------------------------------------------
elif menu == "👤 騎手":
    st.header("騎手データベース")
    jockeys = load_table("jockeys")

    if len(jockeys) == 0:
        st.info("まだ騎手が登録されていません。下の「騎手を追加」から登録できます。")
    else:
        # 1. 検索
        q = st.text_input("🔍 騎手名・IDで検索", key="jockey_search")
        view = jockeys
        if q:
            view = jockeys[jockeys["name"].str.contains(q, case=False, na=False) |
                           jockeys["id"].str.contains(q, case=False, na=False)]
        view = view.sort_values("rating", ascending=False)

        # 2. 一覧(勝率・複勝率も表示)
        st.subheader("登録済みの騎手")
        st.caption(f"{len(view)} 件表示")
        jk_rows = []
        for _, r in view.iterrows():
            # 勝率・複勝率を計算
            conn = get_connection()
            res = conn.execute("""
                SELECT r.finish FROM results r JOIN entries e ON r.entry_id=e.id
                WHERE e.jockey_id=? AND r.finish IS NOT NULL""", (r["id"],)).fetchall()
            conn.close()
            n = len(res)
            win = sum(1 for x in res if x["finish"] == 1)
            place = sum(1 for x in res if x["finish"] <= 3)
            manual = r["rating"]
            edited = (manual is not None and manual != 50)
            jk_rows.append({
                "ID": r["id"], "名前": r["name"],
                "騎乗数": n,
                "勝率": f"{win/n*100:.0f}%" if n else "-",
                "複勝率": f"{place/n*100:.0f}%" if n else "-",
                "手動評価": (f"{int(manual)} 🖊" if edited else ""),
            })
        st.dataframe(pd.DataFrame(jk_rows), use_container_width=True, hide_index=True)
        st.caption("🖊 = 手動評価で上書きした騎手")

        # 3. 詳細・評価編集
        st.divider()
        st.subheader("✏️ 騎手の詳細・評価編集")
        opt = {f'{r["name"]}（{r["id"]}）': r["id"] for _, r in view.iterrows()}
        sel = st.selectbox("騎手を選ぶ", list(opt.keys()), key="edit_jky_sel")
        if sel:
            jid = opt[sel]
            target = jockeys[jockeys["id"] == jid].iloc[0]
            cur_manual = int(target["rating"]) if target["rating"] is not None else 50
            ec1, ec2, ec3 = st.columns([2, 1, 1])
            with ec1:
                new_rating = st.number_input("手動評価(0〜100)", 0, 100, cur_manual,
                                             step=1, key="edit_jky_rating")
            with ec2:
                st.write(""); st.write("")
                if st.button("💾 保存", key="save_jky"):
                    run_sql("UPDATE jockeys SET rating=? WHERE id=?", (new_rating, jid))
                    st.success("保存しました。"); st.rerun()
            with ec3:
                st.write(""); st.write("")
                if st.button("🔄 リセット", key="reset_jky"):
                    run_sql("UPDATE jockeys SET rating=50 WHERE id=?", (jid,))
                    st.success("リセットしました。"); st.rerun()
            with st.expander("🗑 この騎手を削除"):
                if st.button("削除する", key="del_this_jky"):
                    run_sql("DELETE FROM jockeys WHERE id=?", (jid,))
                    st.success("削除しました。"); st.rerun()

    # 4. 追加フォーム(一番下)
    st.divider()
    with st.expander("➕ 騎手を追加（IDは自動で振られます）"):
        with st.form("add_jockey"):
            col1, col2 = st.columns(2)
            with col1:
                j_name = st.text_input("騎手名 *")
                j_dist = st.selectbox("得意距離", ["", "短距離", "マイル", "中距離", "長距離"])
            with col2:
                j_surface = st.selectbox("芝ダート適性", ["", "芝", "ダート"])
                j_turn = st.selectbox("左右回り適性", ["", "右", "左"])
                j_rating = st.slider("手動評価", 0, 100, 50)
            j_comment = st.text_input("コメント")
            if st.form_submit_button("保存"):
                if j_name:
                    new_id = next_id("jockeys", "J")
                    run_sql("""INSERT INTO jockeys (id,name,best_distance,surface_pref,turn_pref,rating,comment)
                               VALUES (?,?,?,?,?,?,?)""",
                            (new_id, j_name, j_dist, j_surface, j_turn, j_rating, j_comment))
                    st.success(f"「{j_name}」を {new_id} として保存しました。"); st.rerun()
                else:
                    st.error("騎手名は必須です。")


# ------------------------------------------------------------
# レース
# ------------------------------------------------------------
elif menu == "📅 レース":
    st.header("📅 レース")
    races = load_table("races")

    # 1. 検索 + 一覧(上)
    if len(races) == 0:
        st.info("まだレースが登録されていません。下の「レースを追加」から登録できます。")
    else:
        q = st.text_input("🔍 レース名・競馬場で検索", key="race_search")
        view = races
        if q:
            view = races[races["name"].str.contains(q, case=False, na=False) |
                         races["course"].str.contains(q, case=False, na=False)]
        st.subheader("登録済みのレース")
        st.caption(f"{len(view)} 件表示")
        st.dataframe(jp(view.sort_values("date", ascending=False)),
                     use_container_width=True, hide_index=True)

        st.divider()

        # 2. レースを選んで「結果」を細かく見る + 手編集
        st.subheader("🏆 レースの結果を見る・編集する")
        race_opt = {f'{r["date"]} {r["course"]} {r["name"]}': r["id"]
                    for _, r in view.sort_values("date", ascending=False).iterrows()}
        sel = st.selectbox("レースを選ぶ", [""] + list(race_opt.keys()), key="race_result_sel")
        if sel:
            rid = race_opt[sel]
            race_row = races[races["id"] == rid].iloc[0]
            st.caption(f'{race_row["surface"]}{race_row["distance"]}m'
                       f'（{categorize_distance(race_row["distance"])}） '
                       f'/ {race_row["turn"]}回り / 馬場:{race_row["track_condition"]} '
                       f'/ 天気:{race_row["weather"]} / クラス:{race_row.get("class","") or "未設定"}')

            conn = get_connection()
            df = pd.read_sql(f"""
                SELECT r.finish AS 着順, e.frame AS 枠, e.number AS 馬番,
                       h.name AS 馬名, j.name AS 騎手,
                       r.time AS タイム, r.margin AS 着差,
                       r.agari AS 上がり, r.corner AS 通過順,
                       e.popularity AS 人気, r.body_weight AS 馬体重,
                       r.trainer AS 調教師, r.hit AS 的中
                FROM entries e
                LEFT JOIN horses h ON e.horse_id=h.id
                LEFT JOIN jockeys j ON e.jockey_id=j.id
                LEFT JOIN results r ON e.id=r.entry_id
                WHERE e.race_id={PH}
                ORDER BY CASE WHEN r.finish IS NULL THEN 999 ELSE r.finish END""",
                conn._conn, params=(rid,))
            conn.close()

            if len(df) == 0:
                st.info("このレースの出走・結果データがありません。"
                        "「📥 取り込み」で結果を取り込んでください。")
            else:
                st.dataframe(df, use_container_width=True, hide_index=True)

                # ===== コーナー通過順まとめ(全馬を横並び) =====
                corner_rows = []
                max_corners = 0
                for _, row in df.iterrows():
                    corner_str = str(row.get("通過順") or "").strip()
                    # スペースまたはハイフン区切りの数字を取り出す
                    nums = re.findall(r"\d+", corner_str)
                    max_corners = max(max_corners, len(nums))
                    corner_rows.append((row.get("馬番"), row.get("馬名"),
                                        row.get("着順"), nums))
                if max_corners > 0:
                    with st.expander("🏁 コーナー通過順まとめ(全馬・展開を見る)", expanded=True):
                        st.caption("各コーナーでの順位を、全馬まとめて表示。"
                                   "数字が小さい=前、大きい=後ろ。右ほどゴールに近い。"
                                   "コーナー数が少ない馬は右詰め(最終コーナーを揃える)。")
                        # コーナー名(最後が4角になるように右詰め)
                        corner_labels_all = ["1角", "2角", "3角", "4角"]
                        if max_corners <= 4:
                            labels = corner_labels_all[4 - max_corners:]
                        else:
                            labels = [f"{i+1}角" for i in range(max_corners)]
                        table = []
                        for num, name, finish, nums in corner_rows:
                            # 右詰め: 足りない分は左を空欄に
                            padded = [""] * (max_corners - len(nums)) + nums
                            d = {"着順": finish, "馬番": num, "馬名": name}
                            for lab, val in zip(labels, padded):
                                d[lab] = val
                            table.append(d)
                        cdf = pd.DataFrame(table).sort_values(
                            "着順", na_position="last").reset_index(drop=True)
                        st.dataframe(cdf, use_container_width=True, hide_index=True)

                # ===== 結果の手編集 =====
                with st.expander("✏️ 結果を手で修正・追記する"):
                    st.caption("1頭ずつ選んで、着順・タイム・着差・上がり・通過順・馬体重・調教師を直せます。")
                    # 出走馬の一覧(entry_idと馬名)
                    conn = get_connection()
                    ent_rows = conn.execute(f"""
                        SELECT e.id AS entry_id, e.number AS num, h.name AS hname
                        FROM entries e LEFT JOIN horses h ON e.horse_id=h.id
                        WHERE e.race_id={PH} ORDER BY e.number""", (rid,)).fetchall()
                    conn.close()
                    edit_opt = {f'{r["num"]}番 {r["hname"]}': r["entry_id"] for r in ent_rows}
                    edit_sel = st.selectbox("編集する馬", [""] + list(edit_opt.keys()),
                                            key="edit_horse_sel")
                    if edit_sel:
                        eid = edit_opt[edit_sel]
                        # 現在の結果を取得
                        conn = get_connection()
                        cur_row = conn.execute(
                            f"SELECT * FROM results WHERE entry_id={PH}", (eid,)).fetchone()
                        conn.close()
                        cur = dict(cur_row) if cur_row else {}

                        with st.form("edit_result_form"):
                            c1, c2, c3 = st.columns(3)
                            ef_finish = c1.text_input("着順", str(cur.get("finish") or ""))
                            ef_time = c2.text_input("タイム", cur.get("time") or "")
                            ef_margin = c3.text_input("着差", cur.get("margin") or "")
                            c4, c5, c6 = st.columns(3)
                            ef_agari = c4.text_input("上がり", cur.get("agari") or "")
                            ef_corner = c5.text_input("通過順(例 3-3-2-1)", cur.get("corner") or "")
                            ef_bw = c6.text_input("馬体重", cur.get("body_weight") or "")
                            ef_trainer = st.text_input("調教師", cur.get("trainer") or "")
                            if st.form_submit_button("💾 保存する"):
                                # finishは数字に変換(空ならNULL)
                                try:
                                    fin_val = int(ef_finish) if ef_finish.strip() else None
                                except ValueError:
                                    fin_val = None
                                hit_val = 1 if (fin_val is not None and fin_val <= 3) else 0
                                if cur_row:
                                    # 既存の結果を更新
                                    run_sql(f"""UPDATE results SET finish={PH}, time={PH},
                                               margin={PH}, agari={PH}, corner={PH},
                                               body_weight={PH}, trainer={PH}, hit={PH}
                                               WHERE entry_id={PH}""",
                                            (fin_val, ef_time, ef_margin, ef_agari, ef_corner,
                                             ef_bw, ef_trainer, hit_val, eid))
                                else:
                                    # 結果が無ければ新規作成
                                    run_sql(f"""INSERT INTO results
                                               (race_id, entry_id, finish, hit, time, margin,
                                                agari, corner, body_weight, trainer)
                                               VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH})""",
                                            (rid, eid, fin_val, hit_val, ef_time, ef_margin,
                                             ef_agari, ef_corner, ef_bw, ef_trainer))
                                st.success("保存しました。"); st.rerun()

        # レース削除
        with st.expander("🗑 レースを削除"):
            del_opt = {f'{r["date"]} {r["name"]}': r["id"] for _, r in view.iterrows()}
            del_sel = st.selectbox("削除するレース", [""] + list(del_opt.keys()), key="del_race")
            if del_sel and st.button("削除する", key="del_race_btn"):
                rid = del_opt[del_sel]
                run_sql("DELETE FROM races WHERE id=?", (rid,))
                run_sql("DELETE FROM entries WHERE race_id=?", (rid,))
                run_sql("DELETE FROM results WHERE race_id=?", (rid,))
                st.success("削除しました(出走・結果も一緒に削除)。"); st.rerun()

    # 3. 追加フォーム(下)
    st.divider()
    with st.expander("➕ レースを手動で追加"):
        st.caption("※ 通常はレース結果ページのコピペ取り込みが便利です(📋 結果取り込み)")
        with st.form("add_race"):
            col1, col2 = st.columns(2)
            with col1:
                r_id = st.text_input("レースID *", placeholder="例：R001")
                r_date = st.date_input("日付")
                r_name = st.text_input("レース名 *")
                r_course = st.selectbox("競馬場",
                    ["", "東京", "中山", "京都", "阪神", "中京", "札幌", "函館", "福島", "新潟", "小倉"])
            with col2:
                r_surface = st.selectbox("芝/ダート", ["芝", "ダート"])
                r_distance = st.number_input("距離(m)", 800, 4000, 1600, step=100)
                r_turn = st.selectbox("左右回り", ["", "右", "左", "直線"])
                r_track = st.selectbox("馬場", ["良", "稍重", "重", "不良"])
                r_weather = st.selectbox("天気", ["晴", "曇", "雨", "雪"])
            if st.form_submit_button("保存"):
                if r_id and r_name:
                    run_sql("""INSERT OR REPLACE INTO races
                               (id,date,name,course,surface,distance,turn,track_condition,weather)
                               VALUES (?,?,?,?,?,?,?,?,?)""",
                            (r_id, str(r_date), r_name, r_course, r_surface,
                             r_distance, r_turn, r_track, r_weather))
                    st.success(f"「{r_name}」を保存しました。距離区分：{categorize_distance(r_distance)}")
                    st.rerun()
                else:
                    st.error("レースIDとレース名は必須です。")


# ------------------------------------------------------------
# 予想・ランキング
# ------------------------------------------------------------
elif menu == "🎯 予想":
    st.header("🎯 予想")
    races = load_table("races"); horses = load_table("horses"); jockeys = load_table("jockeys")

    if len(races) == 0:
        st.info("先に「📅 レース」でレースを登録してください。")
    elif len(horses) == 0 or len(jockeys) == 0:
        st.info("先に「🐎 馬」「👤 騎手」を登録してください。")
    else:
        race_opt = {f'{r["date"]} {r["course"]} {r["name"]}': r["id"] for _, r in races.iterrows()}
        choice = st.selectbox("対象レースを選ぶ", list(race_opt.keys()))
        race_id = race_opt[choice]
        race = races[races["id"] == race_id].iloc[0]
        st.caption(f'{race["surface"]}{race["distance"]}m（{categorize_distance(race["distance"])}） '
                   f'/ {race["turn"]}回り / 馬場:{race["track_condition"]} / 天気:{race["weather"]}')

        # ===== 重み調整(折りたたみ。ここで変えるとその場で予想に反映) =====
        with st.expander("⚖️ 予想点の重み調整(その場で反映)"):
            st.caption("各スコアを偏差値化してから、この重みで合計します。0.1刻み。")
            rw = load_race_weights()
            rw_zenso = st.slider("📊 前走スコア (%)", 0.0, 100.0, rw["zenso"], 0.1, key="yw_zenso")
            rw_speed = st.slider("⚡ 速さスコア (%)", 0.0, 100.0, rw["speed"], 0.1, key="yw_speed")
            rw_fit = st.slider("🎯 条件適性スコア (%)", 0.0, 100.0, rw["fit"], 0.1, key="yw_fit")
            rw_klass = st.slider("⭐ クラススコア (%)", 0.0, 100.0, rw["klass"], 0.1, key="yw_klass")
            rw_jockey = st.slider("👤 騎手スコア (%)", 0.0, 100.0, rw["jockey"], 0.1, key="yw_jockey")
            rtotal = rw_zenso + rw_speed + rw_fit + rw_klass + rw_jockey
            st.caption(f"合計: {rtotal:.1f}%(100%でなくても自動正規化)")
            cbtn1, cbtn2 = st.columns(2)
            if cbtn1.button("💾 重みを保存して反映", key="save_yosou_w"):
                save_race_weights({"zenso": rw_zenso, "speed": rw_speed, "fit": rw_fit,
                                   "klass": rw_klass, "jockey": rw_jockey})
                st.success("保存しました。下のランキングに反映されます。"); st.rerun()
            if cbtn2.button("🔄 初期値に戻す", key="reset_yosou_w"):
                save_race_weights(DEFAULT_RACE_WEIGHTS)
                st.success("初期値に戻しました。"); st.rerun()

        with st.expander("➕ 出走馬を追加する（馬と騎手を選ぶだけ）"):
            with st.form("add_entry"):
                horse_opt = {f'{h["name"]}（{h["id"]}）': h["id"] for _, h in horses.iterrows()}
                jockey_opt = {f'{j["name"]}（{j["id"]}）': j["id"] for _, j in jockeys.iterrows()}
                e_horse = st.selectbox("馬", list(horse_opt.keys()))
                e_jockey = st.selectbox("騎手", list(jockey_opt.keys()))
                cc1, cc2, cc3 = st.columns(3)
                e_frame = cc1.number_input("枠番", 1, 8, 1)
                e_number = cc2.number_input("馬番", 1, 18, 1)
                e_weight = cc3.number_input("斤量", 40.0, 65.0, 55.0, step=0.5)
                cc4, cc5 = st.columns(2)
                e_odds = cc4.number_input("オッズ(任意)", 0.0, 999.0, 0.0, step=0.1)
                e_pop = cc5.number_input("人気(任意)", 0, 18, 0)
                if st.form_submit_button("出走馬を追加"):
                    run_sql("""INSERT INTO entries (race_id,horse_id,jockey_id,frame,number,weight,odds,popularity)
                               VALUES (?,?,?,?,?,?,?,?)""",
                            (race_id, horse_opt[e_horse], jockey_opt[e_jockey], e_frame, e_number,
                             e_weight, e_odds if e_odds > 0 else None, e_pop if e_pop > 0 else None))
                    st.success("追加しました。"); st.rerun()

        conn = get_connection()
        entries = pd.read_sql(f"SELECT * FROM entries WHERE race_id={PH}", conn._conn, params=(race_id,))
        conn.close()

        if len(entries) == 0:
            st.info("このレースにはまだ出走馬がいません。上の「➕」から追加してください。")
        else:
            # ===== 予想点ランキング(設計版: コア5スコア→偏差値→重みづけ) =====
            st.subheader("🎯 予想点ランキング")
            st.caption("過去成績から算出。前走/速さ/条件適性/クラス/騎手の各スコアを偏差値化し、"
                       "重みづけして合計。人気・オッズは参考表示のみ。")
            yosou = calculate_race_scores(race_id)
            if yosou:
                table = []
                for i, h in enumerate(yosou, 1):
                    d = h["_detail"]
                    table.append({
                        "順位": i, "馬番": h["馬番"], "馬名": h["馬名"], "騎手": h["騎手"],
                        "予想点": h["予想点"], "偏差値": h["予想偏差値"],
                        "前走": d["zenso"], "速さ": d["speed"], "適性": d["fit"],
                        "クラス": d["klass"], "騎手力": d["jockey"],
                        "人気": h["人気"], "オッズ": h["オッズ"],
                    })
                ydf = pd.DataFrame(table)
                st.dataframe(ydf, use_container_width=True, hide_index=True)
                top = yosou[0]
                st.success(f'◎ 予想本命：{top["馬名"]}（{top["騎手"]}） '
                           f'予想点 {top["予想点"]} / 偏差値 {top["予想偏差値"]}')
                st.caption("※ 各列の数字は偏差値(50が平均)。Noneは該当データなし。")

            st.divider()

            # ===== 出走馬の近走成績(取り込み済みデータから) =====
            st.subheader("📖 出走馬の近走成績")
            st.caption("取り込み済みの過去レースから、各馬の成績を新しい順に表示します。"
                       "データがある馬だけ出ます(予想点には影響しません)。")
            before_date = race["date"] if "date" in race.index else None
            # 馬番順に出走馬を取得
            entries_sorted = entries.sort_values("number")
            any_data = False
            for _, e in entries_sorted.iterrows():
                hr = horses[horses["id"] == e["horse_id"]]
                horse_name = hr.iloc[0]["name"] if len(hr) > 0 else "?"
                recent = get_horse_recent_races(e["horse_id"], before_date=before_date)
                with st.expander(f'{int(e["number"])}番 {horse_name}'
                                 f'（近走 {len(recent)} 戦）'):
                    if recent:
                        any_data = True
                        rdf = pd.DataFrame(recent)
                        st.dataframe(rdf, use_container_width=True, hide_index=True)
                    else:
                        st.caption("取り込み済みデータがありません。"
                                   "「📋 結果取り込み」で過去レースを入れると表示されます。")
            if not any_data:
                st.info("まだ近走データがありません。過去レースを結果取り込みすると、"
                        "ここに各馬の成績が出てきます。")

            st.divider()

            # ===== 旧・総合点ランキング(手動/自動評価ベース) =====
            with st.expander("📊 旧・総合点ランキング(馬評価×騎手評価+適性ボーナス)"):
                ranking = []
                for _, e in entries.iterrows():
                    hr = horses[horses["id"] == e["horse_id"]]
                    jr = jockeys[jockeys["id"] == e["jockey_id"]]
                    if len(hr) == 0 or len(jr) == 0:
                        continue
                    horse = hr.iloc[0]; jockey = jr.iloc[0]
                    score, bonuses = calculate_score(horse, jockey, race)
                    ranking.append({
                        "馬番": e["number"], "馬名": horse["name"], "騎手": jockey["name"],
                        "脚質": horse["running_style"], "オッズ": e["odds"], "人気": e["popularity"],
                        "総合点": score, "加点内訳": " ".join(bonuses),
                        "_entry_id": e["id"],
                    })
                rank_df = pd.DataFrame(ranking).sort_values("総合点", ascending=False).reset_index(drop=True)
                rank_df.insert(0, "順位", range(1, len(rank_df) + 1))
                show = rank_df.drop(columns=["_entry_id"])
                st.dataframe(show, use_container_width=True, hide_index=True)

            # 出走馬の削除
            with st.expander("🗑 出走馬を削除する"):
                conn = get_connection()
                ent2 = pd.read_sql(f"SELECT e.id, e.number, h.name FROM entries e "
                                   f"LEFT JOIN horses h ON e.horse_id=h.id "
                                   f"WHERE e.race_id={PH}", conn._conn, params=(race_id,))
                conn.close()
                d_opt = {f'{r["number"]} {r["name"]}': r["id"] for _, r in ent2.iterrows()}
                d_sel = st.selectbox("削除する出走馬", [""] + list(d_opt.keys()))
                if d_sel and st.button("削除"):
                    run_sql("DELETE FROM entries WHERE id=?", (int(d_opt[d_sel]),))
                    st.success("削除しました。"); st.rerun()


# ------------------------------------------------------------
# ランキング (器だけ。中身は後で作る)
# ------------------------------------------------------------
elif menu == "📈 ランキング":
    st.header("📈 ランキング")
    st.info("条件を指定して、馬・騎手のランキングを見る機能です(これから作ります)。")
    st.markdown("""
    **作る予定のランキング:**
    - 🐎 馬の勝率・複勝率ランキング
    - 👤 騎手の勝率・複勝率ランキング
    - ⏱ 「2400mの最速タイム」など条件別ランキング
    - 好みの条件でフィルタして表示

    まずはデータを溜めて、必要になったら中身を作りましょう。
    """)
    # 簡易版: 今あるデータで馬・騎手の勝率だけ出しておく
    tab1, tab2 = st.tabs(["🐎 馬の勝率", "👤 騎手の勝率"])
    with tab1:
        conn = get_connection()
        rows = conn.execute("""
            SELECT h.name AS 馬名, COUNT(*) AS 出走,
                   SUM(CASE WHEN r.finish=1 THEN 1 ELSE 0 END) AS 勝利,
                   SUM(CASE WHEN r.finish<=3 THEN 1 ELSE 0 END) AS 複勝
            FROM results r JOIN entries e ON r.entry_id=e.id
            JOIN horses h ON e.horse_id=h.id
            WHERE r.finish IS NOT NULL
            GROUP BY h.id ORDER BY 勝利 DESC, 複勝 DESC
        """).fetchall()
        conn.close()
        if rows:
            df = pd.DataFrame([dict(r) for r in rows])
            df["勝率"] = (df["勝利"] / df["出走"] * 100).round(0).astype(int).astype(str) + "%"
            df["複勝率"] = (df["複勝"] / df["出走"] * 100).round(0).astype(int).astype(str) + "%"
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("まだ結果データがありません。")
    with tab2:
        conn = get_connection()
        rows = conn.execute("""
            SELECT j.name AS 騎手, COUNT(*) AS 騎乗,
                   SUM(CASE WHEN r.finish=1 THEN 1 ELSE 0 END) AS 勝利,
                   SUM(CASE WHEN r.finish<=3 THEN 1 ELSE 0 END) AS 複勝
            FROM results r JOIN entries e ON r.entry_id=e.id
            JOIN jockeys j ON e.jockey_id=j.id
            WHERE r.finish IS NOT NULL
            GROUP BY j.id ORDER BY 勝利 DESC, 複勝 DESC
        """).fetchall()
        conn.close()
        if rows:
            df = pd.DataFrame([dict(r) for r in rows])
            df["勝率"] = (df["勝利"] / df["騎乗"] * 100).round(0).astype(int).astype(str) + "%"
            df["複勝率"] = (df["複勝"] / df["騎乗"] * 100).round(0).astype(int).astype(str) + "%"
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("まだ結果データがありません。")


# ------------------------------------------------------------
# 結果
# ------------------------------------------------------------
elif menu == "📥 取り込み":
    st.header("📥 取り込み")
    st.caption("出馬表(これから走るレース)と、結果(終わったレース)を取り込みます。"
               "下のタブで切り替えてください。")

    tab_shutuba, tab_result = st.tabs(["📝 出馬表(予想用)", "🏆 結果(終わったレース)"])

    # ===== タブ1: 出馬表取り込み =====
    with tab_shutuba:
        st.markdown("""
        **これから走るレースの出馬表**を取り込んで、予想できるようにします。

        1. JRA公式の「**馬体重・オッズ入り出馬表**」または「**出馬表**」ページを開く
        2. ページ全体をコピー (Ctrl+A → Ctrl+C)
        3. 下に貼り付け (Ctrl+V) → 「読み取る」→ 確認 → 「取り込む」

        取り込んだら「🎯 予想」でそのレースを選ぶと予想点が出ます。
        """)

        if st.session_state.get("clear_shutuba_text", False):
            st.session_state["shutuba_text_area"] = ""
            st.session_state["clear_shutuba_text"] = False

        text_input_s = st.text_area("出馬表テキストを貼り付け", height=250,
                                    key="shutuba_text_area",
                                    placeholder="JRAの出馬表ページから、Ctrl+A → Ctrl+C → ここに貼り付け")

        if st.button("🔍 読み取る(確認)", type="primary", key="read_shutuba"):
            if not text_input_s.strip():
                st.session_state["shutuba_parsed"] = None
                st.error("テキストが空です。貼り付けてください。")
            else:
                with st.spinner("解析中..."):
                    parsed = parse_jra_shutuba(text_input_s)
                st.session_state["shutuba_parsed"] = parsed
                st.session_state["shutuba_raw"] = text_input_s

        parsed = st.session_state.get("shutuba_parsed")
        if parsed:
            if not parsed["race"].get("name"):
                st.warning("⚠️ レース情報が読み取れませんでした。出馬表全体を貼ったか確認してください。")
            elif not parsed["entries"]:
                st.warning("⚠️ 出走馬が読み取れませんでした。")
            else:
                n = len(parsed["entries"])
                st.subheader(f"📋 読み取り結果: {n}頭")
                race = parsed["race"]
                st.write(f"**レース:** {race.get('name','?')} "
                         f"／ {race.get('date','?')} {race.get('course','')} "
                         f"{race.get('surface','')}{race.get('distance','')}m")
                st.dataframe(pd.DataFrame(parsed["entries"])[
                    ["枠", "馬番", "馬名", "性別", "年齢", "斤量", "騎手", "オッズ", "人気"]
                ], use_container_width=True, hide_index=True)
                st.warning(f"⚠️ **{n}頭** 読み取れました。"
                           f"本来の出走頭数と合っているか、上の一覧で確認してください。\n\n"
                           f"足りない場合は、出馬表をコピーし直して「読み取る」をやり直してください。")
                col_ok, col_no = st.columns(2)
                if col_ok.button("✅ この内容で取り込む", type="primary", key="do_shutuba"):
                    ok, msg = import_shutuba_from_text(st.session_state.get("shutuba_raw", ""))
                    if ok:
                        st.success(msg)
                        st.session_state["shutuba_parsed"] = None
                        st.session_state["clear_shutuba_text"] = True
                    else:
                        st.error(msg)
                if col_no.button("❌ やめる", key="cancel_shutuba"):
                    st.session_state["shutuba_parsed"] = None
                    st.info("取り込みをキャンセルしました。")
                    st.rerun()

    # ===== タブ2: 結果取り込み =====
    with tab_result:
        st.markdown("""
        **終わったレースの結果**を取り込みます。データが溜まるほど予想が賢くなります。

        1. JRA公式の「**レース結果ページ**」を開く
        2. ページ全体をコピー (Ctrl+A → Ctrl+C)
        3. 下に貼り付け (Ctrl+V) → 「読み取る」→ 確認 → 「取り込む」
        """)

        if st.session_state.get("clear_import_text", False):
            st.session_state["import_text_area"] = ""
            st.session_state["clear_import_text"] = False

        text_input_r = st.text_area("結果テキストを貼り付け", height=250,
                                    key="import_text_area",
                                    placeholder="JRAのレース結果ページから、Ctrl+A → Ctrl+C → ここに貼り付け")

        col_a, col_b = st.columns([1, 4])
        do_read = col_a.button("🔍 読み取る(確認)", type="primary", key="read_result")
        do_clear = col_b.button("🧹 クリア", key="clear_result")

        if do_clear:
            st.session_state["result_parsed"] = None
            st.session_state["clear_import_text"] = True
            st.rerun()

        if do_read:
            if not text_input_r.strip():
                st.session_state["result_parsed"] = None
                st.error("テキストが空です。貼り付けてください。")
            else:
                with st.spinner("解析中..."):
                    parsed = parse_jra_full(text_input_r)
                st.session_state["result_parsed"] = parsed
                st.session_state["result_raw"] = text_input_r

        parsed = st.session_state.get("result_parsed")
        if parsed:
            if not parsed["race"].get("name"):
                st.warning("⚠️ レース情報が読み取れませんでした。テキスト全体を貼ったか確認してください。")
            elif not parsed["results"]:
                st.warning("⚠️ 出走馬の結果が読み取れませんでした。")
            else:
                n = len(parsed["results"])
                st.subheader(f"📋 読み取り結果: {n}頭")
                race = parsed["race"]
                st.write(f"**レース:** {race.get('name','?')} "
                         f"／ {race.get('date','?')} {race.get('course','')} "
                         f"{race.get('surface','')}{race.get('distance','')}m")
                st.dataframe(pd.DataFrame(parsed["results"]),
                             use_container_width=True, hide_index=True)
                st.warning(f"⚠️ **{n}頭** 読み取れました。"
                           f"本来の出走頭数と合っているか、上の一覧で確認してください。\n\n"
                           f"足りない場合は、結果ページをコピーし直して「読み取る」をやり直してください。")
                col_ok, col_no = st.columns(2)
                if col_ok.button("✅ この内容で取り込む", type="primary", key="do_result"):
                    ok, msg = import_race_from_text(st.session_state.get("result_raw", ""))
                    if ok:
                        st.success(msg)
                        st.session_state["result_parsed"] = None
                        st.session_state["clear_import_text"] = True
                        st.info("👇 次のレースを貼り付けて、また「読み取る」を押してください")
                    else:
                        st.error(msg)
                if col_no.button("❌ やめる", key="cancel_result"):
                    st.session_state["result_parsed"] = None
                    st.info("取り込みをキャンセルしました。")
                    st.rerun()


# ------------------------------------------------------------
# 収支
# ------------------------------------------------------------
elif menu == "💰 収支":
    st.header("馬券収支")
    races = load_table("races")
    race_opt = {"": ""}
    for _, r in races.iterrows():
        race_opt[f'{r["date"]} {r["name"]}'] = r["id"]
    with st.form("add_bet"):
        st.subheader("購入記録を追加")
        col1, col2 = st.columns(2)
        with col1:
            b_date = st.date_input("日付")
            b_race = st.selectbox("レース", list(race_opt.keys()))
            b_type = st.selectbox("券種", ["単勝","複勝","馬連","馬単","ワイド","三連複","三連単","枠連"])
        with col2:
            b_sel = st.text_input("買い目", placeholder="例：5 や 3-7")
            b_amount = st.number_input("購入金額(円)", 0, 1000000, 1000, step=100)
            b_payout = st.number_input("払戻(円)", 0, 10000000, 0, step=100)
        b_hit = st.checkbox("的中")
        b_memo = st.text_input("メモ")
        if st.form_submit_button("保存"):
            run_sql("""INSERT INTO bets (date,race_id,bet_type,selection,amount,payout,hit,memo)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (str(b_date), race_opt[b_race], b_type, b_sel, b_amount, b_payout,
                     1 if b_hit else 0, b_memo))
            st.success("保存しました。")
    st.divider()
    bets = load_table("bets")
    if len(bets) == 0:
        st.info("まだ購入記録がありません。")
    else:
        spent = bets["amount"].sum(); payout = bets["payout"].sum()
        profit = payout - spent; roi = (payout / spent * 100) if spent > 0 else 0
        hits = bets["hit"].sum(); hit_rate = (hits / len(bets) * 100) if len(bets) else 0
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("投資", f"{spent:,}円"); c2.metric("払戻", f"{payout:,}円")
        c3.metric("収支", f"{profit:+,}円"); c4.metric("回収率", f"{roi:.1f}%", f"的中率 {hit_rate:.1f}%")
        st.dataframe(bets.sort_values("date", ascending=False), use_container_width=True, hide_index=True)


# ------------------------------------------------------------
# 設定 (自動評価の重み調整)
# ------------------------------------------------------------
elif menu == "⚙️ 設定":
    st.header("⚙️ 自動評価の計算式")
    st.markdown("""
    各要素の **重みづけ(%)** を変えることで、自動評価のクセを変えられます。

    - **大きく(40~50%)** すると、その要素を重視する馬が高評価になる
    - **小さく(0~10%)** すると、その要素は予想にあまり影響しなくなる
    - 合計が100%でなくても動きます(自動で正規化されます)
    """)

    # 現在の値を読み込み
    current = load_weights()

    st.divider()
    st.subheader("重みを調整(0.1刻み)")

    w_win = st.slider("🏆 勝率・複勝率の重み (%)",
                      min_value=0.0, max_value=100.0,
                      value=current["win_rate"], step=0.1,
                      help="過去の勝率と複勝率からの評価")
    w_fit = st.slider("🎯 距離・馬場・コース適性の重み (%)",
                      min_value=0.0, max_value=100.0,
                      value=current["fit"], step=0.1,
                      help="今回の条件と過去のコースが一致した馬の評価")
    w_class = st.slider("⭐ G1・重賞での実績の重み (%)",
                        min_value=0.0, max_value=100.0,
                        value=current["class"], step=0.1,
                        help="高いクラス(G1/G2/G3)で好走した馬の評価")
    w_recent = st.slider("🔥 直近の調子の重み (%)",
                         min_value=0.0, max_value=100.0,
                         value=current["recent"], step=0.1,
                         help="直近3走の着順から見た調子")

    # 合計を確認表示
    total = w_win + w_fit + w_class + w_recent
    if abs(total - 100.0) < 0.05:
        st.success(f"✅ 合計: {total:.1f}% (ちょうど100%)")
    else:
        st.info(f"📊 合計: {total:.1f}% (100%でなくても動きます。自動で正規化されます)")

    st.divider()

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        if st.button("💾 保存して全馬を再計算", type="primary"):
            new_weights = {
                "win_rate": w_win, "fit": w_fit,
                "class": w_class, "recent": w_recent
            }
            save_weights(new_weights)
            with st.spinner("再計算中..."):
                n = update_all_auto_ratings(new_weights)
            st.success(f"✅ 重みを保存し、{n}頭の自動評価を更新しました。"
                       f"「🐎 馬」「🎯 予想・ランキング」を見てください。")
    with col2:
        if st.button("🔄 初期値に戻す"):
            save_weights(DEFAULT_WEIGHTS)
            with st.spinner("初期値で再計算中..."):
                n = update_all_auto_ratings(DEFAULT_WEIGHTS)
            st.success(f"初期値に戻し、{n}頭の評価を再計算しました。")
            st.rerun()
    with col3:
        st.caption("初期値:\n勝率30 / 適性25 / クラス25 / 直近20")

    # 現在の各要素ごとのスコア計算式の説明
    st.divider()
    with st.expander("📖 各要素のスコアの計算方法(詳細)"):
        st.markdown("""
        **🏆 勝率・複勝率スコア**
        - 出走回数に対する勝利数と3着以内回数から計算
        - 計算式: `勝率×200 + 複勝率×50`(最大100にスケーリング)

        **🎯 距離・馬場・コース適性スコア**
        - 過去のレースで「今回と同じ条件」がどれだけあったかを見る
        - 一致した要素ごとに加点: 距離区分25 + 芝ダ35 + 回り20 + 馬場20
        - 着順で重みづけ: 3着以内×1.0 / 5着以内×0.5 / 6着以下×0.2

        **⭐ G1・重賞での実績スコア**
        - クラス別の基準点: G1=100 / G2=80 / G3=65 / L=55 / OP=50 / 3勝=40 / 2勝=30 / 1勝=20 / 未勝利=10 / 新馬=10
        - 着順別の倍率: 1着=1.0 / 3着以内=0.6 / 5着以内=0.3 / 6着以下=0.1
        - 出走数で平均

        **🔥 直近の調子スコア**
        - 直近3走の着順から: 1着=100 / 2着=80 / 3着=65 / 5着以内=45 / 8着以内=25 / それ以下=10
        - 3走の平均

        **総合点(最終評価)**
        - 各スコアに重みを掛けて合計し、合計重みで割って正規化
        - 結果は0〜100の範囲
        """)

    # ========== 予想点の詳細設定(コア5重みは「🎯 予想」画面で調整) ==========
    st.divider()
    st.header("🎯 予想点の詳細設定")
    st.info("コア5スコアの重み(前走・速さ・適性・クラス・騎手)は「🎯 予想」画面で"
            "その場で調整できます。ここでは細かい内訳を設定します。")

    st.subheader("騎手スコアの内訳(0.1刻み)")
    jw = load_jockey_sub_weights()
    jw_winrate = st.slider("　通算勝率・複勝率 (%)", 0.0, 100.0, jw["winrate"], 0.1)
    jw_combi = st.slider("　馬とのコンビ成績 (%)", 0.0, 100.0, jw["combi"], 0.1)
    jw_cond = st.slider("　今回条件での成績 (%)", 0.0, 100.0, jw["cond"], 0.1)
    jw_recent = st.slider("　直近の調子 (%)", 0.0, 100.0, jw["recent"], 0.1,
                          help="初期値0。使いたければ上げる")

    st.subheader("データがないスコアの扱い")
    mode_label = {"zero": "0点として計算(実績ない馬は不利)",
                  "median": "中央値50として計算(平均的とみなす)",
                  "exclude": "除外(あるスコアだけで計算)"}
    current_mode = get_setting("missing_mode", "zero")
    mode_choice = st.radio("データがない項目をどう扱う?",
                           options=list(mode_label.keys()),
                           format_func=lambda k: mode_label[k],
                           index=list(mode_label.keys()).index(current_mode))

    if st.button("💾 予想点の詳細設定を保存", type="primary"):
        save_jockey_sub_weights({"winrate": jw_winrate, "combi": jw_combi,
                                 "cond": jw_cond, "recent": jw_recent})
        set_setting("missing_mode", mode_choice)
        st.success("✅ 保存しました。「🎯 予想」画面で反映されます。")

    if st.button("🔄 予想点の詳細設定を初期値に戻す"):
        save_jockey_sub_weights(DEFAULT_JOCKEY_SUB_WEIGHTS)
        set_setting("missing_mode", "zero")
        st.success("初期値に戻しました。")
        st.rerun()
