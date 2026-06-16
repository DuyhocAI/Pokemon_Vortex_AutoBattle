import json
import sqlite3
from datetime import datetime
from pathlib import Path
from loguru import logger

DB_PATH = Path("data/memory.db")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS encounters (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp  TEXT    NOT NULL,
                pokemon    TEXT    NOT NULL,
                mode       TEXT    NOT NULL,
                result     TEXT    NOT NULL,  -- win | lose | caught | fled
                turns      INTEGER NOT NULL DEFAULT 0,
                moves_used TEXT    NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS move_stats (
                pokemon    TEXT NOT NULL,
                move_name  TEXT NOT NULL,
                wins       INTEGER NOT NULL DEFAULT 0,
                losses     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (pokemon, move_name)
            );

            CREATE INDEX IF NOT EXISTS idx_enc_pokemon ON encounters(pokemon);

            -- Pokedex cache: dữ liệu tra cứu từ internet (PokeAPI)
            CREATE TABLE IF NOT EXISTS pokedex (
                name       TEXT PRIMARY KEY,   -- tên base, lowercase
                data       TEXT NOT NULL,      -- JSON: types, stats, sprite
                fetched_at TEXT NOT NULL
            );

            -- Bài học rút ra sau mỗi trận thua (LLM tự phân tích)
            CREATE TABLE IF NOT EXISTS lessons (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                enemy     TEXT NOT NULL,
                lesson    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lessons_enemy ON lessons(enemy);
        """)
    logger.debug(f"Memory DB sẵn sàng: {DB_PATH}")


def log_encounter(
    pokemon: str,
    mode: str,
    result: str,
    turns: int,
    moves_used: list[str],
) -> None:
    """Ghi lại một encounter sau khi kết thúc."""
    pokemon = pokemon.strip() if pokemon else "Unknown"
    with _conn() as con:
        con.execute(
            "INSERT INTO encounters (timestamp, pokemon, mode, result, turns, moves_used) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), pokemon, mode, result, turns, json.dumps(moves_used)),
        )
        # Cập nhật move_stats
        for move in set(moves_used):
            con.execute(
                "INSERT INTO move_stats (pokemon, move_name, wins, losses) VALUES (?, ?, 0, 0) "
                "ON CONFLICT(pokemon, move_name) DO NOTHING",
                (pokemon, move),
            )
            if result in ("win", "caught"):
                con.execute(
                    "UPDATE move_stats SET wins = wins + 1 WHERE pokemon = ? AND move_name = ?",
                    (pokemon, move),
                )
            else:
                con.execute(
                    "UPDATE move_stats SET losses = losses + 1 WHERE pokemon = ? AND move_name = ?",
                    (pokemon, move),
                )


def get_context(pokemon: str, limit: int = 5) -> str:
    """Trả về chuỗi context về Pokemon này cho LLM prompt."""
    pokemon = pokemon.strip() if pokemon else "Unknown"
    with _conn() as con:
        # Tổng quan win/lose
        row = con.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN result IN ('win','caught') THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN result = 'caught' THEN 1 ELSE 0 END) as catches, "
            "AVG(turns) as avg_turns "
            "FROM encounters WHERE pokemon = ?",
            (pokemon,),
        ).fetchone()

        total = row["total"] or 0
        if total == 0:
            return f"No memory of {pokemon} yet."

        wins     = row["wins"] or 0
        catches  = row["catches"] or 0
        avg_trn  = round(row["avg_turns"] or 0, 1)

        # Move stats tốt nhất
        best_moves = con.execute(
            "SELECT move_name, wins, losses FROM move_stats "
            "WHERE pokemon = ? ORDER BY wins DESC, losses ASC LIMIT 3",
            (pokemon,),
        ).fetchall()

        # 5 trận gần nhất
        recent = con.execute(
            "SELECT result, turns, moves_used FROM encounters "
            "WHERE pokemon = ? ORDER BY id DESC LIMIT ?",
            (pokemon, limit),
        ).fetchall()

    lines = [
        f"Memory: {pokemon} encountered {total}x — {wins} wins, {catches} caught, avg {avg_trn} turns."
    ]

    if best_moves:
        mv_str = ", ".join(
            f"{r['move_name']}({r['wins']}W/{r['losses']}L)" for r in best_moves
        )
        lines.append(f"Best moves vs {pokemon}: {mv_str}")

    if recent:
        rec_str = " | ".join(
            f"{r['result']} in {r['turns']}t" for r in recent
        )
        lines.append(f"Recent: {rec_str}")

    # Bài học từ các trận thua trước (nếu có)
    lessons = get_lessons(pokemon, limit=3)
    for l in lessons:
        if l["enemy"] == pokemon:
            lines.append(f"LESSON (vs {l['enemy']}): {l['lesson']}")

    return "\n".join(lines)


def get_all_stats() -> list[dict]:
    """Trả về thống kê tổng hợp tất cả Pokemon đã gặp."""
    with _conn() as con:
        rows = con.execute(
            "SELECT pokemon, COUNT(*) as total, "
            "SUM(CASE WHEN result IN ('win','caught') THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN result = 'caught' THEN 1 ELSE 0 END) as catches "
            "FROM encounters GROUP BY pokemon ORDER BY total DESC",
        ).fetchall()
    return [dict(r) for r in rows]


# ── Thống kê tổng (lifetime) ────────────────────────────────────────────────

def get_summary() -> dict:
    """Thống kê toàn bộ lịch sử: thắng/thua/tỉ lệ, top đối thủ, đối thủ khó nhất."""
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) as battles, "
            "SUM(CASE WHEN result IN ('win','caught') THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN result = 'lose' THEN 1 ELSE 0 END) as losses, "
            "SUM(CASE WHEN result = 'caught' THEN 1 ELSE 0 END) as catches, "
            "SUM(CASE WHEN result = 'fled' THEN 1 ELSE 0 END) as fled "
            "FROM encounters",
        ).fetchone()

        top = con.execute(
            "SELECT pokemon, COUNT(*) as total, "
            "SUM(CASE WHEN result IN ('win','caught') THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN result = 'lose' THEN 1 ELSE 0 END) as losses "
            "FROM encounters GROUP BY pokemon ORDER BY total DESC LIMIT 10",
        ).fetchall()

        hardest = con.execute(
            "SELECT pokemon, COUNT(*) as total, "
            "SUM(CASE WHEN result = 'lose' THEN 1 ELSE 0 END) as losses "
            "FROM encounters GROUP BY pokemon "
            "HAVING losses > 0 ORDER BY losses DESC LIMIT 5",
        ).fetchall()

        recent = con.execute(
            "SELECT pokemon, result, timestamp FROM encounters ORDER BY id DESC LIMIT 12",
        ).fetchall()

    battles = row["battles"] or 0
    wins    = row["wins"] or 0
    losses  = row["losses"] or 0
    decided = wins + losses
    return {
        "battles":  battles,
        "wins":     wins,
        "losses":   losses,
        "catches":  row["catches"] or 0,
        "fled":     row["fled"] or 0,
        "winrate":  round(wins / decided * 100, 1) if decided else None,
        "top":      [dict(r) for r in top],
        "hardest":  [dict(r) for r in hardest],
        "recent":   [dict(r) for r in recent],
    }


# ── Pokedex cache (internet lookup) ─────────────────────────────────────────

def save_pokedex(name: str, data: dict) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO pokedex (name, data, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at",
            (name.lower().strip(), json.dumps(data, ensure_ascii=False), datetime.now().isoformat()),
        )


def get_pokedex(name: str, max_age_days: int = 30) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT data, fetched_at FROM pokedex WHERE name = ?",
            (name.lower().strip(),),
        ).fetchone()
    if not row:
        return None
    try:
        age = (datetime.now() - datetime.fromisoformat(row["fetched_at"])).days
        if age > max_age_days:
            return None
        return json.loads(row["data"])
    except Exception:
        return None


# ── Lessons: học từ các trận thua ───────────────────────────────────────────

def add_lesson(enemy: str, lesson: str) -> None:
    enemy = (enemy or "Unknown").strip()
    lesson = (lesson or "").strip()
    if not lesson:
        return
    with _conn() as con:
        con.execute(
            "INSERT INTO lessons (timestamp, enemy, lesson) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), enemy, lesson),
        )
    logger.info(f"[LEARN] Bài học mới vs {enemy}: {lesson[:70]}")


def get_lessons(enemy: str | None = None, limit: int = 5) -> list[dict]:
    """Lấy bài học — ưu tiên bài học về đúng đối thủ, kèm bài học chung gần nhất."""
    with _conn() as con:
        if enemy:
            rows = con.execute(
                "SELECT enemy, lesson, timestamp FROM lessons "
                "WHERE enemy = ? ORDER BY id DESC LIMIT ?",
                (enemy.strip(), limit),
            ).fetchall()
            if len(rows) < limit:
                extra = con.execute(
                    "SELECT enemy, lesson, timestamp FROM lessons "
                    "WHERE enemy != ? ORDER BY id DESC LIMIT ?",
                    (enemy.strip(), limit - len(rows)),
                ).fetchall()
                rows = list(rows) + list(extra)
        else:
            rows = con.execute(
                "SELECT enemy, lesson, timestamp FROM lessons ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]
