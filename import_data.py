"""
One-time script to import all 5 tournament CSV files into parkgolf.db.
Run from the parkgolf/ directory: python3 import_data.py
Safe to re-run — deletes existing data and reimports fresh.
"""
import csv
import os
import re
import sqlite3
from datetime import datetime

DB = os.path.join(os.path.dirname(__file__), 'parkgolf.db')
SCHEMA = os.path.join(os.path.dirname(__file__), 'schema.sql')
DATA_DIR = os.path.join(os.path.dirname(__file__), '..')  # /Desktop/claudeCode/

COURSES = {
    'destroyer park golf': ('Destroyer Park Golf', 46),
    'wormburner park golf': ('Wormburner Park Golf', 49),
}

TOURNAMENTS = [
    {
        'file': 'inter.csv',
        'num_rounds': 4,
        'date': 'July 12-13, 2025',   # per-row dates have wrong years
    },
    {
        'file': 'ospgc.csv',
        'num_rounds': 2,
        'date': None,   # read from first valid row
    },
    {
        'file': 'wo.csv',
        'num_rounds': 2,
        'date': 'September 13, 2025',  # use first men's entry date; women rows have wrong years
    },
    {
        'file': 'na.csv',
        'num_rounds': 2,
        'date': None,
    },
    {
        'file': 'Flex.csv',
        'num_rounds': 1,
        'date': None,
    },
]

# ── Name normalization: canonical spellings ──────────────────────────────────
NAME_MAP = {
    'hobie shaw':       'Hobart Shaw',
    'dave grzechowiak': 'David Grzechowiak',
    'phil kosobuki':    'Phil Kosobucki',
    'michael meadows':  'Mike Meadows',
    'craig henman':     'Craig Hemann',
    'sharon cappasso':  'Sharon Capasso',
    'nancy vitiate':    'Nancy Vititoe',
}

# ── Division assignment ───────────────────────────────────────────────────────
# First names that are exclusively women in this dataset
WOMEN_FIRST_NAMES = {
    'erin', 'courtney', 'christine', 'kathy', 'sharon', 'nancy', 'cheryl', 'harper',
    'tashina', 'heidi', 'heather', 'sandy', 'gabrielle', 'valerie',
    'stephanie', 'jessica', 'patricia',
}

# Full names (lowercase) that need explicit tagging (ambiguous first name)
WOMEN_FULL_NAMES = {
    'mel f',
    'kelly montgomery',
}


def get_division(name):
    lower = name.lower()
    first = lower.split()[0] if lower.split() else ''
    if first in WOMEN_FIRST_NAMES:
        return 'Women'
    if lower in WOMEN_FULL_NAMES:
        return 'Women'
    return 'Men'


def normalize_name(name):
    """Title-case and apply canonical spelling corrections."""
    normalized = name.strip().title()
    return NAME_MAP.get(normalized.lower(), normalized)


def parse_sort_date(date_str):
    """Convert display date strings to YYYY-MM-DD for chronological sorting."""
    date_str = re.sub(r'(\w+ \d+)-\d+,', r'\1,', date_str.strip())
    for fmt in ('%B %d, %Y', '%b %d, %Y', '%B %d %Y'):
        try:
            return datetime.strptime(date_str, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return date_str


def recalculate_ratings(db):
    """Recompute course scratches and per-round condition-adjusted ratings.
    effective_scratch = (course_scratch + round_avg_score) / 2
    """
    for course in db.execute('SELECT id, name FROM courses').fetchall():
        cid = course['id']
        avg_all = db.execute('''
            SELECT AVG(r.score) FROM rounds r
            JOIN tournaments t ON t.id = r.tournament_id WHERE t.course_id = ?
        ''', (cid,)).fetchone()[0]
        if avg_all is None:
            continue
        course_scratch = round(avg_all - 15, 1)
        db.execute('UPDATE courses SET course_rating = ? WHERE id = ?', (course_scratch, cid))

        round_groups = db.execute('''
            SELECT r.tournament_id, r.round_number, AVG(r.score) AS avg_round_score
            FROM rounds r
            JOIN tournaments t ON t.id = r.tournament_id
            WHERE t.course_id = ?
            GROUP BY r.tournament_id, r.round_number
        ''', (cid,)).fetchall()

        for grp in round_groups:
            eff_scratch = (course_scratch + grp['avg_round_score']) / 2
            for r in db.execute('''
                SELECT id, score FROM rounds
                WHERE tournament_id = ? AND round_number = ?
            ''', (grp['tournament_id'], grp['round_number'])).fetchall():
                db.execute('UPDATE rounds SET rating = ? WHERE id = ?',
                           (round(1000 + (eff_scratch - r['score']) * 10), r['id']))

        print(f"  {course['name']}: course scratch = {course_scratch}")


def get_or_create_player(db, name):
    division = get_division(name)
    row = db.execute('SELECT id FROM players WHERE LOWER(name) = LOWER(?)', (name,)).fetchone()
    if row:
        db.execute('UPDATE players SET division = ? WHERE id = ?', (division, row[0]))
        return row[0]
    db.execute('INSERT INTO players (name, division) VALUES (?, ?)', (name, division))
    return db.execute('SELECT last_insert_rowid()').fetchone()[0]


def get_or_create_course(db, raw_name):
    key = raw_name.strip().lower()
    if key not in COURSES:
        raise ValueError(f"Unknown course: {raw_name!r}")
    name, rating = COURSES[key]
    row = db.execute('SELECT id FROM courses WHERE LOWER(name) = LOWER(?)', (name,)).fetchone()
    if row:
        return row[0], rating
    db.execute('INSERT INTO courses (name, course_rating, initial_rating) VALUES (?, ?, ?)', (name, rating, rating))
    return db.execute('SELECT last_insert_rowid()').fetchone()[0], rating


def import_tournament(db, cfg):
    filepath = os.path.join(DATA_DIR, cfg['file'])
    with open(filepath, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print(f"  [SKIP] {cfg['file']} — empty")
        return

    def get(row, *keys):
        for k in keys:
            for rk in row:
                if rk.lower().strip() == k.lower():
                    v = row[rk]
                    return v.strip() if v else ''
        return ''

    first = rows[0]
    event_name = get(first, 'event')
    course_raw = get(first, 'course')
    date = cfg['date'] or get(first, 'date')
    sort_date = parse_sort_date(date) if date else None

    course_id, course_rating = get_or_create_course(db, course_raw)

    db.execute(
        'INSERT INTO tournaments (name, course_id, date, sort_date, num_rounds) VALUES (?, ?, ?, ?, ?)',
        (event_name, course_id, date, sort_date, cfg['num_rounds'])
    )
    tournament_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

    rounds_inserted = 0
    players_seen = set()

    for row in rows:
        name = normalize_name(get(row, 'name'))
        if not name:
            continue

        player_id = get_or_create_player(db, name)
        players_seen.add(player_id)

        for rn in range(1, cfg['num_rounds'] + 1):
            score_str = get(row, f'round {rn}')
            if not score_str:
                continue
            try:
                score = int(score_str)
            except ValueError:
                continue
            rating = 1000 + (course_rating - score) * 10
            db.execute(
                'INSERT INTO rounds (tournament_id, player_id, round_number, score, rating) VALUES (?, ?, ?, ?, ?)',
                (tournament_id, player_id, rn, score, rating)
            )
            rounds_inserted += 1

    print(f"  [OK] {event_name} — {len(players_seen)} players, {rounds_inserted} rounds")


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    with open(SCHEMA) as f:
        conn.executescript(f.read())

    # Migrations
    cols = [r[1] for r in conn.execute("PRAGMA table_info(courses)").fetchall()]
    if 'initial_rating' not in cols:
        conn.execute("ALTER TABLE courses ADD COLUMN initial_rating REAL")

    tcols = [r[1] for r in conn.execute("PRAGMA table_info(tournaments)").fetchall()]
    if 'sort_date' not in tcols:
        conn.execute("ALTER TABLE tournaments ADD COLUMN sort_date TEXT")

    pcols = [r[1] for r in conn.execute("PRAGMA table_info(players)").fetchall()]
    if 'division' not in pcols:
        conn.execute("ALTER TABLE players ADD COLUMN division TEXT DEFAULT 'Men'")

    # Seed courses
    for name, rating in COURSES.values():
        if not conn.execute('SELECT 1 FROM courses WHERE LOWER(name)=LOWER(?)', (name,)).fetchone():
            conn.execute('INSERT INTO courses (name, course_rating, initial_rating) VALUES (?, ?, ?)', (name, rating, rating))
    conn.execute("UPDATE courses SET initial_rating = 46 WHERE name = 'Destroyer Park Golf' AND initial_rating IS NULL")
    conn.execute("UPDATE courses SET initial_rating = 49 WHERE name = 'Wormburner Park Golf' AND initial_rating IS NULL")

    # Wipe existing data so we can reimport cleanly
    print("Clearing existing data...")
    conn.execute("DELETE FROM rounds")
    conn.execute("DELETE FROM players")
    conn.execute("DELETE FROM tournaments")
    conn.commit()

    print("Importing tournaments...")
    for cfg in TOURNAMENTS:
        import_tournament(conn, cfg)

    print("Recalculating dynamic course ratings...")
    recalculate_ratings(conn)
    conn.commit()
    conn.close()

    # Summary
    conn2 = sqlite3.connect(DB)
    p = conn2.execute('SELECT COUNT(*) FROM players').fetchone()[0]
    r = conn2.execute('SELECT COUNT(*) FROM rounds').fetchone()[0]
    t = conn2.execute('SELECT COUNT(*) FROM tournaments').fetchone()[0]
    w = conn2.execute("SELECT COUNT(*) FROM players WHERE division='Women'").fetchone()[0]
    conn2.close()
    print(f"\nDone! {t} tournaments, {p} players ({w} women), {r} rounds in database.")


if __name__ == '__main__':
    main()
