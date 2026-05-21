import sqlite3
import os
import re
import shutil
import statistics
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, g, flash, session

app = Flask(__name__)
app.secret_key = os.environ.get('PARKGOLF_SECRET_KEY', 'parkgolf-dev-secret-key')

# ── Admin password ─────────────────────────────────────────────────────────────
# Change this before deploying: set the PARKGOLF_ADMIN_PASSWORD environment variable.
# e.g.  export PARKGOLF_ADMIN_PASSWORD="your-strong-password"
ADMIN_PASSWORD = os.environ.get('PARKGOLF_ADMIN_PASSWORD', 'admin')

# On Render, set DB_PATH env var to point to the persistent disk, e.g. /data/parkgolf.db
# Locally it defaults to the project folder.
DATABASE = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'parkgolf.db'))


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def parse_sort_date(date_str):
    """Convert display date strings to YYYY-MM-DD for chronological sorting."""
    # Collapse ranges like "July 12-13, 2025" → "July 12, 2025"
    date_str = re.sub(r'(\w+ \d+)-\d+,', r'\1,', date_str.strip())
    for fmt in ('%B %d, %Y', '%b %d, %Y', '%B %d %Y'):
        try:
            return datetime.strptime(date_str, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return date_str  # fallback: sort as-is


def age_cutoff():
    """Return the YYYY-MM-DD string for exactly 1 year ago."""
    today = datetime.today()
    try:
        return today.replace(year=today.year - 1).strftime('%Y-%m-%d')
    except ValueError:  # Feb 29 in a leap year
        return today.replace(year=today.year - 1, day=28).strftime('%Y-%m-%d')


def compute_player_rating(rounds):
    """
    Apply rating adjustments:
      1. Players with 8+ rounds: drop rounds older than 1 year.
         Players with 7 or fewer: keep all rounds regardless of age.
      2. Exclude rounds rated more than 2 std devs below the player's mean.
      3. Weight the most recent 25% of rounds (up to 8) 2x over older rounds.

    rounds: list of dicts with keys: round_id, rating, sort_date, round_number
    Returns: {'rating': float, 'rounds_used': int, 'excluded_ids': set}
    """
    if not rounds:
        return {'rating': None, 'rounds_used': 0, 'excluded_ids': set()}

    # Apply 1-year age cutoff only for players with 8+ recorded rounds
    if len(rounds) >= 8:
        cutoff = age_cutoff()
        rounds = [r for r in rounds if r.get('sort_date') and r['sort_date'] >= cutoff]

    if not rounds:
        return {'rating': None, 'rounds_used': 0, 'excluded_ids': set()}

    ratings = [r['rating'] for r in rounds]
    excluded_ids = set()

    if len(ratings) >= 2:
        mean = statistics.mean(ratings)
        stdev = statistics.stdev(ratings)
        rating_cutoff = mean - 2 * stdev
        excluded_ids = {r['round_id'] for r in rounds if r['rating'] < rating_cutoff}

    filtered = [r for r in rounds if r['round_id'] not in excluded_ids] or rounds

    # Sort newest first for recency weighting
    filtered_sorted = sorted(
        filtered,
        key=lambda r: (r['sort_date'], r['round_number']),
        reverse=True
    )

    # Weight top 25% of rounds (by recency), at least 1, at most 8
    recent_count = min(8, max(1, len(filtered_sorted) // 4))

    weighted_sum = weight_total = 0
    for i, r in enumerate(filtered_sorted):
        w = 2 if i < recent_count else 1
        weighted_sum += r['rating'] * w
        weight_total += w

    return {
        'rating': round(weighted_sum / weight_total, 1),
        'rounds_used': len(filtered),
        'excluded_ids': excluded_ids,
        'recent_count': recent_count,
    }



def flex_points_for_position(pos):
    """Flex League points: 1st=100, 2nd=90, then −5 per place."""
    if pos == 1:
        return 100.0
    return max(0.0, 90.0 - (pos - 2) * 5.0)


def travelers_points_for_position(pos):
    """IPGAA Traveler's League points: 1st=100, 2nd=97, 3rd=95, 4th=94, 5th=93, then −1/place, min 75."""
    if pos == 1:
        return 100.0
    if pos == 2:
        return 97.0
    if pos == 3:
        return 95.0
    if pos == 4:
        return 94.0
    if pos == 5:
        return 93.0
    return max(75.0, 93.0 - (pos - 5))


def assign_league_points(player_scores, points_fn):
    """
    Generic league point assignment for one division in one tournament.

    player_scores: list of (player_id, total_score) — lower score = better finish
    points_fn:     function(position) → float
    Returns: dict of {player_id: points}

    Ties: average the points for all tied positions.
    Example (Flex): tie for 2nd among 2 → (90 + 85) / 2 = 87.5 each; next player is 4th.
    """
    if not player_scores:
        return {}

    sorted_players = sorted(player_scores, key=lambda x: x[1])
    result = {}
    i = 0
    while i < len(sorted_players):
        j = i
        while j < len(sorted_players) and sorted_players[j][1] == sorted_players[i][1]:
            j += 1
        tied_pts = sum(points_fn(p) for p in range(i + 1, j + 1)) / (j - i)
        for k in range(i, j):
            result[sorted_players[k][0]] = round(tied_pts, 1)
        i = j
    return result


def compute_flex_standings(db):
    """
    Build Flex League standings across all Flex League tournaments.

    Returns:
      {
        'tournaments': [{'id', 'name', 'date'}, ...],   # in date order
        'Men':    [player_row, ...],                      # sorted by total desc
        'Women':  [player_row, ...],
      }

    Each player_row:
      {
        'player_id', 'name', 'total_points', 'events_played',
        'weekly': {tournament_id: {'points': float|None, 'counted': bool}},
      }
    """
    tournaments = db.execute('''
        SELECT id, name, date, sort_date
        FROM tournaments
        WHERE league = 'Flex League'
        ORDER BY sort_date, id
    ''').fetchall()

    if not tournaments:
        return {'tournaments': [], 'Men': [], 'Women': []}

    # For each tournament compute per-division points
    tour_points = {}   # {tournament_id: {player_id: points}}
    for t in tournaments:
        rows = db.execute('''
            SELECT r.player_id, p.division, SUM(r.score) AS total_score
            FROM rounds r
            JOIN players p ON p.id = r.player_id
            WHERE r.tournament_id = ?
            GROUP BY r.player_id
        ''', (t['id'],)).fetchall()

        by_div = {'Men': [], 'Women': []}
        for row in rows:
            div = row['division'] if row['division'] in by_div else 'Men'
            by_div[div].append((row['player_id'], row['total_score']))

        pts = {}
        for div_players in by_div.values():
            pts.update(assign_league_points(div_players, flex_points_for_position))
        tour_points[t['id']] = pts

    # Collect all player ids that appear in any flex tournament
    all_pids = set(pid for pts in tour_points.values() for pid in pts)
    if not all_pids:
        return {'tournaments': [dict(t) for t in tournaments], 'Men': [], 'Women': []}

    players_info = {
        r['id']: {'name': r['name'], 'division': r['division']}
        for r in db.execute(
            'SELECT id, name, division FROM players WHERE id IN ({})'.format(
                ','.join('?' * len(all_pids))),
            list(all_pids)
        ).fetchall()
    }

    standings = {'Men': [], 'Women': []}
    for pid, info in players_info.items():
        div = info['division'] if info['division'] in standings else 'Men'

        # Build weekly points list in tournament order
        weekly_pts = [(t['id'], tour_points[t['id']].get(pid)) for t in tournaments]
        earned = [(tid, p) for tid, p in weekly_pts if p is not None]

        # Determine top-8 counted weeks
        sorted_earned = sorted(earned, key=lambda x: x[1], reverse=True)
        counted_ids = {tid for tid, _ in sorted_earned[:8]}

        total = sum(p for tid, p in sorted_earned[:8])

        weekly = {
            tid: {'points': p, 'counted': (p is not None and tid in counted_ids)}
            for tid, p in weekly_pts
        }

        standings[div].append({
            'player_id': pid,
            'name': info['name'],
            'total_points': round(total, 1),
            'events_played': len(earned),
            'weekly': weekly,
        })

    for div in standings:
        standings[div].sort(key=lambda x: x['total_points'], reverse=True)

    return {
        'tournaments': [{'id': t['id'], 'name': t['name'], 'date': t['date']} for t in tournaments],
        'Men': standings['Men'],
        'Women': standings['Women'],
    }


def compute_travelers_standings(db):
    """
    Build IPGAA Traveler's League standings. Same structure as compute_flex_standings
    but uses travelers_points_for_position and only the top 3 finishes count.
    """
    tournaments = db.execute(
        "SELECT id, name, date, sort_date FROM tournaments WHERE league = ? ORDER BY sort_date, id",
        ("IPGAA Traveler's League",)
    ).fetchall()

    if not tournaments:
        return {'tournaments': [], 'Men': [], 'Women': []}

    tour_points = {}
    for t in tournaments:
        rows = db.execute('''
            SELECT r.player_id, p.division, SUM(r.score) AS total_score
            FROM rounds r
            JOIN players p ON p.id = r.player_id
            WHERE r.tournament_id = ?
            GROUP BY r.player_id
        ''', (t['id'],)).fetchall()

        by_div = {'Men': [], 'Women': []}
        for row in rows:
            div = row['division'] if row['division'] in by_div else 'Men'
            by_div[div].append((row['player_id'], row['total_score']))

        pts = {}
        for div_players in by_div.values():
            pts.update(assign_league_points(div_players, travelers_points_for_position))
        tour_points[t['id']] = pts

    all_pids = set(pid for pts in tour_points.values() for pid in pts)
    if not all_pids:
        return {'tournaments': [dict(t) for t in tournaments], 'Men': [], 'Women': []}

    players_info = {
        r['id']: {'name': r['name'], 'division': r['division']}
        for r in db.execute(
            'SELECT id, name, division FROM players WHERE id IN ({})'.format(
                ','.join('?' * len(all_pids))),
            list(all_pids)
        ).fetchall()
    }

    standings = {'Men': [], 'Women': []}
    for pid, info in players_info.items():
        div = info['division'] if info['division'] in standings else 'Men'

        weekly_pts = [(t['id'], tour_points[t['id']].get(pid)) for t in tournaments]
        earned = [(tid, p) for tid, p in weekly_pts if p is not None]

        sorted_earned = sorted(earned, key=lambda x: x[1], reverse=True)
        counted_ids = {tid for tid, _ in sorted_earned[:3]}  # top 3 only

        total = sum(p for tid, p in sorted_earned[:3])

        weekly = {
            tid: {'points': p, 'counted': (p is not None and tid in counted_ids)}
            for tid, p in weekly_pts
        }

        standings[div].append({
            'player_id': pid,
            'name': info['name'],
            'total_points': round(total, 1),
            'events_played': len(earned),
            'weekly': weekly,
        })

    for div in standings:
        standings[div].sort(key=lambda x: x['total_points'], reverse=True)

    return {
        'tournaments': [{'id': t['id'], 'name': t['name'], 'date': t['date']} for t in tournaments],
        'Men': standings['Men'],
        'Women': standings['Women'],
    }


def init_db():
    # On first deploy to Render, seed the persistent disk DB from the repo copy
    repo_db = os.path.join(os.path.dirname(__file__), 'parkgolf.db')
    if not os.path.exists(DATABASE) and os.path.exists(repo_db) and DATABASE != repo_db:
        shutil.copy2(repo_db, DATABASE)

    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    with open(os.path.join(os.path.dirname(__file__), 'schema.sql')) as f:
        db.executescript(f.read())

    # Migrate: initial_rating column
    cols = [r[1] for r in db.execute("PRAGMA table_info(courses)").fetchall()]
    if 'initial_rating' not in cols:
        db.execute("ALTER TABLE courses ADD COLUMN initial_rating REAL")

    # Migrate: sort_date column on tournaments
    tcols = [r[1] for r in db.execute("PRAGMA table_info(tournaments)").fetchall()]
    if 'sort_date' not in tcols:
        db.execute("ALTER TABLE tournaments ADD COLUMN sort_date TEXT")

    # Seed courses if not present
    if not db.execute('SELECT 1 FROM courses').fetchone():
        db.execute("INSERT INTO courses (name, course_rating, initial_rating, location) VALUES (?, ?, ?, ?)",
                   ('Destroyer Park Golf', 46, 46, 'Destroyer'))
        db.execute("INSERT INTO courses (name, course_rating, initial_rating, location) VALUES (?, ?, ?, ?)",
                   ('Wormburner Park Golf', 49, 49, 'Wormburner'))

    # Backfill initial_rating
    db.execute("UPDATE courses SET initial_rating = 46 WHERE name = 'Destroyer Park Golf' AND initial_rating IS NULL")
    db.execute("UPDATE courses SET initial_rating = 49 WHERE name = 'Wormburner Park Golf' AND initial_rating IS NULL")

    # Backfill sort_date for existing tournaments
    for t in db.execute("SELECT id, date FROM tournaments WHERE sort_date IS NULL").fetchall():
        db.execute("UPDATE tournaments SET sort_date = ? WHERE id = ?",
                   (parse_sort_date(t['date']), t['id']))

    # Migrate: league column on tournaments
    tourcols = [r[1] for r in db.execute("PRAGMA table_info(tournaments)").fetchall()]
    if 'league' not in tourcols:
        db.execute("ALTER TABLE tournaments ADD COLUMN league TEXT")

    # Migrate: rating_adjustment column on courses (kept for potential future use)
    ccols = [r[1] for r in db.execute("PRAGMA table_info(courses)").fetchall()]
    if 'rating_adjustment' not in ccols:
        db.execute("ALTER TABLE courses ADD COLUMN rating_adjustment REAL DEFAULT 0")
    db.execute("UPDATE courses SET rating_adjustment = 0 WHERE rating_adjustment IS NULL")
    # Reset any previously set adjustment — new formula derives scratch from player ratings
    db.execute("UPDATE courses SET rating_adjustment = 0")

    # Migrate: is_nr column on rounds (marks rounds that cannot be officially rated)
    rcols = [r[1] for r in db.execute("PRAGMA table_info(rounds)").fetchall()]
    if 'is_nr' not in rcols:
        db.execute("ALTER TABLE rounds ADD COLUMN is_nr INTEGER DEFAULT 0")
    db.execute("UPDATE rounds SET is_nr = 0 WHERE is_nr IS NULL")

    # Migrate: division column on players
    pcols = [r[1] for r in db.execute("PRAGMA table_info(players)").fetchall()]
    if 'division' not in pcols:
        db.execute("ALTER TABLE players ADD COLUMN division TEXT DEFAULT 'Men'")
    if 'current_rating' not in pcols:
        db.execute("ALTER TABLE players ADD COLUMN current_rating REAL")
    if 'previous_rating' not in pcols:
        db.execute("ALTER TABLE players ADD COLUMN previous_rating REAL")
    if 'peak_rating' not in pcols:
        db.execute("ALTER TABLE players ADD COLUMN peak_rating REAL")

    # Backfill division: ensure all players have a value, then set known women
    db.execute("UPDATE players SET division = 'Men' WHERE division IS NULL")
    women = ['Erin', 'Courtney', 'Christine', 'Kathy', 'Sharon', 'Nancy', 'Cheryl', 'Harper']
    for first_name in women:
        db.execute(
            "UPDATE players SET division = 'Women' WHERE name LIKE ?",
            (first_name + ' %',)
        )

    # Backfill current_rating for all players (so deltas work after first tournament add)
    needs_backfill = db.execute("SELECT COUNT(*) FROM players WHERE current_rating IS NULL").fetchone()[0]
    if needs_backfill:
        rows = db.execute('''
            SELECT r.id AS round_id, r.rating, r.round_number, t.sort_date, r.player_id
            FROM rounds r JOIN tournaments t ON t.id = r.tournament_id
        ''').fetchall()
        pmap = {}
        for r in rows:
            pid = r['player_id']
            if pid not in pmap:
                pmap[pid] = []
            pmap[pid].append(dict(r))
        for pid, rnds in pmap.items():
            result = compute_player_rating(rnds)
            db.execute('UPDATE players SET current_rating = ? WHERE id = ?', (result['rating'], pid))

    # Backfill peak_rating = current_rating for existing players (first-time migration)
    db.execute('''
        UPDATE players SET peak_rating = current_rating
        WHERE peak_rating IS NULL AND current_rating IS NOT NULL
    ''')

    # Always recalculate all round ratings and player ratings on startup
    # so formula changes take effect immediately on existing data.
    recalculate_ratings(db)
    all_rounds = db.execute('''
        SELECT r.id AS round_id, r.rating, r.round_number, t.sort_date, r.player_id
        FROM rounds r JOIN tournaments t ON t.id = r.tournament_id
        WHERE r.is_nr = 0 OR r.is_nr IS NULL
    ''').fetchall()
    pmap = {}
    for r in all_rounds:
        pid = r['player_id']
        if pid not in pmap:
            pmap[pid] = []
        pmap[pid].append(dict(r))
    for pid, rnds in pmap.items():
        result = compute_player_rating(rnds)
        new_rating = result['rating']
        db.execute('''
            UPDATE players SET current_rating = ?,
                peak_rating = CASE WHEN peak_rating IS NULL OR ? > peak_rating THEN ? ELSE peak_rating END
            WHERE id = ?
        ''', (new_rating, new_rating, new_rating, pid))

    db.commit()
    db.close()


def calculate_rating(course_rating, score):
    return round(1000 + (course_rating - score) * 10)


def recalculate_ratings(db):
    """Recompute per-round ratings in strict chronological order, round-by-round.

    Rules:
      - First round ever (player_ratings empty): eff_scratch = 54 (seeds the system)
      - 3+ established players in a round: derive eff_scratch from their avg score/rating
          eff_scratch = avg_score_of_rated_players + (avg_rating_of_rated_players - 1000) / 10
      - Fewer than 3 established players (after first round): NR — is_nr = 1, no rating assigned
      - After each rated round, immediately update player_ratings so subsequent rounds
        (including later rounds in the same tournament) can use them.
    """
    # Update course_rating for display reference only (all-time avg - 15)
    for course in db.execute('SELECT id FROM courses').fetchall():
        cid = course['id']
        avg_all = db.execute('''
            SELECT AVG(r.score) FROM rounds r
            JOIN tournaments t ON t.id = r.tournament_id WHERE t.course_id = ?
        ''', (cid,)).fetchone()[0]
        if avg_all is not None:
            db.execute('UPDATE courses SET course_rating = ? WHERE id = ?',
                       (round(avg_all - 15, 1), cid))

    tournaments = db.execute(
        'SELECT id, sort_date FROM tournaments ORDER BY sort_date, id'
    ).fetchall()

    # Pre-seed known elite players at 1000 so the very first round can be
    # calibrated using the ≥3 established players formula rather than a fixed scratch.
    # Their pre-seed ratings are replaced by real computed ratings after round 1.
    seed_names = ['Hobart Shaw', 'Brandon Nihiser', 'Taylor Junge']
    seed_rows = db.execute(
        'SELECT id FROM players WHERE name IN ({})'.format(
            ','.join('?' * len(seed_names))
        ), seed_names
    ).fetchall()
    player_ratings = {r['id']: 1000.0 for r in seed_rows}
    player_round_history = {}  # {player_id: [round dicts]} — fed into compute_player_rating

    for t in tournaments:
        tid = t['id']
        sort_date = t['sort_date']

        round_numbers = [r['round_number'] for r in db.execute(
            'SELECT DISTINCT round_number FROM rounds WHERE tournament_id = ? ORDER BY round_number',
            (tid,)
        ).fetchall()]

        for round_number in round_numbers:
            round_rows = db.execute(
                'SELECT id, score, player_id FROM rounds '
                'WHERE tournament_id = ? AND round_number = ?',
                (tid, round_number)
            ).fetchall()

            # Players in this round who already have an established rating
            rated_rows = [r for r in round_rows if r['player_id'] in player_ratings]

            if len(rated_rows) >= 3:
                avg_score  = sum(r['score'] for r in rated_rows) / len(rated_rows)
                avg_rating = sum(player_ratings[r['player_id']] for r in rated_rows) / len(rated_rows)
                eff_scratch = avg_score + (avg_rating - 1000) / 10
                is_rated = True
            else:
                is_rated = False

            if is_rated:
                for r in round_rows:
                    round_rating = round(1000 + (eff_scratch - r['score']) * 10)
                    db.execute('UPDATE rounds SET rating = ?, is_nr = 0 WHERE id = ?',
                               (round_rating, r['id']))
                    pid = r['player_id']
                    if pid not in player_round_history:
                        player_round_history[pid] = []
                    player_round_history[pid].append({
                        'round_id':     r['id'],
                        'rating':       round_rating,
                        'sort_date':    sort_date,
                        'round_number': round_number,
                    })

                # Update player_ratings immediately so the next round can use them
                for r in round_rows:
                    pid = r['player_id']
                    if pid in player_round_history:
                        result = compute_player_rating(player_round_history[pid])
                        if result['rating'] is not None:
                            player_ratings[pid] = result['rating']
            else:
                for r in round_rows:
                    db.execute('UPDATE rounds SET is_nr = 1 WHERE id = ?', (r['id'],))


def update_player_ratings(db):
    """Compute all player ratings and persist current_rating + peak_rating.
    peak_rating never decreases — it holds the all-time high.
    """
    all_rounds = fetch_rounds_for_rating(db)
    player_map = {}
    for r in all_rounds:
        pid = r['player_id']
        if pid not in player_map:
            player_map[pid] = []
        player_map[pid].append(r)
    for pid, rounds in player_map.items():
        result = compute_player_rating(rounds)
        new_rating = result['rating']
        db.execute('''
            UPDATE players SET
                current_rating = ?,
                peak_rating = CASE
                    WHEN peak_rating IS NULL OR ? > peak_rating THEN ?
                    ELSE peak_rating
                END
            WHERE id = ?
        ''', (new_rating, new_rating, new_rating, pid))


def fetch_rounds_for_rating(db, player_id=None):
    """Fetch all officially-rated rounds (is_nr = 0) in the shape compute_player_rating expects."""
    if player_id:
        rows = db.execute('''
            SELECT r.id AS round_id, r.rating, r.round_number, t.sort_date
            FROM rounds r JOIN tournaments t ON t.id = r.tournament_id
            WHERE r.player_id = ? AND (r.is_nr = 0 OR r.is_nr IS NULL)
        ''', (player_id,)).fetchall()
    else:
        rows = db.execute('''
            SELECT r.id AS round_id, r.rating, r.round_number, t.sort_date,
                   r.player_id, p.name
            FROM rounds r
            JOIN tournaments t ON t.id = r.tournament_id
            JOIN players p ON p.id = r.player_id
            WHERE r.is_nr = 0 OR r.is_nr IS NULL
        ''').fetchall()
    return [dict(r) for r in rows]


# ── Auth ──────────────────────────────────────────────────────────────────────

def admin_required(f):
    """Redirect to login if the current session is not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('admin'):
        return redirect(url_for('index'))
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin'] = True
            next_url = request.args.get('next') or url_for('index')
            return redirect(next_url)
        flash('Incorrect password.', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('admin', None)
    flash('Logged out.', 'info')
    return redirect(url_for('index'))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    db = get_db()

    # Available seasons for the year filter
    year_rows = db.execute('''
        SELECT DISTINCT substr(sort_date, 1, 4) AS year
        FROM tournaments WHERE sort_date IS NOT NULL
        ORDER BY year DESC
    ''').fetchall()
    available_years = [r['year'] for r in year_rows]
    selected_year = request.args.get('year', 'all')
    if selected_year not in available_years:
        selected_year = 'all'

    all_rounds = fetch_rounds_for_rating(db)

    # Filter to the selected season if needed
    if selected_year != 'all':
        all_rounds = [r for r in all_rounds if r.get('sort_date', '').startswith(selected_year)]

    # Load division info for each player
    divisions = {r['id']: r['division'] for r in db.execute('SELECT id, division FROM players').fetchall()}

    player_map = {}
    for r in all_rounds:
        pid = r['player_id']
        if pid not in player_map:
            player_map[pid] = {'id': pid, 'name': r['name'], 'rounds': [],
                               'division': divisions.get(pid, 'Men')}
        player_map[pid]['rounds'].append(r)

    players = []
    for pid, data in player_map.items():
        result = compute_player_rating(data['rounds'])
        all_ratings = [r['rating'] for r in data['rounds']]
        players.append({
            'id': pid,
            'name': data['name'],
            'division': data['division'],
            'rating': result['rating'],
            'num_rounds': result['rounds_used'],
            'total_rounds': len(data['rounds']),
            'best_rating': max(all_ratings),
            'excluded': len(result['excluded_ids']),
        })

    players.sort(key=lambda x: (x['rating'] is not None, x['rating']), reverse=True)

    # Rank/rating deltas only make sense for the all-time view
    if selected_year == 'all':
        prev_rows = db.execute('SELECT id, previous_rating, current_rating FROM players').fetchall()
        prev_rating = {r['id']: r['previous_rating'] for r in prev_rows if r['previous_rating'] is not None}
        prev_sorted = sorted(prev_rating.items(), key=lambda x: x[1], reverse=True)
        prev_rank = {pid: i + 1 for i, (pid, _) in enumerate(prev_sorted)}
        for i, p in enumerate(players):
            pid = p['id']
            cur_rank = i + 1
            p['rank_change'] = (prev_rank[pid] - cur_rank) if pid in prev_rank else None
            prev_r = prev_rating.get(pid)
            p['rating_change'] = round(p['rating'] - prev_r, 1) if p['rating'] and prev_r else None
    else:
        for p in players:
            p['rank_change'] = None
            p['rating_change'] = None

    return render_template('index.html', players=players,
                           available_years=available_years,
                           selected_year=selected_year)


@app.route('/player/<int:player_id>')
def player(player_id):
    db = get_db()
    p = db.execute('SELECT * FROM players WHERE id = ?', (player_id,)).fetchone()
    if not p:
        return redirect(url_for('index'))

    rounds_raw = fetch_rounds_for_rating(db, player_id)
    result = compute_player_rating(rounds_raw)
    excluded_ids = result['excluded_ids']

    # Rounds older than 1 year are expired — but only for players with 8+ rounds.
    # Players with 7 or fewer keep all rounds regardless of age.
    if len(rounds_raw) >= 8:
        cutoff = age_cutoff()
        expired_ids = {r['round_id'] for r in rounds_raw if not r.get('sort_date') or r['sort_date'] < cutoff}
    else:
        expired_ids = set()

    # recent_ids: top 25% (up to 8) of non-excluded, non-expired rounds
    active = [r for r in rounds_raw if r['round_id'] not in excluded_ids and r['round_id'] not in expired_ids]
    filtered_sorted = sorted(active, key=lambda r: (r['sort_date'], r['round_number']), reverse=True)
    recent_count = result.get('recent_count', min(8, max(1, len(filtered_sorted) // 4)))
    recent_ids = {r['round_id'] for r in filtered_sorted[:recent_count]}

    rounds = db.execute('''
        SELECT r.id AS round_id, r.round_number, r.score, r.rating, r.is_nr,
               t.id AS tournament_id, t.name AS tournament_name, t.date, t.sort_date,
               c.name AS course_name, c.course_rating
        FROM rounds r
        JOIN tournaments t ON t.id = r.tournament_id
        JOIN courses c ON c.id = t.course_id
        WHERE r.player_id = ?
        ORDER BY t.sort_date, r.round_number
    ''', (player_id,)).fetchall()

    rated_rounds = [r for r in rounds if not r['is_nr']]
    all_ratings = [r['rating'] for r in rated_rounds]
    stats = {
        'rating': result['rating'],
        'num_rounds': result['rounds_used'],
        'total_rounds': len(rated_rounds),
        'excluded': len(excluded_ids),
        'expired': len(expired_ids),
        'nr_count': len(rounds) - len(rated_rounds),
        'best_rating': max(all_ratings) if all_ratings else None,
        'worst_rating': min(all_ratings) if all_ratings else None,
    }

    # Rolling rating: what the player's official rating was after each rated round
    rounds_raw_sorted = sorted(rounds_raw, key=lambda r: (r.get('sort_date') or '', r['round_number']))
    rolling_map = {}
    for i in range(len(rounds_raw_sorted)):
        res_i = compute_player_rating(rounds_raw_sorted[:i + 1])
        rolling_map[rounds_raw_sorted[i]['round_id']] = res_i['rating']

    # Build chart data — only include rated rounds in the chart
    chart_data = []
    for r in rounds:
        if r['is_nr']:
            continue  # NR rounds don't appear on the chart
        if r['round_id'] in excluded_ids:
            status = 'excluded'
        elif r['round_id'] in expired_ids:
            status = 'expired'
        elif r['round_id'] in recent_ids:
            status = 'recent'
        else:
            status = 'normal'
        try:
            lbl = datetime.strptime(r['sort_date'], '%Y-%m-%d').strftime('%b %d') + f" R{r['round_number']}"
        except (ValueError, TypeError):
            lbl = f"R{r['round_number']}"
        chart_data.append({
            'label': lbl,
            'tournament': r['tournament_name'],
            'date': r['date'],
            'round': r['round_number'],
            'score': r['score'],
            'rating': r['rating'],
            'rolling_rating': rolling_map.get(r['round_id']),
            'status': status,
        })

    return render_template('player.html', player=p, stats=stats, rounds=rounds,
                           excluded_ids=excluded_ids, expired_ids=expired_ids,
                           recent_ids=recent_ids, recent_count=recent_count,
                           chart_data=chart_data)


@app.route('/events')
def events():
    db = get_db()
    tournaments = db.execute('''
        SELECT t.id, t.name, t.date, t.num_rounds, t.league,
               c.name AS course_name, c.course_rating,
               COUNT(DISTINCT r.player_id) AS num_players
        FROM tournaments t
        JOIN courses c ON c.id = t.course_id
        LEFT JOIN rounds r ON r.tournament_id = t.id
        GROUP BY t.id
        ORDER BY t.sort_date
    ''').fetchall()
    return render_template('events.html', tournaments=tournaments)


@app.route('/tournament/<int:tournament_id>')
def tournament(tournament_id):
    db = get_db()
    t = db.execute('''
        SELECT t.*, c.name AS course_name, c.course_rating, c.initial_rating
        FROM tournaments t JOIN courses c ON c.id = t.course_id
        WHERE t.id = ?
    ''', (tournament_id,)).fetchone()

    if not t:
        return redirect(url_for('events'))

    players_raw = db.execute('''
        SELECT p.id AS player_id, p.name,
               r.round_number, r.score, r.rating, r.is_nr
        FROM rounds r
        JOIN players p ON p.id = r.player_id
        WHERE r.tournament_id = ?
        ORDER BY p.name, r.round_number
    ''', (tournament_id,)).fetchall()

    player_map = {}
    for row in players_raw:
        pid = row['player_id']
        if pid not in player_map:
            player_map[pid] = {'name': row['name'], 'player_id': pid, 'rounds': {}}
        player_map[pid]['rounds'][row['round_number']] = {
            'score': row['score'], 'rating': row['rating'], 'is_nr': bool(row['is_nr'])
        }

    results = []
    for pid, data in player_map.items():
        rounds = data['rounds']
        total_score = sum(r['score'] for r in rounds.values())
        rated_rounds = [r for r in rounds.values() if not r['is_nr']]
        avg_rating = round(sum(r['rating'] for r in rated_rounds) / len(rated_rounds)) if rated_rounds else None
        results.append({
            'player_id': pid,
            'name': data['name'],
            'rounds': rounds,
            'total_score': total_score,
            'avg_rating': avg_rating,
            'num_rounds': len(rounds),
        })
    results.sort(key=lambda x: (x['avg_rating'] is None, -(x['avg_rating'] or 0)))

    round_numbers = list(range(1, t['num_rounds'] + 1))

    # Per-round stats for display: avg score and actual derived eff_scratch
    # eff_scratch is back-calculated from stored ratings: score + (rating - 1000) / 10
    round_stats = {}
    for row in db.execute('''
        SELECT round_number,
               AVG(score) AS avg_score,
               AVG(CASE WHEN is_nr = 0 THEN score + (rating - 1000.0) / 10 END) AS eff_scratch,
               SUM(CASE WHEN is_nr = 0 THEN 1 ELSE 0 END) AS rated_count
        FROM rounds WHERE tournament_id = ?
        GROUP BY round_number
    ''', (tournament_id,)).fetchall():
        eff = row['eff_scratch']
        round_stats[row['round_number']] = {
            'avg_score':  round(row['avg_score'], 1),
            'eff_scratch': round(eff, 1) if eff is not None else None,
            'is_nr': row['rated_count'] == 0,
        }

    return render_template('tournament.html', t=t, results=results,
                           round_numbers=round_numbers, round_stats=round_stats)


@app.route('/add', methods=['GET', 'POST'])
@admin_required
def add_tournament():
    db = get_db()
    courses = db.execute('SELECT * FROM courses ORDER BY name').fetchall()

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        course_id_raw = request.form.get('course_id', '').strip()
        date = request.form.get('date', '').strip()
        num_rounds = request.form.get('num_rounds', type=int)
        league = request.form.get('league', '').strip() or None

        # Handle new course creation
        if course_id_raw == 'new':
            new_course_name = request.form.get('new_course_name', '').strip()
            new_course_location = request.form.get('new_course_location', '').strip()
            new_course_rating_str = request.form.get('new_course_rating', '').strip()
            if not new_course_name:
                flash('Please enter a name for the new course.', 'danger')
                return render_template('add_tournament.html', courses=courses)
            new_course_rating = float(new_course_rating_str) if new_course_rating_str else 45.0
            db.execute(
                'INSERT INTO courses (name, course_rating, initial_rating, location) VALUES (?, ?, ?, ?)',
                (new_course_name, new_course_rating, new_course_rating, new_course_location or None)
            )
            db.commit()
            course_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            flash(f'Course "{new_course_name}" created.', 'info')
        else:
            try:
                course_id = int(course_id_raw)
            except (ValueError, TypeError):
                course_id = None

        if not all([name, course_id, date, num_rounds]):
            flash('Please fill in all tournament fields.', 'danger')
            return render_template('add_tournament.html', courses=courses)

        course = db.execute('SELECT * FROM courses WHERE id = ?', (course_id,)).fetchone()
        if not course:
            flash('Invalid course.', 'danger')
            return render_template('add_tournament.html', courses=courses)

        # date input is YYYY-MM-DD from the date picker — use directly as sort_date
        sort_date = date
        display_date = datetime.strptime(date, '%Y-%m-%d').strftime('%B %d, %Y') if re.match(r'\d{4}-\d{2}-\d{2}', date) else date

        db.execute('INSERT INTO tournaments (name, course_id, date, sort_date, num_rounds, league) VALUES (?, ?, ?, ?, ?, ?)',
                   (name, course_id, display_date, sort_date, num_rounds, league))
        tournament_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

        player_names = request.form.getlist('player_name')
        errors = []
        inserted = 0

        for i, pname in enumerate(player_names):
            pname = pname.strip().title()
            if not pname:
                continue

            scores = []
            for rn in range(1, num_rounds + 1):
                score_str = request.form.get(f'score_{i}_{rn}', '').strip()
                if score_str:
                    try:
                        scores.append((rn, int(score_str)))
                    except ValueError:
                        errors.append(f"Invalid score for {pname} round {rn}")

            if not scores:
                continue

            division = request.form.get(f'division_{i}', 'Men')

            existing = db.execute(
                'SELECT id FROM players WHERE LOWER(name) = LOWER(?)', (pname,)
            ).fetchone()
            if existing:
                player_id = existing['id']
                # Update division if it changed
                db.execute('UPDATE players SET division = ? WHERE id = ?', (division, player_id))
            else:
                db.execute('INSERT INTO players (name, division) VALUES (?, ?)', (pname, division))
                player_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

            for round_number, score in scores:
                rating = calculate_rating(course['course_rating'], score)
                db.execute(
                    'INSERT INTO rounds (tournament_id, player_id, round_number, score, rating) VALUES (?, ?, ?, ?, ?)',
                    (tournament_id, player_id, round_number, score, rating)
                )
                inserted += 1

        # Snapshot current ratings as "previous" before recalculating
        db.execute("UPDATE players SET previous_rating = current_rating")
        db.commit()
        recalculate_ratings(db)
        update_player_ratings(db)
        db.commit()

        if errors:
            for e in errors:
                flash(e, 'warning')
        flash(f'Tournament added with {inserted} round(s).', 'success')
        return redirect(url_for('tournament', tournament_id=tournament_id))

    all_players = db.execute('SELECT id, name, division FROM players ORDER BY name').fetchall()
    return render_template('add_tournament.html', courses=courses, all_players=all_players)


@app.route('/stats')
def stats():
    db = get_db()
    cutoff = age_cutoff()

    def top1_by_div(rows):
        """Return {'Men': first_men_row, 'Women': first_women_row} from an ordered list."""
        result = {'Men': None, 'Women': None}
        for r in rows:
            div = r['division']
            if div in result and result[div] is None:
                result[div] = r
            if result['Men'] and result['Women']:
                break
        return result

    # Highest player rating ever held (peak_rating — never decreases)
    peak_raw = db.execute('''
        SELECT id AS player_id, name, division, peak_rating
        FROM players
        WHERE peak_rating IS NOT NULL
        ORDER BY peak_rating DESC
    ''').fetchall()
    peak_rating = top1_by_div(peak_raw)

    # Best single round ever (highest rating, all time)
    best_round_raw = db.execute('''
        SELECT p.id AS player_id, p.name, p.division, r.rating, r.score,
               r.round_number, t.name AS tournament_name, t.date,
               c.name AS course_name
        FROM rounds r
        JOIN players p ON p.id = r.player_id
        JOIN tournaments t ON t.id = r.tournament_id
        JOIN courses c ON c.id = t.course_id
        ORDER BY r.rating DESC
    ''').fetchall()
    best_round = top1_by_div(best_round_raw)

    # Most rounds played (active / non-expired only, within 1 year)
    most_rounds_raw = db.execute('''
        SELECT p.id AS player_id, p.name, p.division,
               COUNT(r.id) AS num_rounds
        FROM rounds r
        JOIN players p ON p.id = r.player_id
        JOIN tournaments t ON t.id = r.tournament_id
        WHERE t.sort_date >= ? AND r.is_nr = 0
        GROUP BY p.id
        ORDER BY num_rounds DESC
    ''', (cutoff,)).fetchall()
    most_rounds = top1_by_div(most_rounds_raw)

    # Most 1000+ rated rounds (active only, within 1 year)
    elite_raw = db.execute('''
        SELECT p.id AS player_id, p.name, p.division,
               COUNT(r.id) AS elite_count
        FROM rounds r
        JOIN players p ON p.id = r.player_id
        JOIN tournaments t ON t.id = r.tournament_id
        WHERE r.rating >= 1000 AND t.sort_date >= ? AND r.is_nr = 0
        GROUP BY p.id
        ORDER BY elite_count DESC
    ''', (cutoff,)).fetchall()
    elite_rounds = top1_by_div(elite_raw)

    # Lowest score per course — #1 per division
    course_records = {}
    for course in db.execute('SELECT id, name FROM courses ORDER BY name').fetchall():
        cid = course['id']
        rec_raw = db.execute('''
            SELECT p.id AS player_id, p.name, p.division,
                   r.score, r.rating, t.name AS tournament_name, t.date
            FROM rounds r
            JOIN players p ON p.id = r.player_id
            JOIN tournaments t ON t.id = r.tournament_id
            WHERE t.course_id = ?
            ORDER BY r.score ASC
        ''', (cid,)).fetchall()
        if rec_raw:
            course_records[course['name']] = top1_by_div(rec_raw)

    return render_template('stats.html',
                           peak_rating=peak_rating,
                           best_round=best_round,
                           most_rounds=most_rounds,
                           elite_rounds=elite_rounds,
                           course_records=course_records)


@app.route('/tournament/<int:tournament_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_tournament(tournament_id):
    db = get_db()
    t = db.execute('SELECT * FROM tournaments WHERE id = ?', (tournament_id,)).fetchone()
    if not t:
        return redirect(url_for('events'))

    courses = db.execute('SELECT * FROM courses ORDER BY name').fetchall()
    all_players = db.execute('SELECT id, name, division FROM players ORDER BY name').fetchall()

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        course_id_raw = request.form.get('course_id', '').strip()
        date = request.form.get('date', '').strip()
        num_rounds = request.form.get('num_rounds', type=int)
        league = request.form.get('league', '').strip() or None

        if course_id_raw == 'new':
            new_course_name = request.form.get('new_course_name', '').strip()
            new_course_location = request.form.get('new_course_location', '').strip()
            new_course_rating_str = request.form.get('new_course_rating', '').strip()
            if not new_course_name:
                flash('Please enter a name for the new course.', 'danger')
                return redirect(url_for('edit_tournament', tournament_id=tournament_id))
            new_course_rating = float(new_course_rating_str) if new_course_rating_str else 45.0
            db.execute(
                'INSERT INTO courses (name, course_rating, initial_rating, location) VALUES (?, ?, ?, ?)',
                (new_course_name, new_course_rating, new_course_rating, new_course_location or None)
            )
            db.commit()
            course_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        else:
            try:
                course_id = int(course_id_raw)
            except (ValueError, TypeError):
                course_id = None

        if not all([name, course_id, date, num_rounds]):
            flash('Please fill in all tournament fields.', 'danger')
            return redirect(url_for('edit_tournament', tournament_id=tournament_id))

        sort_date = date
        display_date = datetime.strptime(date, '%Y-%m-%d').strftime('%B %d, %Y') if re.match(r'\d{4}-\d{2}-\d{2}', date) else date

        db.execute(
            'UPDATE tournaments SET name=?, course_id=?, date=?, sort_date=?, num_rounds=?, league=? WHERE id=?',
            (name, course_id, display_date, sort_date, num_rounds, league, tournament_id)
        )

        # Delete existing rounds for this tournament, then re-insert from form
        db.execute('DELETE FROM rounds WHERE tournament_id = ?', (tournament_id,))

        course = db.execute('SELECT * FROM courses WHERE id = ?', (course_id,)).fetchone()
        player_names = request.form.getlist('player_name')
        errors = []
        inserted = 0

        for i, pname in enumerate(player_names):
            pname = pname.strip().title()
            if not pname:
                continue

            scores = []
            for rn in range(1, num_rounds + 1):
                score_str = request.form.get(f'score_{i}_{rn}', '').strip()
                if score_str:
                    try:
                        scores.append((rn, int(score_str)))
                    except ValueError:
                        errors.append(f"Invalid score for {pname} round {rn}")

            if not scores:
                continue

            division = request.form.get(f'division_{i}', 'Men')
            existing = db.execute('SELECT id FROM players WHERE LOWER(name) = LOWER(?)', (pname,)).fetchone()
            if existing:
                player_id = existing['id']
                db.execute('UPDATE players SET division = ? WHERE id = ?', (division, player_id))
            else:
                db.execute('INSERT INTO players (name, division) VALUES (?, ?)', (pname, division))
                player_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

            for round_number, score in scores:
                rating = calculate_rating(course['course_rating'], score)
                db.execute(
                    'INSERT INTO rounds (tournament_id, player_id, round_number, score, rating) VALUES (?, ?, ?, ?, ?)',
                    (tournament_id, player_id, round_number, score, rating)
                )
                inserted += 1

        db.execute("UPDATE players SET previous_rating = current_rating")
        db.commit()
        recalculate_ratings(db)
        update_player_ratings(db)
        db.commit()

        if errors:
            for e in errors:
                flash(e, 'warning')
        flash(f'Tournament updated — {inserted} round(s) saved.', 'success')
        return redirect(url_for('tournament', tournament_id=tournament_id))

    # GET: load existing players/scores for pre-population
    players_raw = db.execute('''
        SELECT p.id, p.name, p.division, r.round_number, r.score
        FROM rounds r JOIN players p ON p.id = r.player_id
        WHERE r.tournament_id = ?
        ORDER BY p.name, r.round_number
    ''', (tournament_id,)).fetchall()

    player_data = {}
    player_order = []
    for row in players_raw:
        pid = row['id']
        if pid not in player_data:
            player_data[pid] = {'name': row['name'], 'division': row['division'], 'scores': {}}
            player_order.append(pid)
        player_data[pid]['scores'][row['round_number']] = row['score']

    existing_players = [player_data[pid] for pid in player_order]

    return render_template('edit_tournament.html', t=t, courses=courses,
                           all_players=all_players, existing_players=existing_players)


@app.route('/tournament/<int:tournament_id>/delete', methods=['GET', 'POST'])
@admin_required
def delete_tournament(tournament_id):
    db = get_db()
    t = db.execute('''
        SELECT t.*, c.name AS course_name FROM tournaments t
        JOIN courses c ON c.id = t.course_id WHERE t.id = ?
    ''', (tournament_id,)).fetchone()
    if not t:
        return redirect(url_for('events'))

    if request.method == 'POST':
        db.execute('DELETE FROM rounds WHERE tournament_id = ?', (tournament_id,))
        db.execute('DELETE FROM tournaments WHERE id = ?', (tournament_id,))
        db.execute("UPDATE players SET previous_rating = current_rating")
        db.commit()
        recalculate_ratings(db)
        update_player_ratings(db)
        db.commit()
        flash(f'Tournament "{t["name"]}" has been deleted.', 'success')
        return redirect(url_for('events'))

    round_count = db.execute('SELECT COUNT(*) FROM rounds WHERE tournament_id = ?', (tournament_id,)).fetchone()[0]
    player_count = db.execute('SELECT COUNT(DISTINCT player_id) FROM rounds WHERE tournament_id = ?', (tournament_id,)).fetchone()[0]
    return render_template('delete_confirm.html', t=t, round_count=round_count, player_count=player_count)


@app.route('/flex-league')
def flex_league():
    db = get_db()
    standings = compute_flex_standings(db)
    return render_template('flex_league.html', standings=standings)


@app.route('/travelers-league')
def travelers_league():
    db = get_db()
    standings = compute_travelers_standings(db)
    return render_template('travelers_league.html', standings=standings)


@app.route('/reset-baselines', methods=['POST'])
@admin_required
def reset_baselines():
    """Reset previous_rating and peak_rating to current_rating for all players.
    Clears change indicators and corrects any peak_rating inflation."""
    db = get_db()
    db.execute('UPDATE players SET previous_rating = current_rating, peak_rating = current_rating')
    db.commit()
    flash('Baselines reset — rating change indicators and peak ratings have been corrected.', 'success')
    return redirect(url_for('index'))


@app.route('/calculator')
def calculator():
    db = get_db()
    courses = db.execute('SELECT * FROM courses ORDER BY name').fetchall()
    return render_template('calculator.html', courses=courses)


# Initialize DB on startup whether running via gunicorn or directly
with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5001)
