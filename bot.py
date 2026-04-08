import logging
import os
import re
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from threading import Thread
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import discord
import psycopg2
import pytz
from discord.ext import commands
from discord.ui import Button, Modal, Select, TextInput, View
from flask import Flask


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("volt-bot")


def load_local_env():
    base_dir = Path(__file__).resolve().parent
    for env_name in (".env", ".env.txt"):
        env_path = base_dir / env_name
        if not env_path.exists():
            continue

        for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

        logger.info("Loaded local environment from %s", env_path.name)
        break


load_local_env()


app = Flask(__name__)


@app.route("/")
def home():
    return "VOLT System is Online!"


def run_web_server():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), use_reloader=False)


def keep_alive():
    Thread(target=run_web_server, daemon=True).start()


DATABASE_URL = os.getenv("DATABASE_URL")
DB_SSLMODE = os.getenv("DB_SSLMODE")
TOKEN = os.getenv("DISCORD_TOKEN")
KST = pytz.timezone("Asia/Seoul")

TIER_ORDER = [
    "아이언",
    "브론즈",
    "실버",
    "골드",
    "플래티넘",
    "에메랄드",
    "다이아몬드",
    "마스터",
    "그랜드마스터",
    "챌린저",
]
TIER_DATA = {tier: index + 1 for index, tier in enumerate(TIER_ORDER)}
POSITIONS = ["탑", "정글", "미드", "원딜", "서폿"]
SUB_POSITIONS = [*POSITIONS, "상관없음"]
POINTS_WIN = 10
POINTS_ACTIVITY = 10
MATCH_CAPACITY = 10
MAX_SELECT_OPTIONS = 25
MENTION_RE = re.compile(r"^<@!?(\d+)>$")


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


ALLOW_DUPLICATE_SIGNUPS = env_flag("ALLOW_DUPLICATE_SIGNUPS", default=True)


def now_kst() -> str:
    return discord.utils.utcnow().astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")


def normalize_database_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    parsed = urlparse(url)
    if not parsed.scheme:
        return url

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    sslmode = DB_SSLMODE or ("require" if "supabase" in parsed.netloc.lower() else None)
    if sslmode and "sslmode" not in query:
        query["sslmode"] = sslmode
        parsed = parsed._replace(query=urlencode(query))
        return urlunparse(parsed)

    return url


def get_db_conn():
    url = normalize_database_url(DATABASE_URL)
    if not url:
        logger.error("DATABASE_URL is not configured.")
        return None

    try:
        return psycopg2.connect(url, connect_timeout=5)
    except Exception:
        logger.exception("Failed to connect to database.")
        return None


def init_db():
    conn = get_db_conn()
    if not conn:
        logger.warning("Skipping DB initialization because connection is unavailable.")
        return False

    try:
        with closing(conn.cursor()) as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS volt_rank (
                    user_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    points INTEGER DEFAULT 0,
                    activity_points INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                ALTER TABLE volt_rank
                ADD COLUMN IF NOT EXISTS name TEXT
                """
            )
            cur.execute(
                """
                ALTER TABLE volt_rank
                ADD COLUMN IF NOT EXISTS wins INTEGER DEFAULT 0
                """
            )
            cur.execute(
                """
                ALTER TABLE volt_rank
                ADD COLUMN IF NOT EXISTS losses INTEGER DEFAULT 0
                """
            )
            cur.execute(
                """
                ALTER TABLE volt_rank
                ADD COLUMN IF NOT EXISTS points INTEGER DEFAULT 0
                """
            )
            cur.execute(
                """
                ALTER TABLE volt_rank
                ADD COLUMN IF NOT EXISTS activity_points INTEGER DEFAULT 0
                """
            )
            cur.execute(
                """
                ALTER TABLE volt_rank
                ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS volt_operator_access (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    granted_by TEXT,
                    granted_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )
            cur.execute("UPDATE volt_rank SET wins = COALESCE(wins, 0)")
            cur.execute("UPDATE volt_rank SET losses = COALESCE(losses, 0)")
            cur.execute("UPDATE volt_rank SET points = COALESCE(points, 0)")
            cur.execute("UPDATE volt_rank SET activity_points = COALESCE(activity_points, 0)")
            cur.execute("UPDATE volt_rank SET updated_at = COALESCE(updated_at, NOW())")
        conn.commit()
        load_operator_access(conn)
        return True
    finally:
        conn.close()


@dataclass
class PlayerEntry:
    entry_id: str
    user_id: int
    name: str
    tier: str
    main: str
    sub: str


@dataclass
class Match:
    match_id: int
    title: str
    waiting_list: Dict[str, PlayerEntry] = field(default_factory=dict)
    created_at: str = field(default_factory=now_kst)
    announcement_channel_id: Optional[int] = None
    announcement_message_id: Optional[int] = None

    def has_user(self, user_id: int) -> bool:
        return any(player.user_id == user_id for player in self.waiting_list.values())

    def get_entry_id_by_user(self, user_id: int) -> Optional[str]:
        for entry_id, player in self.waiting_list.items():
            if player.user_id == user_id:
                return entry_id
        return None

    def list_players(self) -> List[PlayerEntry]:
        return list(self.waiting_list.values())

    def unique_users(self) -> List[Tuple[int, str]]:
        seen = set()
        users: List[Tuple[int, str]] = []
        for player in self.waiting_list.values():
            if player.user_id in seen:
                continue
            seen.add(player.user_id)
            users.append((player.user_id, player.name))
        return users


class MatchManager:
    def __init__(self):
        self.matches: Dict[int, Match] = {}
        self.last_teams: Dict[int, Dict[str, List[Dict[str, object]]]] = {}

    def list_matches(self) -> List[Match]:
        return [self.matches[key] for key in sorted(self.matches.keys())]

    def next_match_id(self) -> int:
        next_id = 1
        while next_id in self.matches:
            next_id += 1
        return next_id

    def create_match(self, title: str) -> int:
        match_id = self.next_match_id()
        self.matches[match_id] = Match(match_id=match_id, title=title)
        return match_id

    def get_match(self, match_id: int) -> Optional[Match]:
        return self.matches.get(match_id)

    def close_match(self, match_id: int) -> bool:
        removed = self.matches.pop(match_id, None)
        self.last_teams.pop(match_id, None)
        return removed is not None

    def register_player(self, match_id: int, user_id: int, name: str, tier: str, main: str, sub: str):
        match = self.get_match(match_id)
        if not match:
            return False, "이미 종료되었거나 존재하지 않는 내전입니다."
        if not ALLOW_DUPLICATE_SIGNUPS and match.has_user(user_id):
            return False, "이미 이 내전에 신청되어 있습니다."
        if len(match.waiting_list) >= MATCH_CAPACITY:
            return False, "정원이 가득 찼습니다."

        entry_id = str(uuid.uuid4())
        match.waiting_list[entry_id] = PlayerEntry(
            entry_id=entry_id,
            user_id=user_id,
            name=name,
            tier=tier,
            main=main,
            sub=sub,
        )
        return True, f"{name}님 신청이 완료되었습니다."

    def unregister_player(self, match_id: int, user_id: int):
        match = self.get_match(match_id)
        if not match:
            return False, "존재하지 않는 내전입니다."
        entry_id = match.get_entry_id_by_user(user_id)
        if not entry_id:
            return False, "이 내전에 신청된 기록이 없습니다."
        removed_name = match.waiting_list.pop(entry_id).name
        return True, f"{removed_name}님의 신청이 취소되었습니다."


manager = MatchManager()
operator_access: Dict[int, set[int]] = {}


def load_operator_access(conn=None):
    should_close = conn is None
    if conn is None:
        conn = get_db_conn()
    if not conn:
        return False

    loaded: Dict[int, set[int]] = {}
    try:
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT guild_id, user_id FROM volt_operator_access")
            for guild_id_raw, user_id_raw in cur.fetchall():
                guild_id = int(guild_id_raw)
                user_id = int(user_id_raw)
                loaded.setdefault(guild_id, set()).add(user_id)
        operator_access.clear()
        operator_access.update(loaded)
        return True
    finally:
        if should_close:
            conn.close()


def list_operator_ids(guild_id: int) -> List[int]:
    return sorted(operator_access.get(guild_id, set()))


def grant_operator_access(guild_id: int, user_id: int, granted_by: int):
    conn = get_db_conn()
    if not conn:
        return False, "DB 연결에 실패해 운영권한을 저장하지 못했습니다."

    try:
        with closing(conn.cursor()) as cur:
            cur.execute(
                """
                INSERT INTO volt_operator_access (guild_id, user_id, granted_by, granted_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    granted_by = EXCLUDED.granted_by,
                    granted_at = NOW()
                """,
                (str(guild_id), str(user_id), str(granted_by)),
            )
        conn.commit()
    except Exception:
        logger.exception("Failed to grant operator access for guild %s user %s", guild_id, user_id)
        try:
            conn.rollback()
        except Exception:
            pass
        return False, "운영권한 저장 중 오류가 발생했습니다."
    finally:
        conn.close()

    operator_access.setdefault(guild_id, set()).add(user_id)
    return True, "운영권한을 부여했습니다."


def revoke_operator_access(guild_id: int, user_id: int):
    conn = get_db_conn()
    if not conn:
        return False, "DB 연결에 실패해 운영권한을 회수하지 못했습니다."

    try:
        with closing(conn.cursor()) as cur:
            cur.execute(
                "DELETE FROM volt_operator_access WHERE guild_id = %s AND user_id = %s",
                (str(guild_id), str(user_id)),
            )
            removed = cur.rowcount > 0
        conn.commit()
    except Exception:
        logger.exception("Failed to revoke operator access for guild %s user %s", guild_id, user_id)
        try:
            conn.rollback()
        except Exception:
            pass
        return False, "운영권한 회수 중 오류가 발생했습니다."
    finally:
        conn.close()

    operator_access.setdefault(guild_id, set()).discard(user_id)
    if not removed:
        return False, "부여된 운영권한 기록이 없습니다."
    return True, "운영권한을 회수했습니다."


def is_discord_admin(member: Optional[discord.abc.User]) -> bool:
    guild_permissions = getattr(member, "guild_permissions", None)
    return bool(guild_permissions and guild_permissions.administrator)


def has_control_access(member: Optional[discord.abc.User]) -> bool:
    if member is None:
        return False
    if is_discord_admin(member):
        return True
    guild = getattr(member, "guild", None)
    if guild is None:
        return False
    return member.id in operator_access.get(guild.id, set())


def access_level_label(member: Optional[discord.abc.User]) -> str:
    if is_discord_admin(member):
        return "서버 관리자"
    if has_control_access(member):
        return "봇 운영진"
    return "일반 유저"


async def require_control_access_interaction(interaction: discord.Interaction, feature_name: str = "이 기능") -> bool:
    if has_control_access(interaction.user):
        return True
    await interaction.response.send_message(
        f"{feature_name}은 서버 관리자 또는 봇 운영권한이 있는 멤버만 사용할 수 있습니다.",
        ephemeral=True,
    )
    return False


async def require_discord_admin_interaction(interaction: discord.Interaction, feature_name: str = "이 기능") -> bool:
    if is_discord_admin(interaction.user):
        return True
    await interaction.response.send_message(
        f"{feature_name}은 서버 관리자만 사용할 수 있습니다.",
        ephemeral=True,
    )
    return False


async def require_control_access_ctx(ctx: commands.Context, feature_name: str = "이 기능") -> bool:
    if ctx.guild is None:
        await ctx.send(f"{feature_name}은 서버 채널에서만 사용할 수 있습니다.")
        return False
    if has_control_access(ctx.author):
        return True
    await ctx.send(f"{feature_name}은 서버 관리자 또는 봇 운영권한이 있는 멤버만 사용할 수 있습니다.")
    return False


async def require_discord_admin_ctx(ctx: commands.Context, feature_name: str = "이 기능") -> bool:
    if ctx.guild is None:
        await ctx.send(f"{feature_name}은 서버 채널에서만 사용할 수 있습니다.")
        return False
    if is_discord_admin(ctx.author):
        return True
    await ctx.send(f"{feature_name}은 서버 관리자만 사용할 수 있습니다.")
    return False


def format_player(player: PlayerEntry) -> str:
    return f"{player.name} [{player.tier}] ({player.main}/{player.sub})"


def format_button_label(player: PlayerEntry) -> str:
    return f"[{player.tier}] {player.name} | {player.main}/{player.sub}"[:80]


def build_draft_pool(players: List[PlayerEntry], captain1_id: int, captain2_id: int) -> List[PlayerEntry]:
    if not ALLOW_DUPLICATE_SIGNUPS:
        return [player for player in players if player.user_id not in {captain1_id, captain2_id}]

    removed = set()
    draft_pool = []
    for player in players:
        if player.user_id == captain1_id and captain1_id not in removed:
            removed.add(captain1_id)
            continue
        if player.user_id == captain2_id and captain2_id not in removed:
            removed.add(captain2_id)
            continue
        draft_pool.append(player)
    return draft_pool


async def resolve_member_from_text(guild: Optional[discord.Guild], raw: str) -> Optional[discord.Member]:
    if guild is None:
        return None

    value = raw.strip()
    match = MENTION_RE.match(value)
    member_id = int(match.group(1)) if match else int(value) if value.isdigit() else None

    if member_id is not None:
        member = guild.get_member(member_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(member_id)
        except discord.HTTPException:
            return None

    lowered = value.casefold()
    for member in guild.members:
        candidates = [member.display_name, member.name, member.global_name]
        if any(candidate and candidate.casefold() == lowered for candidate in candidates):
            return member
    return None


def build_main_panel_embed(user: discord.abc.User, can_manage: bool) -> discord.Embed:
    embed = discord.Embed(
        title="VOLT 컨트롤 패널",
        description=(
            f"{user.display_name}님, 아래 패널에서 내전 기능을 바로 조작할 수 있습니다.\n"
            "시작 명령어는 `!1`이며, 유저 기능과 운영 기능을 한 화면에서 열 수 있습니다."
        ),
        color=0x5865F2,
    )
    embed.add_field(name="현재 권한", value=access_level_label(user), inline=True)
    embed.add_field(name="열린 내전", value=f"{len(manager.list_matches())}개", inline=True)
    embed.add_field(
        name="유저 기능",
        value="참여 신청, 신청 취소, 내전 목록, 명단 보기, 랭킹 조회, 점수 확인",
        inline=False,
    )
    embed.add_field(
        name="운영 기능",
        value="내전 생성, 명단 수정, 공지 새로고침, 드래프트, 결과 기록, 내전 삭제, 운영권한 관리",
        inline=False,
    )
    embed.set_footer(
        text=(
            "운영 패널 접근 가능"
            if can_manage
            else "운영 패널은 서버 관리자 또는 봇 운영권한이 있는 멤버만 사용할 수 있습니다."
        )
    )
    return embed


def build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="VOLT 사용 안내",
        description="대부분의 기능은 `!1` 패널에서 버튼과 선택 메뉴로 처리할 수 있습니다.",
        color=0x2ECC71,
    )
    embed.add_field(
        name="1. 유저 사용 흐름",
        value="`!1` → 참여 신청 / 신청 취소 / 명단 보기 / 랭킹",
        inline=False,
    )
    embed.add_field(
        name="2. 운영 사용 흐름",
        value="`!1` → 운영 패널 → 내전 생성 또는 내전 관리 → 명단 수정 / 드래프트 / 결과 기록 / 삭제",
        inline=False,
    )
    embed.add_field(
        name="3. 운영권한",
        value="서버 관리자는 일반 멤버에게 봇 운영권한을 부여하거나 회수할 수 있습니다.",
        inline=False,
    )
    embed.add_field(
        name="백업 명령어",
        value="`!내전생성`, `!운영권한부여`, `!드래프트`, `!결과기록` 같은 텍스트 명령어도 계속 사용할 수 있습니다.",
        inline=False,
    )
    embed.set_footer(text="테스트 중에는 ALLOW_DUPLICATE_SIGNUPS=true, 정식 출시 때는 false")
    return embed


def build_operator_access_embed(guild: Optional[discord.Guild]) -> discord.Embed:
    embed = discord.Embed(
        title="운영권한 관리",
        description="서버 관리자 권한이 없는 멤버에게 봇 운영 패널 접근 권한을 부여하거나 회수할 수 있습니다.",
        color=0x8E44AD,
    )
    if guild is None:
        embed.add_field(name="현재 상태", value="서버 정보를 찾을 수 없습니다.", inline=False)
        return embed

    operator_lines = []
    for user_id in list_operator_ids(guild.id):
        member = guild.get_member(user_id)
        operator_lines.append(member.mention if member else f"`{user_id}`")

    embed.add_field(
        name="현재 위임된 운영진",
        value="\n".join(operator_lines[:20]) if operator_lines else "현재 위임된 운영진이 없습니다.",
        inline=False,
    )
    embed.add_field(
        name="권한 범위",
        value="운영 패널 접근, 내전 생성/수정/삭제, 드래프트, 결과 기록",
        inline=False,
    )
    embed.set_footer(text="서버 관리자는 항상 전체 권한을 가지며, 이 화면은 서버 관리자만 열 수 있습니다.")
    return embed


def build_admin_panel_embed(member: Optional[discord.abc.User]) -> discord.Embed:
    guild = getattr(member, "guild", None)
    operator_count = len(list_operator_ids(guild.id)) if guild else 0
    embed = discord.Embed(
        title="VOLT 운영 패널",
        description="내전 생성부터 공지 관리, 드래프트, 결과 기록, 운영권한 관리까지 여기서 처리할 수 있습니다.",
        color=0xE67E22,
    )
    embed.add_field(name="현재 권한", value=access_level_label(member), inline=True)
    embed.add_field(name="위임 운영진", value=f"{operator_count}명", inline=True)
    embed.add_field(
        name="빠른 작업",
        value="내전 생성, 내전 관리, 운영권한 관리, 목록 확인, 도움말",
        inline=False,
    )
    embed.set_footer(text="서버 관리자는 운영권한 부여/회수 가능 | 위임 운영진도 내전 운영 기능 사용 가능")
    return embed


def build_score_embed() -> discord.Embed:
    embed = discord.Embed(title="VOLT 점수 안내", color=0xF1C40F)
    embed.add_field(name="승리 점수", value=f"{POINTS_WIN}점", inline=True)
    embed.add_field(name="참여 점수", value=f"{POINTS_ACTIVITY}점", inline=True)
    embed.add_field(name="종합 점수", value="승리 점수 + 참여 점수", inline=False)
    embed.set_footer(text="랭킹 패널에서 개인/전체 랭킹을 바로 조회할 수 있습니다.")
    return embed


def build_match_list_embed(matches: List[Match]) -> discord.Embed:
    embed = discord.Embed(title="진행 중인 내전 목록", color=0x3498DB)
    if not matches:
        embed.description = "현재 진행 중인 내전이 없습니다."
        return embed

    embed.description = "\n".join(
        f"• `{match.match_id}`번 | {match.title} | {len(match.waiting_list)}/{MATCH_CAPACITY}명 | 남은 자리 {MATCH_CAPACITY - len(match.waiting_list)}"
        for match in matches
    )
    embed.set_footer(text="삭제된 번호는 다음 생성 시 자동으로 재사용됩니다.")
    return embed


def build_match_embed(match: Match) -> discord.Embed:
    embed = discord.Embed(
        title=f"{match.title} 명단",
        description=f"방 번호: `{match.match_id}` | 생성 시간: {match.created_at}",
        color=0x3498DB,
    )
    players = match.list_players()
    if not players:
        embed.add_field(name="참여자", value="아직 참여자가 없습니다.", inline=False)
    else:
        embed.add_field(
            name=f"참여자 ({len(players)}/{MATCH_CAPACITY})",
            value="\n".join(f"• {format_player(player)}" for player in players),
            inline=False,
        )
    return embed


def build_manage_embed(match: Match) -> discord.Embed:
    embed = discord.Embed(
        title=f"운영 패널 | {match.title}",
        description=(
            f"방 번호: `{match.match_id}` | 현재 인원: {len(match.waiting_list)}/{MATCH_CAPACITY}\n"
            f"남은 자리: {MATCH_CAPACITY - len(match.waiting_list)}"
        ),
        color=0xE67E22,
    )
    embed.add_field(
        name="관리 기능",
        value="명단 보기, 명단 수정, 공지 새로고침, 드래프트 시작, 결과 기록, 내전 삭제",
        inline=False,
    )
    embed.add_field(
        name="운영 팁",
        value="드래프트는 정확히 10명이 모였을 때 시작되고, 결과 기록은 드래프트 완료 후 사용할 수 있습니다.",
        inline=False,
    )
    if match.list_players():
        preview = "\n".join(f"• {player.name} [{player.tier}] ({player.main}/{player.sub})" for player in match.list_players()[:10])
        embed.add_field(name="현재 명단", value=preview, inline=False)
    else:
        embed.add_field(name="현재 명단", value="아직 참여자가 없습니다.", inline=False)
    return embed

def build_match_announcement_content(match: Match) -> str:
    status = "모집 마감" if len(match.waiting_list) >= MATCH_CAPACITY else "모집 중"
    return f"@here 내전 모집이 시작되었습니다. [{status}]"


def build_match_announcement_embed(match: Match) -> discord.Embed:
    is_full = len(match.waiting_list) >= MATCH_CAPACITY
    embed = discord.Embed(
        title=f"내전 모집 | {match.title}",
        description=(
            f"방 번호: `{match.match_id}`\n"
            f"생성 시간: {match.created_at}\n"
            + (
                "현재 정원이 가득 차 모집이 마감되었습니다."
                if is_full
                else "아래 버튼으로 바로 신청, 취소, 명단 확인이 가능합니다."
            )
        ),
        color=0x5865F2,
    )
    players = match.list_players()
    if players:
        preview = "\n".join(f"• {player.name} [{player.tier}] ({player.main}/{player.sub})" for player in players[:10])
        embed.add_field(name=f"현재 참가자 ({len(players)}/{MATCH_CAPACITY})", value=preview, inline=False)
    else:
        embed.add_field(name=f"현재 참가자 (0/{MATCH_CAPACITY})", value="아직 신청자가 없습니다.", inline=False)

    footer = "테스트 모드: 중복 신청 허용 중" if ALLOW_DUPLICATE_SIGNUPS else "중복 신청은 자동으로 막힙니다."
    if is_full:
        footer += " | 취소가 나오면 다시 열립니다."
    embed.set_footer(text=footer)
    return embed


async def close_match_announcement(match: Match, content: str):
    if not match.announcement_channel_id or not match.announcement_message_id:
        return

    channel = bot.get_channel(match.announcement_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(match.announcement_channel_id)
        except Exception:
            logger.exception("Failed to fetch announcement channel for match %s", match.match_id)
            return

    try:
        message = await channel.fetch_message(match.announcement_message_id)
        await message.edit(content=content, embed=build_match_announcement_embed(match), view=None)
    except Exception:
        logger.exception("Failed to close announcement message for match %s", match.match_id)


async def refresh_match_announcement(match_id: int):
    match = manager.get_match(match_id)
    if not match or not match.announcement_channel_id or not match.announcement_message_id:
        return

    channel = bot.get_channel(match.announcement_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(match.announcement_channel_id)
        except Exception:
            logger.exception("Failed to fetch announcement channel for match %s", match_id)
            return

    try:
        message = await channel.fetch_message(match.announcement_message_id)
        await message.edit(
            content=build_match_announcement_content(match),
            embed=build_match_announcement_embed(match),
            view=MatchAnnouncementView(match_id),
        )
    except Exception:
        logger.exception("Failed to refresh announcement message for match %s", match_id)


async def record_match_result(match_id: int, win_team: int):
    if win_team not in (1, 2):
        return False, "승리 팀 번호는 1 또는 2만 입력할 수 있습니다."

    teams = manager.last_teams.get(match_id)
    if not teams:
        return False, "기록할 팀 데이터가 없습니다. 먼저 드래프트를 완료해주세요."

    conn = get_db_conn()
    if not conn:
        return False, "DB 연결 실패로 결과를 기록하지 못했습니다. Render의 DATABASE_URL 설정을 확인해주세요."

    lose_team = 2 if win_team == 1 else 1
    try:
        with closing(conn.cursor()) as cur:
            for player in teams[f"team{win_team}"]:
                cur.execute(
                    """
                    INSERT INTO volt_rank (user_id, name, wins, points, activity_points, updated_at)
                    VALUES (%s, %s, 1, %s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        wins = volt_rank.wins + 1,
                        points = volt_rank.points + EXCLUDED.points,
                        activity_points = volt_rank.activity_points + EXCLUDED.activity_points,
                        updated_at = NOW()
                    """,
                    (str(player["user_id"]), player["name"], POINTS_WIN, POINTS_ACTIVITY),
                )

            for player in teams[f"team{lose_team}"]:
                cur.execute(
                    """
                    INSERT INTO volt_rank (user_id, name, losses, activity_points, updated_at)
                    VALUES (%s, %s, 1, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        losses = volt_rank.losses + 1,
                        activity_points = volt_rank.activity_points + EXCLUDED.activity_points,
                        updated_at = NOW()
                    """,
                    (str(player["user_id"]), player["name"], POINTS_ACTIVITY),
                )
        conn.commit()
        return True, "결과 기록이 완료되었습니다."
    except Exception as exc:
        logger.exception("Failed to record match result for match %s", match_id)
        try:
            conn.rollback()
        except Exception:
            pass
        return False, f"DB 저장 중 오류가 발생했습니다. `{type(exc).__name__}`"
    finally:
        conn.close()


class RankSelectView(View):
    def __init__(self, is_all: bool):
        super().__init__(timeout=120)
        self.is_all = is_all

    async def process_rank(self, interaction: discord.Interaction, sort_col: str, label: str):
        conn = get_db_conn()
        if not conn:
            await interaction.response.edit_message(content="DB 연결에 실패했습니다. 잠시 후 다시 시도해주세요.", view=None)
            return

        order_by = sort_col if sort_col != "total" else "(points + activity_points)"
        try:
            with closing(conn.cursor()) as cur:
                cur.execute(
                    f"""
                    SELECT user_id, name, points, activity_points, wins, losses
                    FROM volt_rank
                    ORDER BY {order_by} DESC, wins DESC, name ASC
                    """
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            await interaction.response.edit_message(content="아직 기록된 데이터가 없습니다.", view=None)
            return

        if self.is_all:
            lines = []
            for index, row in enumerate(rows, start=1):
                value = row[2] if sort_col == "points" else row[3] if sort_col == "activity_points" else row[2] + row[3]
                lines.append(f"**{index}위 {row[1]}** | {value}pt")
            embed = discord.Embed(
                title=f"VOLT 전체 랭킹 ({label})",
                description="\n".join(lines[:25]),
                color=0xFFD700,
            )
        else:
            user_row = next(((index, row) for index, row in enumerate(rows, start=1) if row[0] == str(interaction.user.id)), None)
            if not user_row:
                await interaction.response.edit_message(content="해당 부문의 기록이 없습니다.", view=None)
                return

            rank_index, row = user_row
            total_points = row[2] + row[3]
            total_games = row[4] + row[5]
            win_rate = (row[4] / total_games * 100) if total_games else 0

            embed = discord.Embed(title=f"{row[1]}님의 랭킹 정보", color=0x3498DB)
            embed.add_field(name=f"{label} 순위", value=f"**{rank_index}위**", inline=True)
            embed.add_field(name="종합 점수", value=f"**{total_points}pt**", inline=True)
            embed.add_field(name="상세 점수", value=f"승리: {row[2]}pt / 참여: {row[3]}pt", inline=False)
            embed.add_field(name="전적", value=f"{row[4]}승 {row[5]}패 (승률 {win_rate:.1f}%)", inline=False)

        await interaction.response.edit_message(content=None, embed=embed, view=None)

    @discord.ui.button(label="승리 점수", style=discord.ButtonStyle.success, row=0)
    async def victory_rank(self, interaction: discord.Interaction, button: Button):
        await self.process_rank(interaction, "points", "승리 점수")

    @discord.ui.button(label="참여 점수", style=discord.ButtonStyle.primary, row=0)
    async def activity_rank(self, interaction: discord.Interaction, button: Button):
        await self.process_rank(interaction, "activity_points", "참여 점수")

    @discord.ui.button(label="종합 점수", style=discord.ButtonStyle.secondary, row=0)
    async def total_rank(self, interaction: discord.Interaction, button: Button):
        await self.process_rank(interaction, "total", "종합 점수")


class PositionSelectView(View):
    def __init__(self, match_id: int, tier: str):
        super().__init__(timeout=180)
        self.match_id = match_id
        self.tier = tier
        self.main_position: Optional[str] = None

    @discord.ui.select(
        placeholder="주 라인을 선택하세요.",
        options=[discord.SelectOption(label=lane, value=lane) for lane in POSITIONS],
    )
    async def main_callback(self, interaction: discord.Interaction, select: Select):
        self.main_position = select.values[0]
        self.clear_items()
        sub_select = Select(
            placeholder="부 라인을 선택하세요.",
            options=[discord.SelectOption(label=lane, value=lane) for lane in SUB_POSITIONS],
        )
        sub_select.callback = self.final_callback
        self.add_item(sub_select)
        await interaction.response.edit_message(content="부 라인을 선택하세요.", view=self)

    async def final_callback(self, interaction: discord.Interaction):
        success, message = manager.register_player(
            self.match_id,
            interaction.user.id,
            interaction.user.display_name,
            self.tier,
            self.main_position or POSITIONS[0],
            interaction.data["values"][0],
        )
        if success:
            await refresh_match_announcement(self.match_id)
        await interaction.response.edit_message(content=message, view=None)


class TierSelectView(View):
    def __init__(self, match_id: int):
        super().__init__(timeout=120)
        self.match_id = match_id

    @discord.ui.select(
        placeholder="티어를 선택하세요.",
        options=[discord.SelectOption(label=tier, value=tier) for tier in TIER_ORDER],
    )
    async def tier_callback(self, interaction: discord.Interaction, select: Select):
        await interaction.response.send_message(
            "라인을 선택해주세요.",
            view=PositionSelectView(self.match_id, select.values[0]),
            ephemeral=True,
        )


class EditListView(View):
    def __init__(self, match_id: int):
        super().__init__(timeout=300)
        self.match_id = match_id
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        match = manager.get_match(self.match_id)
        if not match:
            return
        for player in match.list_players():
            button = Button(
                label=f"제외 {player.name}"[:80],
                style=discord.ButtonStyle.danger,
                custom_id=player.entry_id,
            )
            button.callback = self.delete_player
            self.add_item(button)

    async def delete_player(self, interaction: discord.Interaction):
        if not await require_control_access_interaction(interaction, "명단 수정"):
            return
        match = manager.get_match(self.match_id)
        entry_id = interaction.data["custom_id"]
        if not match or entry_id not in match.waiting_list:
            await interaction.response.edit_message(content="이미 제거되었거나 존재하지 않는 참가자입니다.", view=None)
            return

        removed_name = match.waiting_list.pop(entry_id).name
        await refresh_match_announcement(self.match_id)
        self.update_buttons()
        if self.children:
            await interaction.response.edit_message(content=f"{removed_name}님을 명단에서 제외했습니다.", view=self)
        else:
            await interaction.response.edit_message(content=f"{removed_name}님을 명단에서 제외했습니다. 현재 참가자가 없습니다.", view=None)


class DraftView(View):
    def __init__(self, match_id: int, captain1: discord.Member, captain2: discord.Member, players: List[PlayerEntry]):
        super().__init__(timeout=900)
        self.match_id = match_id
        self.captains = [captain1, captain2]
        self.players = players
        self.teams: List[List[PlayerEntry]] = [[], []]
        self.pick_seq = [0, 1, 1, 0, 0, 1, 1, 0]
        self.step = 0
        self.update_buttons()

    def make_embed(self) -> discord.Embed:
        match = manager.get_match(self.match_id)
        title = match.title if match else f"{self.match_id}번 내전"
        embed = discord.Embed(title=f"{title} 드래프트", color=0x5865F2)
        team1 = [f"캡틴: **{self.captains[0].display_name}**"] + [f"• {format_player(player)}" for player in self.teams[0]]
        team2 = [f"캡틴: **{self.captains[1].display_name}**"] + [f"• {format_player(player)}" for player in self.teams[1]]
        embed.add_field(name="1팀", value="\n".join(team1), inline=True)
        embed.add_field(name="2팀", value="\n".join(team2), inline=True)
        if self.step < len(self.pick_seq):
            embed.set_footer(text=f"{self.captains[self.pick_seq[self.step]].display_name}님의 차례입니다.")
        else:
            embed.set_footer(text="드래프트가 완료되었습니다.")
        return embed

    def update_buttons(self):
        self.clear_items()
        picked_ids = {player.entry_id for player in self.teams[0] + self.teams[1]}
        for index, player in enumerate(self.players):
            if player.entry_id in picked_ids:
                continue
            button = Button(
                label=format_button_label(player),
                style=discord.ButtonStyle.secondary,
                custom_id=str(index),
            )
            button.callback = self.pick_callback
            self.add_item(button)

    async def pick_callback(self, interaction: discord.Interaction):
        current_captain = self.captains[self.pick_seq[self.step]]
        if interaction.user.id != current_captain.id:
            await interaction.response.send_message("지금은 본인 차례가 아닙니다.", ephemeral=True)
            return

        selected_player = self.players[int(interaction.data["custom_id"])]
        if any(selected_player.entry_id == player.entry_id for player in self.teams[0] + self.teams[1]):
            await interaction.response.send_message("이미 선택된 플레이어입니다.", ephemeral=True)
            return

        self.teams[self.pick_seq[self.step]].append(selected_player)
        self.step += 1

        if self.step >= len(self.pick_seq):
            team1 = [{"name": self.captains[0].display_name, "user_id": self.captains[0].id}] + [
                {"name": player.name, "user_id": player.user_id} for player in self.teams[0]
            ]
            team2 = [{"name": self.captains[1].display_name, "user_id": self.captains[1].id}] + [
                {"name": player.name, "user_id": player.user_id} for player in self.teams[1]
            ]
            manager.last_teams[self.match_id] = {"team1": team1, "team2": team2}
            await interaction.response.edit_message(content="드래프트가 완료되었습니다.", embed=self.make_embed(), view=None)
            return

        self.update_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

class MatchPickerView(View):
    def __init__(self, mode: str, user_id: Optional[int] = None):
        super().__init__(timeout=180)
        self.mode = mode
        matches = manager.list_matches()
        if mode == "cancel" and user_id is not None:
            matches = [match for match in matches if match.has_user(user_id)]

        options = [
            discord.SelectOption(
                label=f"[{match.match_id}] {match.title} ({len(match.waiting_list)}/{MATCH_CAPACITY})",
                value=str(match.match_id),
            )
            for match in matches[:MAX_SELECT_OPTIONS]
        ]

        if not options:
            placeholder = {
                "join": "참여할 내전이 없습니다.",
                "cancel": "취소할 신청 내역이 없습니다.",
                "view": "조회할 내전이 없습니다.",
                "manage": "관리할 내전이 없습니다.",
            }[mode]
            select = Select(
                placeholder=placeholder,
                options=[discord.SelectOption(label="선택 가능한 내전이 없습니다.", value="0")],
                disabled=True,
            )
            self.add_item(select)
            return

        placeholder = {
            "join": "참여할 내전을 선택하세요.",
            "cancel": "신청 취소할 내전을 선택하세요.",
            "view": "명단을 볼 내전을 선택하세요.",
            "manage": "관리할 내전을 선택하세요.",
        }[mode]
        select = Select(placeholder=placeholder, options=options)
        select.callback = self.handle_select
        self.add_item(select)

    async def handle_select(self, interaction: discord.Interaction):
        match_id = int(interaction.data["values"][0])
        match = manager.get_match(match_id)
        if not match:
            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
            return

        if self.mode == "join":
            await interaction.response.send_message(
                f"`{match_id}`번 내전 신청을 진행합니다. 티어를 선택해주세요.",
                view=TierSelectView(match_id),
                ephemeral=True,
            )
            return

        if self.mode == "cancel":
            success, message = manager.unregister_player(match_id, interaction.user.id)
            if success:
                await refresh_match_announcement(match_id)
            await interaction.response.send_message(message, ephemeral=True)
            return

        if self.mode == "view":
            await interaction.response.send_message(embed=build_match_embed(match), ephemeral=True)
            return

        if not await require_control_access_interaction(interaction, "내전 관리"):
            return
        await interaction.response.send_message(
            embed=build_manage_embed(match),
            view=MatchManageView(match_id),
            ephemeral=True,
        )


class ConfirmDeleteMatchView(View):
    def __init__(self, match_id: int):
        super().__init__(timeout=120)
        self.match_id = match_id

    @discord.ui.button(label="삭제 확인", style=discord.ButtonStyle.danger, row=0)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        if not await require_control_access_interaction(interaction, "내전 삭제"):
            return
        match = manager.get_match(self.match_id)
        if not match:
            await interaction.response.edit_message(content="이미 삭제된 내전입니다.", view=None)
            return
        await close_match_announcement(match, "이 내전 모집은 종료되었습니다.")
        manager.close_match(self.match_id)
        await interaction.response.edit_message(content=f"`{self.match_id}`번 내전을 삭제했습니다.", view=None)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary, row=0)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(content="내전 삭제를 취소했습니다.", view=None)


class CaptainOneSelectView(View):
    def __init__(self, match_id: int):
        super().__init__(timeout=180)
        self.match_id = match_id
        match = manager.get_match(match_id)
        users = match.unique_users() if match else []
        options = [discord.SelectOption(label=name[:100], value=str(user_id)) for user_id, name in users[:MAX_SELECT_OPTIONS]]
        select = Select(
            placeholder="첫 번째 캡틴을 선택하세요.",
            options=options or [discord.SelectOption(label="선택 가능한 인원이 없습니다.", value="0")],
            disabled=not options,
        )
        select.callback = self.select_first
        self.add_item(select)

    async def select_first(self, interaction: discord.Interaction):
        if not await require_control_access_interaction(interaction, "드래프트"):
            return
        captain1_id = int(interaction.data["values"][0])
        await interaction.response.send_message(
            "두 번째 캡틴을 선택하세요.",
            view=CaptainTwoSelectView(self.match_id, captain1_id),
            ephemeral=True,
        )


class CaptainTwoSelectView(View):
    def __init__(self, match_id: int, captain1_id: int):
        super().__init__(timeout=180)
        self.match_id = match_id
        self.captain1_id = captain1_id
        match = manager.get_match(match_id)
        users = [user for user in match.unique_users() if user[0] != captain1_id] if match else []
        options = [discord.SelectOption(label=name[:100], value=str(user_id)) for user_id, name in users[:MAX_SELECT_OPTIONS]]
        select = Select(
            placeholder="두 번째 캡틴을 선택하세요.",
            options=options or [discord.SelectOption(label="선택 가능한 인원이 없습니다.", value="0")],
            disabled=not options,
        )
        select.callback = self.select_second
        self.add_item(select)

    async def select_second(self, interaction: discord.Interaction):
        if not await require_control_access_interaction(interaction, "드래프트"):
            return
        match = manager.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
            return

        captain2_id = int(interaction.data["values"][0])
        if captain2_id == self.captain1_id:
            await interaction.response.send_message("캡틴 두 명은 서로 달라야 합니다.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("서버 정보를 찾을 수 없습니다.", ephemeral=True)
            return

        captain1 = guild.get_member(self.captain1_id)
        captain2 = guild.get_member(captain2_id)
        if captain1 is None:
            try:
                captain1 = await guild.fetch_member(self.captain1_id)
            except discord.HTTPException:
                captain1 = None
        if captain2 is None:
            try:
                captain2 = await guild.fetch_member(captain2_id)
            except discord.HTTPException:
                captain2 = None
        if captain1 is None or captain2 is None:
            await interaction.response.send_message("캡틴 정보를 불러오지 못했습니다.", ephemeral=True)
            return

        all_players = match.list_players()
        if len(all_players) != MATCH_CAPACITY:
            await interaction.response.send_message(
                f"드래프트는 정확히 {MATCH_CAPACITY}명이 모였을 때만 시작할 수 있습니다. 현재 {len(all_players)}명입니다.",
                ephemeral=True,
            )
            return

        draft_pool = build_draft_pool(all_players, captain1.id, captain2.id)
        if len(draft_pool) != 8:
            await interaction.response.send_message(
                f"드래프트 대상 인원 수가 올바르지 않습니다. 현재 드래프트 가능 인원은 {len(draft_pool)}명입니다.",
                ephemeral=True,
            )
            return

        draft_view = DraftView(self.match_id, captain1, captain2, draft_pool)
        await interaction.response.send_message(
            f"{captain1.display_name}님과 {captain2.display_name}님의 드래프트를 시작합니다.",
            embed=draft_view.make_embed(),
            view=draft_view,
        )


class ResultTeamSelectView(View):
    def __init__(self, match_id: int):
        super().__init__(timeout=180)
        self.match_id = match_id

    @discord.ui.button(label="1팀 승리", style=discord.ButtonStyle.success, row=0)
    async def team1_win(self, interaction: discord.Interaction, button: Button):
        await self.record(interaction, 1)

    @discord.ui.button(label="2팀 승리", style=discord.ButtonStyle.success, row=0)
    async def team2_win(self, interaction: discord.Interaction, button: Button):
        await self.record(interaction, 2)

    async def record(self, interaction: discord.Interaction, win_team: int):
        if not await require_control_access_interaction(interaction, "결과 기록"):
            return
        success, message = await record_match_result(self.match_id, win_team)
        if success:
            await interaction.response.send_message(message, view=MatchManageView(self.match_id))
        else:
            await interaction.response.send_message(message, ephemeral=True)


class MatchManageView(View):
    def __init__(self, match_id: int):
        super().__init__(timeout=300)
        self.match_id = match_id

    @discord.ui.button(label="명단 보기", style=discord.ButtonStyle.secondary, row=0)
    async def view_roster(self, interaction: discord.Interaction, button: Button):
        if not await require_control_access_interaction(interaction, "명단 보기"):
            return
        match = manager.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
            return
        await interaction.response.send_message(embed=build_match_embed(match), ephemeral=True)

    @discord.ui.button(label="명단 수정", style=discord.ButtonStyle.primary, row=0)
    async def edit_roster(self, interaction: discord.Interaction, button: Button):
        if not await require_control_access_interaction(interaction, "명단 수정"):
            return
        match = manager.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
            return
        if not match.list_players():
            await interaction.response.send_message("현재 참가자가 없어 수정할 명단이 없습니다.", ephemeral=True)
            return
        await interaction.response.send_message("제외할 인원을 누르세요.", view=EditListView(self.match_id), ephemeral=True)

    @discord.ui.button(label="공지 새로고침", style=discord.ButtonStyle.secondary, row=0)
    async def refresh_notice(self, interaction: discord.Interaction, button: Button):
        if not await require_control_access_interaction(interaction, "공지 새로고침"):
            return
        match = manager.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
            return
        await refresh_match_announcement(self.match_id)
        await interaction.response.send_message("공지 메시지를 새로고침했습니다.", ephemeral=True)

    @discord.ui.button(label="드래프트", style=discord.ButtonStyle.success, row=1)
    async def start_draft(self, interaction: discord.Interaction, button: Button):
        if not await require_control_access_interaction(interaction, "드래프트"):
            return
        match = manager.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
            return
        if len(match.unique_users()) < 2:
            await interaction.response.send_message("캡틴으로 선택할 수 있는 인원이 부족합니다.", ephemeral=True)
            return
        await interaction.response.send_message("첫 번째 캡틴을 선택하세요.", view=CaptainOneSelectView(self.match_id), ephemeral=True)

    @discord.ui.button(label="결과 기록", style=discord.ButtonStyle.success, row=1)
    async def record_result(self, interaction: discord.Interaction, button: Button):
        if not await require_control_access_interaction(interaction, "결과 기록"):
            return
        if self.match_id not in manager.last_teams:
            await interaction.response.send_message("먼저 드래프트를 완료해주세요.", ephemeral=True)
            return
        await interaction.response.send_message("승리 팀을 선택하세요.", view=ResultTeamSelectView(self.match_id), ephemeral=True)

    @discord.ui.button(label="내전 삭제", style=discord.ButtonStyle.danger, row=1)
    async def delete_match(self, interaction: discord.Interaction, button: Button):
        if not await require_control_access_interaction(interaction, "내전 삭제"):
            return
        await interaction.response.send_message(
            f"`{self.match_id}`번 내전을 삭제할까요?",
            view=ConfirmDeleteMatchView(self.match_id),
            ephemeral=True,
        )


def eligible_operator_grant_members(guild: Optional[discord.Guild]) -> List[discord.Member]:
    if guild is None:
        return []
    candidates = [
        member
        for member in guild.members
        if not member.bot and not has_control_access(member)
    ]
    return sorted(candidates, key=lambda member: member.display_name.casefold())


def eligible_operator_revoke_members(guild: Optional[discord.Guild]) -> List[discord.Member]:
    if guild is None:
        return []
    candidates = [
        member
        for member in guild.members
        if not member.bot and not is_discord_admin(member) and member.id in operator_access.get(guild.id, set())
    ]
    return sorted(candidates, key=lambda member: member.display_name.casefold())


class OperatorGrantSelectView(View):
    def __init__(self, guild: Optional[discord.Guild]):
        super().__init__(timeout=180)
        members = eligible_operator_grant_members(guild)
        options = [
            discord.SelectOption(label=member.display_name[:100], value=str(member.id), description=member.name[:100])
            for member in members[:MAX_SELECT_OPTIONS]
        ]
        select = Select(
            placeholder="운영권한을 부여할 멤버를 선택하세요.",
            options=options or [discord.SelectOption(label="부여 가능한 멤버가 없습니다.", value="0")],
            disabled=not options,
        )
        select.callback = self.handle_select
        self.add_item(select)

    async def handle_select(self, interaction: discord.Interaction):
        if not await require_discord_admin_interaction(interaction, "운영권한 부여"):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("서버 정보를 찾을 수 없습니다.", ephemeral=True)
            return
        target_id = int(interaction.data["values"][0])
        member = guild.get_member(target_id)
        if member is None:
            try:
                member = await guild.fetch_member(target_id)
            except discord.HTTPException:
                member = None
        if member is None:
            await interaction.response.send_message("대상 멤버를 찾지 못했습니다.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("봇 계정에는 운영권한을 부여할 수 없습니다.", ephemeral=True)
            return
        if has_control_access(member):
            await interaction.response.send_message("이미 운영 패널을 사용할 수 있는 멤버입니다.", ephemeral=True)
            return
        success, message = grant_operator_access(guild.id, member.id, interaction.user.id)
        await interaction.response.edit_message(
            content=f"{member.mention} {message}" if success else message,
            embed=build_operator_access_embed(guild),
            view=None,
        )


class OperatorRevokeSelectView(View):
    def __init__(self, guild: Optional[discord.Guild]):
        super().__init__(timeout=180)
        members = eligible_operator_revoke_members(guild)
        options = [
            discord.SelectOption(label=member.display_name[:100], value=str(member.id), description=member.name[:100])
            for member in members[:MAX_SELECT_OPTIONS]
        ]
        select = Select(
            placeholder="운영권한을 회수할 멤버를 선택하세요.",
            options=options or [discord.SelectOption(label="회수할 운영진이 없습니다.", value="0")],
            disabled=not options,
        )
        select.callback = self.handle_select
        self.add_item(select)

    async def handle_select(self, interaction: discord.Interaction):
        if not await require_discord_admin_interaction(interaction, "운영권한 회수"):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("서버 정보를 찾을 수 없습니다.", ephemeral=True)
            return
        target_id = int(interaction.data["values"][0])
        member = guild.get_member(target_id)
        if member is None:
            try:
                member = await guild.fetch_member(target_id)
            except discord.HTTPException:
                member = None
        success, message = revoke_operator_access(guild.id, target_id)
        target_label = member.mention if member else f"`{target_id}`"
        await interaction.response.edit_message(
            content=f"{target_label} {message}" if success else message,
            embed=build_operator_access_embed(guild),
            view=None,
        )


class OperatorAccessView(View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="권한 부여", style=discord.ButtonStyle.success, row=0)
    async def grant_access(self, interaction: discord.Interaction, button: Button):
        if not await require_discord_admin_interaction(interaction, "운영권한 부여"):
            return
        await interaction.response.send_message(
            "운영권한을 부여할 멤버를 선택하세요.",
            view=OperatorGrantSelectView(interaction.guild),
            ephemeral=True,
        )

    @discord.ui.button(label="권한 회수", style=discord.ButtonStyle.danger, row=0)
    async def revoke_access(self, interaction: discord.Interaction, button: Button):
        if not await require_discord_admin_interaction(interaction, "운영권한 회수"):
            return
        await interaction.response.send_message(
            "운영권한을 회수할 멤버를 선택하세요.",
            view=OperatorRevokeSelectView(interaction.guild),
            ephemeral=True,
        )

    @discord.ui.button(label="권한 목록", style=discord.ButtonStyle.secondary, row=0)
    async def list_access(self, interaction: discord.Interaction, button: Button):
        if not await require_discord_admin_interaction(interaction, "운영권한 목록"):
            return
        await interaction.response.send_message(embed=build_operator_access_embed(interaction.guild), ephemeral=True)


class MatchAnnouncementView(View):
    def __init__(self, match_id: int):
        super().__init__(timeout=1800)
        self.match_id = match_id
        match = manager.get_match(match_id)
        if match and len(match.waiting_list) >= MATCH_CAPACITY:
            self.join_match.disabled = True

    @discord.ui.button(label="참여하기", style=discord.ButtonStyle.success, row=0)
    async def join_match(self, interaction: discord.Interaction, button: Button):
        match = manager.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
            return
        if len(match.waiting_list) >= MATCH_CAPACITY:
            await interaction.response.send_message("정원이 가득 찼습니다.", ephemeral=True)
            return
        if not ALLOW_DUPLICATE_SIGNUPS and match.has_user(interaction.user.id):
            await interaction.response.send_message("이미 이 내전에 신청되어 있습니다.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"`{self.match_id}`번 내전 신청을 진행합니다. 티어를 선택해주세요.",
            view=TierSelectView(self.match_id),
            ephemeral=True,
        )

    @discord.ui.button(label="신청취소", style=discord.ButtonStyle.danger, row=0)
    async def cancel_match(self, interaction: discord.Interaction, button: Button):
        success, message = manager.unregister_player(self.match_id, interaction.user.id)
        if success:
            await refresh_match_announcement(self.match_id)
        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label="명단 보기", style=discord.ButtonStyle.secondary, row=0)
    async def view_match(self, interaction: discord.Interaction, button: Button):
        match = manager.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
            return
        await interaction.response.send_message(embed=build_match_embed(match), ephemeral=True)

    @discord.ui.button(label="운영 메뉴", style=discord.ButtonStyle.primary, row=1)
    async def manage_match(self, interaction: discord.Interaction, button: Button):
        if not await require_control_access_interaction(interaction, "운영 메뉴"):
            return
        match = manager.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
            return
        await interaction.response.send_message(embed=build_manage_embed(match), view=MatchManageView(self.match_id), ephemeral=True)

class CreateMatchModal(Modal, title="내전 생성"):
    title_input = TextInput(label="내전 제목", placeholder="예: 저녁 8시 내전", max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_control_access_interaction(interaction, "내전 생성"):
            return
        title = str(self.title_input).strip()
        if not title:
            await interaction.response.send_message("내전 제목을 입력해주세요.", ephemeral=True)
            return
        if interaction.channel is None:
            await interaction.response.send_message("채널 정보를 찾을 수 없습니다.", ephemeral=True)
            return

        match_id = manager.create_match(title)
        match = manager.get_match(match_id)
        if not match:
            await interaction.response.send_message("내전 생성 중 오류가 발생했습니다.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        announcement = await interaction.channel.send(
            content=build_match_announcement_content(match),
            embed=build_match_announcement_embed(match),
            view=MatchAnnouncementView(match_id),
        )
        match.announcement_channel_id = announcement.channel.id
        match.announcement_message_id = announcement.id
        await interaction.followup.send(f"`{match_id}`번 내전 공지를 생성했습니다.", ephemeral=True)


class AdminPanelView(View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="내전 생성", style=discord.ButtonStyle.success, row=0)
    async def create_match(self, interaction: discord.Interaction, button: Button):
        if not await require_control_access_interaction(interaction, "내전 생성"):
            return
        await interaction.response.send_modal(CreateMatchModal())

    @discord.ui.button(label="내전 관리", style=discord.ButtonStyle.primary, row=0)
    async def manage_match(self, interaction: discord.Interaction, button: Button):
        if not await require_control_access_interaction(interaction, "내전 관리"):
            return
        await interaction.response.send_message("관리할 내전을 선택하세요.", view=MatchPickerView("manage"), ephemeral=True)

    @discord.ui.button(label="권한 관리", style=discord.ButtonStyle.primary, row=1)
    async def manage_access(self, interaction: discord.Interaction, button: Button):
        if not await require_discord_admin_interaction(interaction, "운영권한 관리"):
            return
        await interaction.response.send_message(
            embed=build_operator_access_embed(interaction.guild),
            view=OperatorAccessView(),
            ephemeral=True,
        )

    @discord.ui.button(label="내전 목록", style=discord.ButtonStyle.secondary, row=1)
    async def list_matches(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(embed=build_match_list_embed(manager.list_matches()), ephemeral=True)

    @discord.ui.button(label="도움말", style=discord.ButtonStyle.secondary, row=1)
    async def show_help(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(embed=build_help_embed(), ephemeral=True)


class RankMenuView(View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="내 랭킹", style=discord.ButtonStyle.primary, row=0)
    async def my_rank(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("조회 기준을 선택하세요.", view=RankSelectView(is_all=False), ephemeral=True)

    @discord.ui.button(label="전체 랭킹", style=discord.ButtonStyle.primary, row=0)
    async def all_rank(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("정렬 기준을 선택하세요.", view=RankSelectView(is_all=True), ephemeral=True)

    @discord.ui.button(label="점수표", style=discord.ButtonStyle.secondary, row=0)
    async def score_info(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(embed=build_score_embed(), ephemeral=True)


class MainPanelView(View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="참여 신청", style=discord.ButtonStyle.success, row=0)
    async def apply_match(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("참여할 내전을 선택하세요.", view=MatchPickerView("join"), ephemeral=True)

    @discord.ui.button(label="신청 취소", style=discord.ButtonStyle.danger, row=0)
    async def cancel_match(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "신청 취소할 내전을 선택하세요.",
            view=MatchPickerView("cancel", user_id=interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(label="명단 보기", style=discord.ButtonStyle.secondary, row=0)
    async def view_roster(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("명단을 볼 내전을 선택하세요.", view=MatchPickerView("view"), ephemeral=True)

    @discord.ui.button(label="내전 목록", style=discord.ButtonStyle.secondary, row=1)
    async def list_matches(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(embed=build_match_list_embed(manager.list_matches()), ephemeral=True)

    @discord.ui.button(label="랭킹", style=discord.ButtonStyle.primary, row=1)
    async def rank_menu(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("랭킹 메뉴입니다.", view=RankMenuView(), ephemeral=True)

    @discord.ui.button(label="도움말", style=discord.ButtonStyle.secondary, row=1)
    async def help_menu(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(embed=build_help_embed(), ephemeral=True)

    @discord.ui.button(label="운영 패널", style=discord.ButtonStyle.primary, row=2)
    async def admin_menu(self, interaction: discord.Interaction, button: Button):
        if not await require_control_access_interaction(interaction, "운영 패널"):
            return
        await interaction.response.send_message(
            embed=build_admin_panel_embed(interaction.user),
            view=AdminPanelView(),
            ephemeral=True,
        )


class VoltBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        init_db()

    async def on_ready(self):
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")


bot = VoltBot()


@bot.command(name="1")
async def open_panel(ctx: commands.Context):
    await ctx.send(
        embed=build_main_panel_embed(ctx.author, has_control_access(ctx.author)),
        view=MainPanelView(),
    )


@bot.command(name="도움말")
async def help_command(ctx: commands.Context):
    await ctx.send(embed=build_help_embed(), view=MainPanelView())


@bot.command(name="신청")
async def join_command(ctx: commands.Context):
    await ctx.send("참여할 내전을 선택하세요.", view=MatchPickerView("join"))


@bot.command(name="신청취소")
async def cancel_command(ctx: commands.Context, match_id: int = None):
    if match_id is None:
        await ctx.send("신청 취소할 내전을 선택하세요.", view=MatchPickerView("cancel", user_id=ctx.author.id))
        return
    success, message = manager.unregister_player(match_id, ctx.author.id)
    if success:
        await refresh_match_announcement(match_id)
    await ctx.send(message)


@bot.command(name="내전목록")
async def list_command(ctx: commands.Context):
    await ctx.send(embed=build_match_list_embed(manager.list_matches()))


@bot.command(name="명단")
async def roster_command(ctx: commands.Context, match_id: int = None):
    if match_id is None:
        await ctx.send("명단을 볼 내전을 선택하세요.", view=MatchPickerView("view"))
        return
    match = manager.get_match(match_id)
    if not match:
        await ctx.send("해당 번호의 내전이 없습니다.")
        return
    await ctx.send(embed=build_match_embed(match))

@bot.command(name="내랭킹")
async def my_rank_command(ctx: commands.Context):
    await ctx.send("조회 기준을 선택하세요.", view=RankSelectView(is_all=False))


@bot.command(name="전체랭킹")
async def all_rank_command(ctx: commands.Context):
    await ctx.send("정렬 기준을 선택하세요.", view=RankSelectView(is_all=True))


@bot.command(name="점수표")
async def score_command(ctx: commands.Context):
    await ctx.send(embed=build_score_embed())


@bot.command(name="내전생성")
async def create_match_command(ctx: commands.Context, *, title: str):
    if not await require_control_access_ctx(ctx, "내전 생성"):
        return
    match_id = manager.create_match(title)
    match = manager.get_match(match_id)
    if not match:
        await ctx.send("내전 생성 중 오류가 발생했습니다.")
        return

    announcement = await ctx.send(
        content=build_match_announcement_content(match),
        embed=build_match_announcement_embed(match),
        view=MatchAnnouncementView(match_id),
    )
    match.announcement_channel_id = announcement.channel.id
    match.announcement_message_id = announcement.id


@bot.command(name="내전삭제")
async def delete_match_command(ctx: commands.Context, match_id: int):
    if not await require_control_access_ctx(ctx, "내전 삭제"):
        return
    match = manager.get_match(match_id)
    if not match:
        await ctx.send("해당 번호의 내전이 없습니다.")
        return
    await close_match_announcement(match, "이 내전 모집은 종료되었습니다.")
    manager.close_match(match_id)
    await ctx.send(f"`{match_id}`번 내전을 삭제했습니다.")


@bot.command(name="내전종료")
async def close_match_command(ctx: commands.Context, match_id: int):
    if not await require_control_access_ctx(ctx, "내전 종료"):
        return
    await delete_match_command(ctx, match_id)


@bot.command(name="명단수정")
async def edit_roster_command(ctx: commands.Context, match_id: int = None):
    if not await require_control_access_ctx(ctx, "명단 수정"):
        return
    if match_id is None:
        await ctx.send("관리할 내전을 선택하세요.", view=MatchPickerView("manage"))
        return
    match = manager.get_match(match_id)
    if not match:
        await ctx.send("해당 번호의 내전이 없습니다.")
        return
    if not match.list_players():
        await ctx.send("현재 참가자가 없어 수정할 명단이 없습니다.")
        return
    await ctx.send("제외할 인원을 누르세요.", view=EditListView(match_id))


@bot.command(name="드래프트")
async def draft_command(ctx: commands.Context, match_id: int, captain1_raw: str, captain2_raw: str):
    if not await require_control_access_ctx(ctx, "드래프트"):
        return
    match = manager.get_match(match_id)
    if not match:
        await ctx.send("해당 번호의 내전이 없습니다.")
        return

    captain1 = await resolve_member_from_text(ctx.guild, captain1_raw)
    captain2 = await resolve_member_from_text(ctx.guild, captain2_raw)
    if captain1 is None or captain2 is None:
        await ctx.send("캡틴을 찾지 못했습니다. 실제 멘션, 닉네임, 유저 ID 중 하나를 사용해주세요.")
        return
    if captain1.id == captain2.id:
        await ctx.send("캡틴 두 명은 서로 다른 사람이어야 합니다.")
        return
    if not match.has_user(captain1.id) or not match.has_user(captain2.id):
        await ctx.send("캡틴은 반드시 해당 내전 신청자여야 합니다.")
        return

    all_players = match.list_players()
    if len(all_players) != MATCH_CAPACITY:
        await ctx.send(f"드래프트는 정확히 {MATCH_CAPACITY}명이 모였을 때만 시작할 수 있습니다. 현재 {len(all_players)}명입니다.")
        return

    draft_pool = build_draft_pool(all_players, captain1.id, captain2.id)
    if len(draft_pool) != 8:
        await ctx.send(f"드래프트 대상 인원 수가 올바르지 않습니다. 현재 드래프트 가능 인원은 {len(draft_pool)}명입니다.")
        return

    draft_view = DraftView(match_id, captain1, captain2, draft_pool)
    await ctx.send(
        f"{captain1.display_name}님과 {captain2.display_name}님의 드래프트를 시작합니다.",
        embed=draft_view.make_embed(),
        view=draft_view,
    )


@bot.command(name="결과기록")
async def result_command(ctx: commands.Context, match_id: int, win_team: int):
    if not await require_control_access_ctx(ctx, "결과 기록"):
        return
    success, message = await record_match_result(match_id, win_team)
    if success:
        await ctx.send(message, view=MatchManageView(match_id))
    else:
        await ctx.send(message)


@bot.command(name="운영권한부여")
async def grant_operator_command(ctx: commands.Context, *, member_raw: str):
    if not await require_discord_admin_ctx(ctx, "운영권한 부여"):
        return
    member = await resolve_member_from_text(ctx.guild, member_raw)
    if member is None:
        await ctx.send("대상 멤버를 찾지 못했습니다. 실제 멘션, 닉네임, 유저 ID 중 하나를 사용해주세요.")
        return
    if member.bot:
        await ctx.send("봇 계정에는 운영권한을 부여할 수 없습니다.")
        return
    if has_control_access(member):
        await ctx.send("이미 운영 패널을 사용할 수 있는 멤버입니다.")
        return
    success, message = grant_operator_access(ctx.guild.id, member.id, ctx.author.id)
    await ctx.send(f"{member.mention} {message}" if success else message)


@bot.command(name="운영권한회수")
async def revoke_operator_command(ctx: commands.Context, *, member_raw: str):
    if not await require_discord_admin_ctx(ctx, "운영권한 회수"):
        return
    member = await resolve_member_from_text(ctx.guild, member_raw)
    if member is None:
        await ctx.send("대상 멤버를 찾지 못했습니다. 실제 멘션, 닉네임, 유저 ID 중 하나를 사용해주세요.")
        return
    success, message = revoke_operator_access(ctx.guild.id, member.id)
    await ctx.send(f"{member.mention} {message}" if success else message)


@bot.command(name="운영권한목록")
async def list_operator_command(ctx: commands.Context):
    if not await require_discord_admin_ctx(ctx, "운영권한 목록"):
        return
    await ctx.send(embed=build_operator_access_embed(ctx.guild))


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"입력값이 부족합니다. `!도움말` 또는 `!1`로 사용법을 확인해주세요. ({error.param.name})")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send("입력 형식이 올바르지 않습니다. 멘션 또는 숫자 값을 다시 확인해주세요.")
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("이 명령어를 사용할 권한이 없습니다.")
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send("이 명령어를 사용할 권한이 없습니다.")
        return
    if isinstance(error, commands.CommandNotFound):
        return

    logger.exception("Unhandled command error", exc_info=error)
    await ctx.send("명령 처리 중 오류가 발생했습니다. 로그를 확인해주세요.")


def validate_environment() -> bool:
    required = {"DISCORD_TOKEN": TOKEN, "DATABASE_URL": DATABASE_URL}
    missing = [name for name, value in required.items() if not value]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        return False
    return True


if __name__ == "__main__":
    if not validate_environment():
        raise SystemExit(1)
    init_db()
    keep_alive()
    bot.run(TOKEN)
