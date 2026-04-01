#!/usr/bin/env python3
"""
Roster Alert — runs via GitHub Actions every 15 min during game hours.
1. Checks MLB schedule for today's first game time.
2. If current time is within the check window (first pitch - 1h5m ± 25min),
   and we haven't already sent today, runs a roster check against ESPN Fantasy.
3. Sends an Ntfy push notification with the result.
4. Writes status/last_check.json so the local dashboard can show step results.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta

import requests
from espn_api.baseball import League

# ── Config ────────────────────────────────────────────────────────────────────
MAX_ROSTER = 26
WINDOW_MINUTES = 25          # ±25 min of target time
LEAD_TIME = timedelta(hours=1, minutes=5)
ET = timezone(timedelta(hours=-4))


# ── Step 1: MLB Schedule ─────────────────────────────────────────────────────

def get_first_game_today() -> datetime | None:
    """Return the UTC datetime of today's earliest MLB game, or None on off days."""
    today_et = datetime.now(ET).strftime("%Y-%m-%d")
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={today_et}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    games = []
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            gt = game.get("gameDate")
            if gt:
                games.append(datetime.fromisoformat(gt.replace("Z", "+00:00")))

    return min(games) if games else None


# ── Step 2: Roster Check ─────────────────────────────────────────────────────

def check_rosters() -> tuple[list[dict], list[dict]]:
    """Return (teams_data, violations)."""
    league = League(
        league_id=int(os.environ["LEAGUE_ID"]),
        year=int(os.environ.get("YEAR", "2026")),
        espn_s2=os.environ["ESPN_S2"],
        swid=os.environ["ESPN_SWID"],
    )

    teams_data = []
    over_limit_teams = []

    for team in league.teams:
        active = [p for p in team.roster if p.lineupSlot != "IL"]
        count = len(active)
        over = count > MAX_ROSTER
        teams_data.append({"name": team.team_name, "count": count, "over_limit": over})
        if over:
            over_limit_teams.append(team)

    teams_data.sort(key=lambda t: t["name"])

    violations = []
    if over_limit_teams:
        activities = league.recent_activity(size=200)
        ADD_ACTIONS = {"FA ADDED", "WAIVER ADDED", "TRADED"}

        for team in over_limit_teams:
            team_count = next(t["count"] for t in teams_data if t["name"] == team.team_name)
            num_to_show = team_count - MAX_ROSTER  # 27 → 1, 28 → 2, etc.
            recent_adds = []
            for activity in activities:
                for act_team, action, player in activity.actions:
                    if (
                        hasattr(act_team, "team_name")
                        and act_team.team_name == team.team_name
                        and action in ADD_ACTIONS
                        and player
                    ):
                        dt = datetime.fromtimestamp(activity.date / 1000, tz=timezone.utc)
                        recent_adds.append({
                            "player": str(player),
                            "date_str": dt.strftime("%-m/%-d/%Y"),
                            "type": action,
                        })
                        break
                if len(recent_adds) >= num_to_show:
                    break

            violations.append({"team": team.team_name, "count": team_count, "recent_adds": recent_adds})

    return teams_data, violations


# ── Step 3: Ntfy Push ─────────────────────────────────────────────────────────

def send_ntfy(topic: str, message: str, title: str, tags: str) -> None:
    resp = requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Tags": tags},
        timeout=10,
    )
    resp.raise_for_status()


# ── Status file ───────────────────────────────────────────────────────────────

def already_sent_today() -> bool:
    """Return True if we already sent a notification today (UTC date)."""
    try:
        with open("status/last_check.json") as f:
            data = json.load(f)
        if data.get("result") in ("all_clear", "violations"):
            ts = data.get("timestamp")
            if ts:
                last_dt = datetime.fromisoformat(ts)
                return last_dt.date() == datetime.now(timezone.utc).date()
    except Exception:
        pass
    return False


def write_status(steps: dict, result: str) -> None:
    status = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "result": result,
        "steps": steps,
    }
    os.makedirs("status", exist_ok=True)
    with open("status/last_check.json", "w") as f:
        json.dump(status, f, indent=2)
    print(json.dumps(status, indent=2))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ntfy_topic = os.environ["NTFY_TOPIC"]

    steps = {
        "schedule_check": {"status": "pending", "detail": ""},
        "roster_check":   {"status": "skipped", "detail": ""},
        "ntfy_alert":     {"status": "skipped", "detail": ""},
    }

    # ── Step 1: MLB schedule ──────────────────────────────────────────────
    try:
        first_game = get_first_game_today()
        if first_game is None:
            steps["schedule_check"] = {"status": "success", "detail": "Off day — no games scheduled"}
            write_status(steps, "off_day")
            print("Off day — no games today.")
            return 0

        game_str = first_game.astimezone(ET).strftime("%-I:%M %p ET")
        steps["schedule_check"] = {"status": "success", "detail": f"First pitch: {game_str}"}
    except Exception as e:
        steps["schedule_check"] = {"status": "error", "detail": str(e)}
        write_status(steps, "error")
        return 1

    # ── Window check ──────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    target = first_game - LEAD_TIME
    diff_min = abs((now - target).total_seconds()) / 60

    if diff_min > WINDOW_MINUTES:
        print(f"Not in window. Target: {target.isoformat()}, diff: {diff_min:.0f}min")
        return 0  # silent exit — don't write status for skipped runs

    if already_sent_today():
        print("Already sent today — skipping duplicate.")
        return 0

    print(f"In window (diff: {diff_min:.1f}min). Running roster check.")

    # ── Step 2: Roster check ──────────────────────────────────────────────
    try:
        teams_data, violations = check_rosters()
        all_clear = len(violations) == 0
        steps["roster_check"] = {
            "status": "success",
            "detail": f"All clear — {len(teams_data)} teams checked" if all_clear
                      else f"{len(violations)} team(s) over limit",
            "teams": teams_data,
            "violations": violations,
            "all_clear": all_clear,
        }
    except Exception as e:
        steps["roster_check"] = {"status": "error", "detail": str(e)}
        write_status(steps, "error")
        return 1

    # ── Step 3: Send notification ─────────────────────────────────────────
    try:
        if all_clear:
            title = "Roster Check - All Clear"
            max_name = max(len(t["name"]) for t in teams_data)
            pad = max_name + 2
            lines = []
            lines.append(f"{'#':>2}  {'TEAM':<{pad}} PLAYERS")
            lines.append("-" * (pad + 14))
            for i, t in enumerate(teams_data, 1):
                lines.append(f"{i:>2}  {t['name']:<{pad}} {t['count']:>2}")
            lines.append(f"\nFirst pitch: {game_str}")
            msg = "\n".join(lines)
            tags = "white_check_mark,baseball"
        else:
            title = f"Roster Check - {len(violations)} Over Limit"
            max_name = max(len(t["name"]) for t in teams_data)
            pad = max_name + 2
            lines = []
            lines.append(f"{'#':>2}  {'TEAM':<{pad}} PLAYERS")
            lines.append("-" * (pad + 14))
            for i, t in enumerate(teams_data, 1):
                marker = " !!!" if t["over_limit"] else ""
                lines.append(f"{i:>2}  {t['name']:<{pad}} {t['count']:>2}{marker}")
            for v in violations:
                lines.append("")
                lines.append(f"Latest adds for {v['team']}:")
                for add in v.get("recent_adds", []):
                    lines.append(f"  {add['player']} - {add['type']} {add['date_str']}")
            lines.append(f"\nFirst pitch: {game_str}")
            msg = "\n".join(lines)
            tags = "biohazard,baseball"

        send_ntfy(ntfy_topic, msg, title, tags)
        steps["ntfy_alert"] = {"status": "success", "detail": "Notification sent"}
    except Exception as e:
        steps["ntfy_alert"] = {"status": "error", "detail": str(e)}
        write_status(steps, "error")
        return 1

    write_status(steps, "all_clear" if all_clear else "violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
