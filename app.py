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

    # For each tournament compute per-division points, places, and scores
    tour_points = {}   # {tournament_id: {player_id: points}}
    tour_scores = {}   # {tournament_id: {player_id: total_score}}
    tour_places = {}   # {tournament_id: {player_id: finishing place in division}}
    for t in tournaments:
        rows = db.execute('''
            SELECT r.player_id, p.division, SUM(r.score) AS total_score
            FROM rounds r
            JOIN players p ON p.id = r.player_id
            WHERE r.tournament_id = ?
            GROUP BY r.player_id
        ''', (t['id'],)).fetchall()

        by_div = {'Men': [], 'Women': []}
        scores_map = {}
        for row in rows:
            div = row['division'] if row['division'] in by_div else 'Men'
            by_div[div].append((row['player_id'], row['total_score']))
            scores_map[row['player_id']] = row['total_score']

        tour_scores[t['id']] = scores_map

        pts = {}
        places = {}
        for div_players in by_div.values():
            pts.update(assign_league_points(div_players, flex_points_for_position))
            # Assign finishing places (ties share lowest place number)
            sorted_div = sorted(div_players, key=lambda x: x[1])
            i = 0
            while i < len(sorted_div):
                j = i
                while j < len(sorted_div) and sorted_div[j][1] == sorted_div[i][1]:
                    j += 1
                for k in range(i, j):
                    places[sorted_div[k][0]] = i + 1
                i = j
        tour_points[t['id']] = pts
        tour_places[t['id']] = places

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
            tid: {
                'points': p,
                'counted': (p is not None and tid in counted_ids),
                'place': tour_places.get(tid, {}).get(pid),
                'score': tour_scores.get(tid, {}).get(pid),
            }
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
    tour_scores = {}
    tour_places = {}
    for t in tournaments:
        rows = db.execute('''
            SELECT r.player_id, p.division, SUM(r.score) AS total_score
            FROM rounds r
            JOIN players p ON p.id = r.player_id
            WHERE r.tournament_id = ?
            GROUP BY r.player_id
        ''', (t['id'],)).fetchall()

        by_div = {'Men': [], 'Women': []}
        scores_map = {}
        for row in rows:
            div = row['division'] if row['division'] in by_div else 'Men'
            by_div[div].append((row['player_id'], row['total_score']))
            scores_map[row['player_id']] = row['total_score']

        tour_scores[t['id']] = scores_map

        pts = {}
        places = {}
        for div_players in by_div.values():
            pts.update(assign_league_points(div_players, travelers_points_for_position))
            sorted_div = sorted(div_players, key=lambda x: x[1])
            i = 0
            while i < len(sorted_div):
                j = i
                while j < len(sorted_div) and sorted_div[j][1] == sorted_div[i][1]:
                    j += 1
                for k in range(i, j):
                    places[sorted_div[k][0]] = i + 1
                i = j
        tour_points[t['id']] = pts
        tour_places[t['id']] = places

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
            tid: {
                'points': p,
                'counted': (p is not None and tid in counted_ids),
                'place': tour_places.get(tid, {}).get(pid),
                'score': tour_scores.get(tid, {}).get(pid),
            }
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

    # Migrate: rating_adjustment + address columns on courses
    ccols = [r[1] for r in db.execute("PRAGMA table_info(courses)").fetchall()]
    if 'rating_adjustment' not in ccols:
        db.execute("ALTER TABLE courses ADD COLUMN rating_adjustment REAL DEFAULT 0")
    if 'address' not in ccols:
        db.execute("ALTER TABLE courses ADD COLUMN address TEXT")
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


def generate_hole_labels(num_holes, scheme):
    """Return list of (hole_label, sort_order) for a given hole config."""
    if scheme == '1-9':
        return [(str(i), i) for i in range(1, 10)]
    if scheme == '1-18':
        return [(str(i), i) for i in range(1, 19)]
    if scheme == 'A1-A9':
        return [(f'A{i}', i) for i in range(1, 10)]
    if scheme == 'A1-B9':
        return [(f'A{i}', i) for i in range(1, 10)] + [(f'B{i}', i + 9) for i in range(1, 10)]
    return []


def delete_rounds_for_tournament(db, tournament_id):
    """Delete all rounds (and their hole scores) for a tournament."""
    round_ids = [r['id'] for r in db.execute(
        'SELECT id FROM rounds WHERE tournament_id = ?', (tournament_id,)
    ).fetchall()]
    if round_ids:
        db.execute('DELETE FROM hole_scores WHERE round_id IN ({})'.format(
            ','.join('?' * len(round_ids))), round_ids)
    db.execute('DELETE FROM rounds WHERE tournament_id = ?', (tournament_id,))


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

    # Pre-seed known players so the very first round can be calibrated.
    # Elite players seeded at 1000; mid-tier players seeded at 900.
    # These seeds are replaced by real computed ratings after their first round.
    seeds = {
        'Hobart Shaw': 1030.0,
        'Brandon Nihiser': 1030.0,
        'Taylor Junge': 1030.0,
        'Christine Shaw': 930.0,
        'Erin Spires': 930.0,
        'Frank Todaro': 930.0,
    }
    seed_rows = db.execute(
        'SELECT id, name FROM players WHERE name IN ({})'.format(
            ','.join('?' * len(seeds))
        ), list(seeds.keys())
    ).fetchall()
    player_ratings = {r['id']: seeds[r['name']] for r in seed_rows}
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
                scores_all  = [r['score'] for r in rated_rows]
                ratings_all = [player_ratings[r['player_id']] for r in rated_rows]
                avg_score_all  = sum(scores_all)  / len(scores_all)
                avg_rating_all = sum(ratings_all) / len(ratings_all)

                # Dynamic slope: split rated players into top/bottom halves by rating.
                # Need ≥5 rated players; otherwise fall back to fixed slope of 10.
                # Slope is clamped to [6, 15] to prevent runaway feedback — as player
                # ratings naturally diverge over time the raw rating spread grows faster
                # than the score spread, which would cause the slope (and all ratings)
                # to inflate without bound.
                if len(rated_rows) >= 5:
                    sorted_rated = sorted(rated_rows, key=lambda r: player_ratings[r['player_id']], reverse=True)
                    n = len(sorted_rated)
                    # Allow overlap at the midpoint so both halves always have ≥2 players
                    split = (n + 1) // 2
                    top_half    = sorted_rated[:split]
                    bottom_half = sorted_rated[n - split:]

                    avg_rating_top    = sum(player_ratings[r['player_id']] for r in top_half)    / len(top_half)
                    avg_score_top     = sum(r['score'] for r in top_half)    / len(top_half)
                    avg_rating_bottom = sum(player_ratings[r['player_id']] for r in bottom_half) / len(bottom_half)
                    avg_score_bottom  = sum(r['score'] for r in bottom_half) / len(bottom_half)

                    score_spread = avg_score_bottom - avg_score_top
                    if score_spread > 0:
                        raw_slope = (avg_rating_top - avg_rating_bottom) / score_spread
                        slope = max(6.0, min(15.0, raw_slope))
                    else:
                        slope = 10.0  # guard against zero spread
                else:
                    slope = 10.0

                # Anchor the 1000-scratch using all rated players + derived slope
                eff_scratch = avg_score_all + (avg_rating_all - 1000) / slope
                is_rated = True
            else:
                is_rated = False

            if is_rated:
                for r in round_rows:
                    round_rating = round(1000 + (eff_scratch - r['score']) * slope)
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
    peak_rating is rebuilt from scratch each time so deleting tournaments
    always produces accurate stats.
    """
    # Reset peak_rating so it reflects only actual current data
    db.execute("UPDATE players SET peak_rating = NULL")

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
        return redirect(url_for("leaderboard"))
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
    return redirect(url_for("leaderboard"))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('landing.html')


@app.route('/leaderboard')
def leaderboard():
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
        return redirect(url_for("leaderboard"))

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
        ORDER BY t.sort_date DESC, r.round_number DESC
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

    # Build chart data — oldest to newest (left → right on the trend line)
    chart_data = []
    for r in reversed(rounds):
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
        ORDER BY t.sort_date DESC
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
            'avg_score':   round(row['avg_score'], 1),
            'eff_scratch': round(eff, 1) if eff is not None else None,
            'is_nr':       row['rated_count'] == 0,
        }

    # ── Hole Details tab ────────────────────────────────────────────────────
    course_holes = db.execute(
        'SELECT * FROM course_holes WHERE course_id = ? ORDER BY sort_order',
        (t['course_id'],)
    ).fetchall()

    hole_score_rows = db.execute('''
        SELECT hs.hole_label, hs.score, r.round_number, r.player_id
        FROM hole_scores hs
        JOIN rounds r ON r.id = hs.round_id
        WHERE r.tournament_id = ?
    ''', (tournament_id,)).fetchall()

    has_hole_data = len(hole_score_rows) > 0

    # {round_number: {player_id: {hole_label: score}}}
    hole_scores_by_round = {}
    for hs in hole_score_rows:
        rn  = hs['round_number']
        pid = hs['player_id']
        hole_scores_by_round.setdefault(rn, {}).setdefault(pid, {})[hs['hole_label']] = hs['score']

    def _build_hole_stats(scores_subset, par):
        """Given a list of raw scores and a par value, return {avg, dist}."""
        if not scores_subset:
            return {'avg': None, 'dist': None}
        avg = round(sum(scores_subset) / len(scores_subset), 2)
        dist = None
        if par:
            buckets = dict(ace=0, albatross=0, eagle=0, birdie=0,
                           par=0, bogey=0, double=0, worse=0)
            for s in scores_subset:
                diff = s - par
                if s == 1:       buckets['ace']       += 1
                elif diff <= -3: buckets['albatross'] += 1
                elif diff == -2: buckets['eagle']     += 1
                elif diff == -1: buckets['birdie']    += 1
                elif diff == 0:  buckets['par']       += 1
                elif diff == 1:  buckets['bogey']     += 1
                elif diff == 2:  buckets['double']    += 1
                else:            buckets['worse']     += 1
            dist = {k: round(v / len(scores_subset) * 100, 1) for k, v in buckets.items()}
        return {'avg': avg, 'dist': dist}

    # Per-hole stats keyed by round: {round_number: {hole_label: {avg, dist}}}
    # Also overall event stats per hole (all rounds combined): {hole_label: {avg, dist}}
    round_hole_stats  = {}   # per-round, used in each round's table column
    tourn_hole_stats  = {}   # all-rounds combined, used in the event summary bar
    overall_event_dist = None  # single distribution across every hole score in event

    if has_hole_data:
        par_map = {h['hole_label']: h['par'] for h in course_holes}

        for h in course_holes:
            label = h['hole_label']
            par   = h['par']
            all_scores = [hs['score'] for hs in hole_score_rows if hs['hole_label'] == label]
            tourn_hole_stats[label] = _build_hole_stats(all_scores, par)

            for rn in round_numbers:
                rn_scores = [hs['score'] for hs in hole_score_rows
                             if hs['hole_label'] == label and hs['round_number'] == rn]
                round_hole_stats.setdefault(rn, {})[label] = _build_hole_stats(rn_scores, par)

        # Overall event distribution: bucket every hole score that has a known par
        all_scored = [(hs['score'], par_map.get(hs['hole_label'])) for hs in hole_score_rows
                      if par_map.get(hs['hole_label']) is not None]
        if all_scored:
            buckets = dict(ace=0, albatross=0, eagle=0, birdie=0,
                           par=0, bogey=0, double=0, worse=0)
            for s, par in all_scored:
                diff = s - par
                if s == 1:       buckets['ace']       += 1
                elif diff <= -3: buckets['albatross'] += 1
                elif diff == -2: buckets['eagle']     += 1
                elif diff == -1: buckets['birdie']    += 1
                elif diff == 0:  buckets['par']       += 1
                elif diff == 1:  buckets['bogey']     += 1
                elif diff == 2:  buckets['double']    += 1
                else:            buckets['worse']     += 1
            total = len(all_scored)
            overall_event_dist = {k: round(v / total * 100, 1) for k, v in buckets.items()}

    return render_template('tournament.html', t=t, results=results,
                           round_numbers=round_numbers, round_stats=round_stats,
                           course_holes=course_holes, has_hole_data=has_hole_data,
                           hole_scores_by_round=hole_scores_by_round,
                           tourn_hole_stats=tourn_hole_stats,
                           round_hole_stats=round_hole_stats,
                           overall_event_dist=overall_event_dist)


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

    # Most rounds played (all time)
    most_rounds_raw = db.execute('''
        SELECT p.id AS player_id, p.name, p.division,
               COUNT(r.id) AS num_rounds
        FROM rounds r
        JOIN players p ON p.id = r.player_id
        WHERE r.is_nr = 0
        GROUP BY p.id
        ORDER BY num_rounds DESC
    ''').fetchall()
    most_rounds = top1_by_div(most_rounds_raw)

    # Most 1000+ rated rounds (all time)
    elite_raw = db.execute('''
        SELECT p.id AS player_id, p.name, p.division,
               COUNT(r.id) AS elite_count
        FROM rounds r
        JOIN players p ON p.id = r.player_id
        WHERE r.rating >= 1000 AND r.is_nr = 0
        GROUP BY p.id
        ORDER BY elite_count DESC
    ''').fetchall()
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

        # Delete existing rounds (and hole scores) for this tournament, then re-insert from form
        delete_rounds_for_tournament(db, tournament_id)

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
        delete_rounds_for_tournament(db, tournament_id)
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
    return redirect(url_for("leaderboard"))


@app.route('/calculator')
@admin_required
def calculator():
    db = get_db()
    courses = db.execute('SELECT * FROM courses ORDER BY name').fetchall()
    return render_template('calculator.html', courses=courses)


# ── Courses ────────────────────────────────────────────────────────────────────

def _hole_score_distribution(db, course_id):
    """
    Return a dict of percentage breakdowns (ace, albatross, eagle, birdie, par,
    bogey, double, worse) for all hole scores recorded at this course where par
    is known.  Returns None if there is no data.
    """
    rows = db.execute('''
        SELECT hs.score, ch.par
        FROM hole_scores hs
        JOIN rounds r       ON r.id          = hs.round_id
        JOIN tournaments t  ON t.id          = r.tournament_id
        JOIN course_holes ch ON ch.course_id = t.course_id
                             AND ch.hole_label = hs.hole_label
        WHERE t.course_id = ? AND ch.par IS NOT NULL
    ''', (course_id,)).fetchall()

    if not rows:
        return None

    buckets = dict(ace=0, albatross=0, eagle=0, birdie=0,
                   par=0, bogey=0, double=0, worse=0)
    for row in rows:
        diff = row['score'] - row['par']
        if row['score'] == 1:
            buckets['ace'] += 1
        elif diff <= -3:
            buckets['albatross'] += 1
        elif diff == -2:
            buckets['eagle'] += 1
        elif diff == -1:
            buckets['birdie'] += 1
        elif diff == 0:
            buckets['par'] += 1
        elif diff == 1:
            buckets['bogey'] += 1
        elif diff == 2:
            buckets['double'] += 1
        else:
            buckets['worse'] += 1

    total = len(rows)
    return {k: round(v / total * 100, 1) for k, v in buckets.items()}


@app.route('/courses')
def courses():
    db = get_db()
    courses_raw = db.execute('SELECT * FROM courses ORDER BY name').fetchall()
    course_list = []
    for c in courses_raw:
        stats = db.execute('''
            SELECT COUNT(r.id) AS num_rounds,
                   MIN(r.score)  AS course_record,
                   AVG(r.score)  AS avg_score
            FROM rounds r
            JOIN tournaments t ON t.id = r.tournament_id
            WHERE t.course_id = ?
        ''', (c['id'],)).fetchone()
        holes = db.execute(
            'SELECT par, yards FROM course_holes WHERE course_id = ? ORDER BY sort_order',
            (c['id'],)
        ).fetchall()
        total_par   = sum(h['par']   for h in holes if h['par'])   or None
        total_yards = sum(h['yards'] for h in holes if h['yards']) or None
        course_list.append({
            'id': c['id'], 'name': c['name'], 'location': c['location'],
            'num_rounds':    stats['num_rounds'] or 0,
            'course_record': stats['course_record'],
            'avg_score':     round(stats['avg_score'], 1) if stats['avg_score'] else None,
            'total_par':     total_par,
            'total_yards':   total_yards,
            'num_holes':     len(holes),
            'dist':          _hole_score_distribution(db, c['id']),
        })
    return render_template('courses.html', courses=course_list)


@app.route('/courses/<int:course_id>')
def course(course_id):
    db = get_db()
    c = db.execute('SELECT * FROM courses WHERE id = ?', (course_id,)).fetchone()
    if not c:
        return redirect(url_for('courses'))

    holes = db.execute(
        'SELECT * FROM course_holes WHERE course_id = ? ORDER BY sort_order',
        (course_id,)
    ).fetchall()

    overall = db.execute('''
        SELECT COUNT(r.id) AS num_rounds,
               MIN(r.score)  AS record_score,
               AVG(r.score)  AS avg_score,
               MAX(r.rating) AS best_rating
        FROM rounds r JOIN tournaments t ON t.id = r.tournament_id
        WHERE t.course_id = ?
    ''', (course_id,)).fetchone()

    record_row = None
    if overall['record_score']:
        record_row = db.execute('''
            SELECT p.name, r.score, r.rating, r.round_number,
                   t.name AS tournament_name, t.date
            FROM rounds r
            JOIN players p ON p.id = r.player_id
            JOIN tournaments t ON t.id = r.tournament_id
            WHERE t.course_id = ? AND r.score = ?
            ORDER BY t.sort_date ASC LIMIT 1
        ''', (course_id, overall['record_score'])).fetchone()

    # Top 5 rated rounds ever at this course
    top_rounds = db.execute('''
        SELECT p.name AS player_name, r.score, r.rating, r.round_number,
               t.name AS tournament_name, t.date, t.id AS tournament_id
        FROM rounds r
        JOIN players p ON p.id = r.player_id
        JOIN tournaments t ON t.id = r.tournament_id
        WHERE t.course_id = ? AND r.is_nr = 0
        ORDER BY r.rating DESC LIMIT 5
    ''', (course_id,)).fetchall()

    tournaments = db.execute('''
        SELECT id, name, date, sort_date, league
        FROM tournaments WHERE course_id = ? ORDER BY sort_date DESC
    ''', (course_id,)).fetchall()

    # Hole-by-hole stats (aggregated across all rounds with hole scores at this course)
    hole_stats = {}
    for h in holes:
        row = db.execute('''
            SELECT COUNT(hs.score) AS cnt,
                   AVG(hs.score)   AS avg
            FROM hole_scores hs
            JOIN rounds r ON r.id = hs.round_id
            JOIN tournaments t ON t.id = r.tournament_id
            WHERE t.course_id = ? AND hs.hole_label = ?
        ''', (course_id, h['hole_label'])).fetchone()

        # Per-hole score distribution (relative to par if known)
        dist = None
        if h['par'] and (row['cnt'] or 0) > 0:
            score_rows = db.execute('''
                SELECT hs.score
                FROM hole_scores hs
                JOIN rounds r ON r.id = hs.round_id
                JOIN tournaments t ON t.id = r.tournament_id
                WHERE t.course_id = ? AND hs.hole_label = ?
            ''', (course_id, h['hole_label'])).fetchall()
            buckets = dict(ace=0, albatross=0, eagle=0, birdie=0,
                           par=0, bogey=0, double=0, worse=0)
            par = h['par']
            for sr in score_rows:
                diff = sr['score'] - par
                if sr['score'] == 1:
                    buckets['ace'] += 1
                elif diff <= -3:
                    buckets['albatross'] += 1
                elif diff == -2:
                    buckets['eagle'] += 1
                elif diff == -1:
                    buckets['birdie'] += 1
                elif diff == 0:
                    buckets['par'] += 1
                elif diff == 1:
                    buckets['bogey'] += 1
                elif diff == 2:
                    buckets['double'] += 1
                else:
                    buckets['worse'] += 1
            total = len(score_rows)
            dist = {k: round(v / total * 100, 1) for k, v in buckets.items()}

        hole_stats[h['hole_label']] = {
            'cnt':  row['cnt'] or 0,
            'avg':  round(row['avg'], 2) if row['avg'] else None,
            'dist': dist,
        }

    total_par   = sum(h['par']   for h in holes if h['par'])   or None
    total_yards = sum(h['yards'] for h in holes if h['yards']) or None

    return render_template('course.html',
        c=c, holes=holes, hole_stats=hole_stats,
        overall=overall, record_row=record_row, top_rounds=top_rounds,
        tournaments=tournaments, total_par=total_par, total_yards=total_yards)


@app.route('/courses/<int:course_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_course(course_id):
    db = get_db()
    c = db.execute('SELECT * FROM courses WHERE id = ?', (course_id,)).fetchone()
    if not c:
        return redirect(url_for('courses'))

    if request.method == 'POST':
        action = request.form.get('action', 'save_holes')

        if action == 'save_info':
            city    = request.form.get('city',    '').strip() or None
            address = request.form.get('address', '').strip() or None
            db.execute('UPDATE courses SET location = ?, address = ? WHERE id = ?',
                       (city, address, course_id))
            db.commit()
            flash('Course info saved.', 'success')

        elif action == 'configure':
            num_holes = int(request.form.get('num_holes', 18))
            scheme    = request.form.get('scheme', '1-18')
            db.execute('DELETE FROM course_holes WHERE course_id = ?', (course_id,))
            for label, sort_order in generate_hole_labels(num_holes, scheme):
                db.execute(
                    'INSERT INTO course_holes (course_id, hole_label, sort_order) VALUES (?, ?, ?)',
                    (course_id, label, sort_order)
                )
            db.commit()
            flash(f'Configured {num_holes} holes ({scheme}).', 'success')

        elif action == 'save_holes':
            for h in db.execute(
                'SELECT * FROM course_holes WHERE course_id = ? ORDER BY sort_order',
                (course_id,)
            ).fetchall():
                par_str   = request.form.get(f'par_{h["hole_label"]}',   '').strip()
                yards_str = request.form.get(f'yards_{h["hole_label"]}', '').strip()
                par   = int(par_str)   if par_str.isdigit()   else None
                yards = int(yards_str) if yards_str.isdigit() else None
                db.execute('UPDATE course_holes SET par = ?, yards = ? WHERE id = ?',
                           (par, yards, h['id']))
            db.commit()
            flash('Hole details saved.', 'success')

        return redirect(url_for('edit_course', course_id=course_id))

    holes = db.execute(
        'SELECT * FROM course_holes WHERE course_id = ? ORDER BY sort_order',
        (course_id,)
    ).fetchall()

    num_holes = len(holes)
    if holes:
        first = holes[0]['hole_label']
        if first.startswith('A'):
            scheme = 'A1-A9' if num_holes == 9 else 'A1-B9'
        else:
            scheme = '1-9' if num_holes == 9 else '1-18'
    else:
        num_holes, scheme = 18, '1-18'

    return render_template('edit_course.html',
        c=c, holes=holes, num_holes=num_holes, scheme=scheme)


@app.route('/tournament/<int:tournament_id>/hole-scores', methods=['GET', 'POST'])
@admin_required
def edit_hole_scores(tournament_id):
    db = get_db()
    t = db.execute('''
        SELECT t.*, c.name AS course_name, c.id AS course_id
        FROM tournaments t JOIN courses c ON c.id = t.course_id
        WHERE t.id = ?
    ''', (tournament_id,)).fetchone()
    if not t:
        return redirect(url_for('events'))

    holes = db.execute(
        'SELECT * FROM course_holes WHERE course_id = ? ORDER BY sort_order',
        (t['course_id'],)
    ).fetchall()

    rounds_raw = db.execute('''
        SELECT r.id AS round_id, r.round_number, r.score AS total_score,
               p.id AS player_id, p.name AS player_name
        FROM rounds r JOIN players p ON p.id = r.player_id
        WHERE r.tournament_id = ?
        ORDER BY r.round_number, p.name
    ''', (tournament_id,)).fetchall()

    rounds_by_num = {}
    for r in rounds_raw:
        rounds_by_num.setdefault(r['round_number'], []).append(r)

    # Existing hole scores keyed by {round_id: {hole_label: score}}
    existing_hs = {}
    if rounds_raw:
        rids = [r['round_id'] for r in rounds_raw]
        for hs in db.execute(
            'SELECT round_id, hole_label, score FROM hole_scores WHERE round_id IN ({})'.format(
                ','.join('?' * len(rids))), rids
        ).fetchall():
            existing_hs.setdefault(hs['round_id'], {})[hs['hole_label']] = hs['score']

    if request.method == 'POST':
        if rounds_raw:
            rids = [r['round_id'] for r in rounds_raw]
            db.execute('DELETE FROM hole_scores WHERE round_id IN ({})'.format(
                ','.join('?' * len(rids))), rids)
        inserted = 0
        for r in rounds_raw:
            for h in holes:
                val = request.form.get(f'hs_{r["round_id"]}_{h["hole_label"]}', '').strip()
                if val and val.isdigit():
                    db.execute(
                        'INSERT OR REPLACE INTO hole_scores (round_id, hole_label, score) VALUES (?, ?, ?)',
                        (r['round_id'], h['hole_label'], int(val))
                    )
                    inserted += 1
        db.commit()
        flash(f'Hole scores saved ({inserted} entries).', 'success')
        return redirect(url_for('edit_hole_scores', tournament_id=tournament_id))

    return render_template('edit_hole_scores.html',
        t=t, holes=holes, rounds_by_num=rounds_by_num, existing_hs=existing_hs)


# Initialize DB on startup whether running via gunicorn or directly
with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5001)
