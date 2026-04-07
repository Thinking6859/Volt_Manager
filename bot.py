diff --git a/C:\Users\swjeong\Documents\GitHub\Volt_Manager\bot.py b/C:\Users\swjeong\Documents\GitHub\Volt_Manager\bot.py
new file mode 100644
--- /dev/null
+++ b/C:\Users\swjeong\Documents\GitHub\Volt_Manager\bot.py
@@ -0,0 +1,1411 @@
+import logging
+import os
+import re
+import uuid
+from contextlib import closing
+from dataclasses import dataclass, field
+from threading import Thread
+from typing import Dict, List, Optional, Tuple
+from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
+
+import discord
+import psycopg2
+import pytz
+from discord.ext import commands
+from discord.ui import Button, Modal, Select, TextInput, View
+from flask import Flask
+
+
+logging.basicConfig(
+    level=os.getenv("LOG_LEVEL", "INFO"),
+    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
+)
+logger = logging.getLogger("volt-bot")
+
+
+app = Flask(__name__)
+
+
+@app.route("/")
+def home():
+    return "VOLT System is Online!"
+
+
+def run_web_server():
+    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), use_reloader=False)
+
+
+def keep_alive():
+    Thread(target=run_web_server, daemon=True).start()
+
+
+DATABASE_URL = os.getenv("DATABASE_URL")
+DB_SSLMODE = os.getenv("DB_SSLMODE")
+TOKEN = os.getenv("DISCORD_TOKEN")
+KST = pytz.timezone("Asia/Seoul")
+
+TIER_ORDER = [
+    "아이언",
+    "브론즈",
+    "실버",
+    "골드",
+    "플래티넘",
+    "에메랄드",
+    "다이아몬드",
+    "마스터",
+    "그랜드마스터",
+    "챌린저",
+]
+TIER_DATA = {tier: index + 1 for index, tier in enumerate(TIER_ORDER)}
+POSITIONS = ["탑", "정글", "미드", "원딜", "서폿"]
+SUB_POSITIONS = [*POSITIONS, "상관없음"]
+POINTS_WIN = 10
+POINTS_ACTIVITY = 10
+MATCH_CAPACITY = 10
+MAX_SELECT_OPTIONS = 25
+MENTION_RE = re.compile(r"^<@!?(\d+)>$")
+
+
+def env_flag(name: str, default: bool = False) -> bool:
+    raw = os.getenv(name)
+    if raw is None:
+        return default
+    return raw.strip().lower() in {"1", "true", "yes", "on"}
+
+
+ALLOW_DUPLICATE_SIGNUPS = env_flag("ALLOW_DUPLICATE_SIGNUPS", default=True)
+
+
+def now_kst() -> str:
+    return discord.utils.utcnow().astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
+
+
+def normalize_database_url(url: Optional[str]) -> Optional[str]:
+    if not url:
+        return None
+
+    if url.startswith("postgres://"):
+        url = url.replace("postgres://", "postgresql://", 1)
+
+    parsed = urlparse(url)
+    if not parsed.scheme:
+        return url
+
+    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
+    sslmode = DB_SSLMODE or ("require" if "supabase" in parsed.netloc.lower() else None)
+    if sslmode and "sslmode" not in query:
+        query["sslmode"] = sslmode
+        parsed = parsed._replace(query=urlencode(query))
+        return urlunparse(parsed)
+
+    return url
+
+
+def get_db_conn():
+    url = normalize_database_url(DATABASE_URL)
+    if not url:
+        logger.error("DATABASE_URL is not configured.")
+        return None
+
+    try:
+        return psycopg2.connect(url, connect_timeout=5)
+    except Exception:
+        logger.exception("Failed to connect to database.")
+        return None
+
+
+def init_db():
+    conn = get_db_conn()
+    if not conn:
+        logger.warning("Skipping DB initialization because connection is unavailable.")
+        return False
+
+    with closing(conn), closing(conn.cursor()) as cur:
+        cur.execute(
+            """
+            CREATE TABLE IF NOT EXISTS volt_rank (
+                user_id TEXT PRIMARY KEY,
+                name TEXT NOT NULL,
+                wins INTEGER DEFAULT 0,
+                losses INTEGER DEFAULT 0,
+                points INTEGER DEFAULT 0,
+                activity_points INTEGER DEFAULT 0,
+                updated_at TIMESTAMP DEFAULT NOW()
+            )
+            """
+        )
+        cur.execute(
+            """
+            ALTER TABLE volt_rank
+            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()
+            """
+        )
+        conn.commit()
+    return True
+
+
+@dataclass
+class PlayerEntry:
+    entry_id: str
+    user_id: int
+    name: str
+    tier: str
+    main: str
+    sub: str
+
+
+@dataclass
+class Match:
+    match_id: int
+    title: str
+    waiting_list: Dict[str, PlayerEntry] = field(default_factory=dict)
+    created_at: str = field(default_factory=now_kst)
+    announcement_channel_id: Optional[int] = None
+    announcement_message_id: Optional[int] = None
+
+    def has_user(self, user_id: int) -> bool:
+        return any(player.user_id == user_id for player in self.waiting_list.values())
+
+    def get_entry_id_by_user(self, user_id: int) -> Optional[str]:
+        for entry_id, player in self.waiting_list.items():
+            if player.user_id == user_id:
+                return entry_id
+        return None
+
+    def list_players(self) -> List[PlayerEntry]:
+        return list(self.waiting_list.values())
+
+    def unique_users(self) -> List[Tuple[int, str]]:
+        seen = set()
+        users = []
+        for player in self.waiting_list.values():
+            if player.user_id in seen:
+                continue
+            seen.add(player.user_id)
+            users.append((player.user_id, player.name))
+        return users
+
+
+class MatchManager:
+    def __init__(self):
+        self.matches: Dict[int, Match] = {}
+        self.match_count = 0
+        self.last_teams: Dict[int, Dict[str, List[Dict[str, int]]]] = {}
+
+    def list_matches(self) -> List[Match]:
+        return [self.matches[key] for key in sorted(self.matches.keys())]
+
+    def create_match(self, title: str) -> int:
+        self.match_count += 1
+        self.matches[self.match_count] = Match(match_id=self.match_count, title=title)
+        return self.match_count
+
+    def get_match(self, match_id: int) -> Optional[Match]:
+        return self.matches.get(match_id)
+
+    def close_match(self, match_id: int) -> bool:
+        removed = self.matches.pop(match_id, None)
+        self.last_teams.pop(match_id, None)
+        return removed is not None
+
+    def get_user_matches(self, user_id: int) -> List[Match]:
+        return [match for match in self.list_matches() if match.has_user(user_id)]
+
+    def register_player(self, match_id: int, user_id: int, name: str, tier: str, main: str, sub: str):
+        match = self.get_match(match_id)
+        if not match:
+            return False, "이미 종료되었거나 존재하지 않는 내전입니다."
+        if not ALLOW_DUPLICATE_SIGNUPS and match.has_user(user_id):
+            return False, "이미 이 내전에 신청되어 있습니다."
+        if len(match.waiting_list) >= MATCH_CAPACITY:
+            return False, "정원이 가득 찼습니다."
+
+        entry_id = str(uuid.uuid4())
+        match.waiting_list[entry_id] = PlayerEntry(
+            entry_id=entry_id,
+            user_id=user_id,
+            name=name,
+            tier=tier,
+            main=main,
+            sub=sub,
+        )
+        return True, f"{name}님 신청이 완료되었습니다."
+
+    def unregister_player(self, match_id: int, user_id: int):
+        match = self.get_match(match_id)
+        if not match:
+            return False, "존재하지 않는 내전입니다."
+        entry_id = match.get_entry_id_by_user(user_id)
+        if not entry_id:
+            return False, "이 내전에 신청된 기록이 없습니다."
+        removed_name = match.waiting_list.pop(entry_id).name
+        return True, f"{removed_name}님의 신청이 취소되었습니다."
+
+
+manager = MatchManager()
+
+
+def format_player(player: PlayerEntry) -> str:
+    return f"{player.name} [{player.tier}] ({player.main}/{player.sub})"
+
+
+def format_button_label(player: PlayerEntry) -> str:
+    label = f"[{player.tier}] {player.name} | {player.main}/{player.sub}"
+    return label[:80]
+
+
+def build_draft_pool(players: List[PlayerEntry], captain1_id: int, captain2_id: int) -> List[PlayerEntry]:
+    if not ALLOW_DUPLICATE_SIGNUPS:
+        return [player for player in players if player.user_id not in {captain1_id, captain2_id}]
+
+    removed = set()
+    draft_pool = []
+    for player in players:
+        if player.user_id == captain1_id and captain1_id not in removed:
+            removed.add(captain1_id)
+            continue
+        if player.user_id == captain2_id and captain2_id not in removed:
+            removed.add(captain2_id)
+            continue
+        draft_pool.append(player)
+    return draft_pool
+
+
+async def resolve_member_from_text(guild: Optional[discord.Guild], raw: str) -> Optional[discord.Member]:
+    if guild is None:
+        return None
+
+    value = raw.strip()
+    match = MENTION_RE.match(value)
+    member_id = int(match.group(1)) if match else int(value) if value.isdigit() else None
+
+    if member_id is not None:
+        member = guild.get_member(member_id)
+        if member is not None:
+            return member
+        try:
+            return await guild.fetch_member(member_id)
+        except discord.HTTPException:
+            return None
+
+    lowered = value.casefold()
+    for member in guild.members:
+        candidates = [member.display_name, member.name, member.global_name]
+        if any(candidate and candidate.casefold() == lowered for candidate in candidates):
+            return member
+    return None
+
+
+def build_main_panel_embed(user: Optional[discord.abc.User], is_admin: bool) -> discord.Embed:
+    name = user.display_name if isinstance(user, discord.Member) else getattr(user, "display_name", "VOLT")
+    embed = discord.Embed(
+        title="VOLT 컨트롤 패널",
+        description=(
+            f"{name}님, 아래 버튼으로 내전 기능을 바로 조작할 수 있습니다.\n"
+            "추천 진입점은 `!1` 입니다."
+        ),
+        color=0x5865F2,
+    )
+    embed.add_field(
+        name="유저 기능",
+        value="참여 신청, 신청 취소, 내전 목록, 명단 보기, 랭킹 조회, 점수 확인",
+        inline=False,
+    )
+    embed.add_field(
+        name="운영 기능",
+        value="내전 생성, 명단 수정, 드래프트, 결과 기록, 내전 삭제",
+        inline=False,
+    )
+    embed.set_footer(text="운영 패널은 관리자만 사용할 수 있습니다." if is_admin else "관리자 권한이 있으면 운영 패널도 열 수 있습니다.")
+    return embed
+
+
+def build_help_embed() -> discord.Embed:
+    embed = discord.Embed(
+        title="VOLT 사용 안내",
+        description="이제 대부분의 기능은 `!1` 패널에서 버튼과 선택 메뉴로 조작할 수 있습니다.",
+        color=0x2ECC71,
+    )
+    embed.add_field(
+        name="가장 쉬운 사용법",
+        value="`!1` 입력 후 버튼으로 이동\n참여 신청, 신청 취소, 명단 보기, 랭킹 조회 가능",
+        inline=False,
+    )
+    embed.add_field(
+        name="운영진 추천 흐름",
+        value="`!1` -> 운영 패널 -> 내전 생성 / 내전 관리\n관리 화면에서 명단 수정, 드래프트, 결과 기록, 삭제 가능",
+        inline=False,
+    )
+    embed.add_field(
+        name="백업 명령어",
+        value="`!내전생성` `!드래프트` `!결과기록` 같은 텍스트 명령어도 계속 사용 가능",
+        inline=False,
+    )
+    embed.set_footer(text="테스트 중에는 ALLOW_DUPLICATE_SIGNUPS=true, 정식 출시 때는 false")
+    return embed
+
+
+def build_score_embed() -> discord.Embed:
+    embed = discord.Embed(title="VOLT 점수 안내", color=0xF1C40F)
+    embed.add_field(name="승리 점수", value=f"{POINTS_WIN}점", inline=True)
+    embed.add_field(name="참여 점수", value=f"{POINTS_ACTIVITY}점", inline=True)
+    embed.add_field(name="종합 점수", value="승리 점수 + 참여 점수", inline=False)
+    embed.set_footer(text="랭킹 패널에서 개인/전체 랭킹을 바로 조회할 수 있습니다.")
+    return embed
+
+
+def build_match_list_embed(matches: List[Match]) -> discord.Embed:
+    embed = discord.Embed(title="진행 중인 내전 목록", color=0x3498DB)
+    if not matches:
+        embed.description = "현재 진행 중인 내전이 없습니다."
+        return embed
+
+    lines = [
+        f"• `{match.match_id}`번 | {match.title} | {len(match.waiting_list)}/{MATCH_CAPACITY}명"
+        for match in matches
+    ]
+    embed.description = "\n".join(lines)
+    return embed
+
+
+def build_match_embed(match: Match) -> discord.Embed:
+    embed = discord.Embed(
+        title=f"{match.title} 명단",
+        description=f"방 번호: `{match.match_id}` | 생성 시간: {match.created_at}",
+        color=0x3498DB,
+    )
+    players = match.list_players()
+    if not players:
+        embed.add_field(name="참여자", value="아직 참여자가 없습니다.", inline=False)
+    else:
+        lines = [f"• {format_player(player)}" for player in players]
+        embed.add_field(name=f"참여자 ({len(players)}/{MATCH_CAPACITY})", value="\n".join(lines), inline=False)
+    return embed
+
+
+def build_manage_embed(match: Match) -> discord.Embed:
+    embed = discord.Embed(
+        title=f"운영 패널 | {match.title}",
+        description=f"방 번호: `{match.match_id}` | 현재 인원: {len(match.waiting_list)}/{MATCH_CAPACITY}",
+        color=0xE67E22,
+    )
+    embed.add_field(
+        name="관리 기능",
+        value="명단 보기, 명단 수정, 공지 새로고침, 드래프트 시작, 결과 기록, 내전 삭제",
+        inline=False,
+    )
+    if match.list_players():
+        preview = "\n".join(f"• {player.name} [{player.tier}] ({player.main}/{player.sub})" for player in match.list_players()[:10])
+        embed.add_field(name="현재 명단", value=preview, inline=False)
+    else:
+        embed.add_field(name="현재 명단", value="아직 참여자가 없습니다.", inline=False)
+    return embed
+
+
+def build_match_announcement_content(match: Match) -> str:
+    status = "모집 마감" if len(match.waiting_list) >= MATCH_CAPACITY else "모집 중"
+    return f"@here 내전 모집이 시작되었습니다. [{status}]"
+
+
+def build_match_announcement_embed(match: Match) -> discord.Embed:
+    is_full = len(match.waiting_list) >= MATCH_CAPACITY
+    embed = discord.Embed(
+        title=f"내전 모집 | {match.title}",
+        description=(
+            f"방 번호: `{match.match_id}`\n"
+            f"생성 시간: {match.created_at}\n"
+            + (
+                "현재 정원이 가득 차 모집이 마감되었습니다."
+                if is_full
+                else "아래 버튼으로 바로 신청, 취소, 명단 확인이 가능합니다."
+            )
+        ),
+        color=0x5865F2,
+    )
+    players = match.list_players()
+    if players:
+        preview = "\n".join(f"• {player.name} [{player.tier}] ({player.main}/{player.sub})" for player in players[:10])
+        embed.add_field(name=f"현재 참가자 ({len(players)}/{MATCH_CAPACITY})", value=preview, inline=False)
+    else:
+        embed.add_field(name=f"현재 참가자 (0/{MATCH_CAPACITY})", value="아직 신청자가 없습니다.", inline=False)
+
+    footer = "테스트 모드: 중복 신청 허용 중" if ALLOW_DUPLICATE_SIGNUPS else "중복 신청은 자동으로 막힙니다."
+    if is_full:
+        footer += " | 취소가 나오면 다시 열립니다."
+    embed.set_footer(text=footer)
+    return embed
+
+
+async def close_match_announcement(match: Match, content: str):
+    if not match.announcement_channel_id or not match.announcement_message_id:
+        return
+
+    channel = bot.get_channel(match.announcement_channel_id)
+    if channel is None:
+        try:
+            channel = await bot.fetch_channel(match.announcement_channel_id)
+        except Exception:
+            logger.exception("Failed to fetch announcement channel for match %s", match.match_id)
+            return
+
+    try:
+        message = await channel.fetch_message(match.announcement_message_id)
+        await message.edit(content=content, embed=build_match_announcement_embed(match), view=None)
+    except Exception:
+        logger.exception("Failed to close announcement message for match %s", match.match_id)
+
+
+async def refresh_match_announcement(match_id: int):
+    match = manager.get_match(match_id)
+    if not match or not match.announcement_channel_id or not match.announcement_message_id:
+        return
+
+    channel = bot.get_channel(match.announcement_channel_id)
+    if channel is None:
+        try:
+            channel = await bot.fetch_channel(match.announcement_channel_id)
+        except Exception:
+            logger.exception("Failed to fetch announcement channel for match %s", match_id)
+            return
+
+    try:
+        message = await channel.fetch_message(match.announcement_message_id)
+        await message.edit(
+            content=build_match_announcement_content(match),
+            embed=build_match_announcement_embed(match),
+            view=MatchAnnouncementView(match_id),
+        )
+    except Exception:
+        logger.exception("Failed to refresh announcement message for match %s", match_id)
+
+
+async def record_match_result(match_id: int, win_team: int):
+    if win_team not in (1, 2):
+        return False, "승리 팀 번호는 1 또는 2만 입력할 수 있습니다."
+
+    teams = manager.last_teams.get(match_id)
+    if not teams:
+        return False, "기록할 팀 데이터가 없습니다. 먼저 드래프트를 완료해주세요."
+
+    conn = get_db_conn()
+    if not conn:
+        return False, "DB 연결 실패로 결과를 기록하지 못했습니다. Render의 DATABASE_URL 설정을 확인해주세요."
+
+    lose_team = 2 if win_team == 1 else 1
+    try:
+        with closing(conn), closing(conn.cursor()) as cur:
+            for player in teams[f"team{win_team}"]:
+                cur.execute(
+                    """
+                    INSERT INTO volt_rank (user_id, name, wins, points, activity_points, updated_at)
+                    VALUES (%s, %s, 1, %s, %s, NOW())
+                    ON CONFLICT (user_id) DO UPDATE SET
+                        name = EXCLUDED.name,
+                        wins = volt_rank.wins + 1,
+                        points = volt_rank.points + EXCLUDED.points,
+                        activity_points = volt_rank.activity_points + EXCLUDED.activity_points,
+                        updated_at = NOW()
+                    """,
+                    (str(player["user_id"]), player["name"], POINTS_WIN, POINTS_ACTIVITY),
+                )
+
+            for player in teams[f"team{lose_team}"]:
+                cur.execute(
+                    """
+                    INSERT INTO volt_rank (user_id, name, losses, activity_points, updated_at)
+                    VALUES (%s, %s, 1, %s, NOW())
+                    ON CONFLICT (user_id) DO UPDATE SET
+                        name = EXCLUDED.name,
+                        losses = volt_rank.losses + 1,
+                        activity_points = volt_rank.activity_points + EXCLUDED.activity_points,
+                        updated_at = NOW()
+                    """,
+                    (str(player["user_id"]), player["name"], POINTS_ACTIVITY),
+                )
+            conn.commit()
+    except Exception as exc:
+        logger.exception("Failed to record match result for match %s", match_id)
+        try:
+            conn.rollback()
+        except Exception:
+            pass
+        return False, f"DB 저장 중 오류가 발생했습니다. `{type(exc).__name__}`"
+
+    return True, "결과 기록이 완료되었습니다."
+
+
+class RankSelectView(View):
+    def __init__(self, is_all: bool):
+        super().__init__(timeout=120)
+        self.is_all = is_all
+
+    async def process_rank(self, interaction: discord.Interaction, sort_col: str, label: str):
+        conn = get_db_conn()
+        if not conn:
+            await interaction.response.edit_message(content="DB 연결에 실패했습니다. 잠시 후 다시 시도해주세요.", view=None)
+            return
+
+        order_by = sort_col if sort_col != "total" else "(points + activity_points)"
+        with closing(conn), closing(conn.cursor()) as cur:
+            cur.execute(
+                f"""
+                SELECT user_id, name, points, activity_points, wins, losses
+                FROM volt_rank
+                ORDER BY {order_by} DESC, wins DESC, name ASC
+                """
+            )
+            rows = cur.fetchall()
+
+        if not rows:
+            await interaction.response.edit_message(content="아직 기록된 데이터가 없습니다.", view=None)
+            return
+
+        if self.is_all:
+            lines = []
+            for index, row in enumerate(rows, start=1):
+                value = row[2] if sort_col == "points" else row[3] if sort_col == "activity_points" else row[2] + row[3]
+                lines.append(f"**{index}위 {row[1]}** | {value}pt")
+            embed = discord.Embed(
+                title=f"VOLT 전체 랭킹 ({label})",
+                description="\n".join(lines[:25]),
+                color=0xFFD700,
+            )
+        else:
+            user_row = next(((index, row) for index, row in enumerate(rows, start=1) if row[0] == str(interaction.user.id)), None)
+            if not user_row:
+                await interaction.response.edit_message(content="해당 부문의 기록이 없습니다.", view=None)
+                return
+
+            rank_index, row = user_row
+            total_points = row[2] + row[3]
+            total_games = row[4] + row[5]
+            win_rate = (row[4] / total_games * 100) if total_games else 0
+
+            embed = discord.Embed(title=f"{row[1]}님의 랭킹 정보", color=0x3498DB)
+            embed.add_field(name=f"{label} 순위", value=f"**{rank_index}위**", inline=True)
+            embed.add_field(name="종합 점수", value=f"**{total_points}pt**", inline=True)
+            embed.add_field(name="상세 점수", value=f"승리: {row[2]}pt / 참여: {row[3]}pt", inline=False)
+            embed.add_field(name="전적", value=f"{row[4]}승 {row[5]}패 (승률 {win_rate:.1f}%)", inline=False)
+
+        await interaction.response.edit_message(content=None, embed=embed, view=None)
+
+    @discord.ui.button(label="승리 점수", style=discord.ButtonStyle.success, row=0)
+    async def victory_rank(self, interaction: discord.Interaction, button: Button):
+        await self.process_rank(interaction, "points", "승리 점수")
+
+    @discord.ui.button(label="참여 점수", style=discord.ButtonStyle.primary, row=0)
+    async def activity_rank(self, interaction: discord.Interaction, button: Button):
+        await self.process_rank(interaction, "activity_points", "참여 점수")
+
+    @discord.ui.button(label="종합 점수", style=discord.ButtonStyle.secondary, row=0)
+    async def total_rank(self, interaction: discord.Interaction, button: Button):
+        await self.process_rank(interaction, "total", "종합 점수")
+
+
+class PositionSelectView(View):
+    def __init__(self, match_id: int, tier: str):
+        super().__init__(timeout=180)
+        self.match_id = match_id
+        self.tier = tier
+        self.main_position: Optional[str] = None
+
+    @discord.ui.select(
+        placeholder="주 라인을 선택하세요.",
+        options=[discord.SelectOption(label=lane, value=lane) for lane in POSITIONS],
+    )
+    async def main_callback(self, interaction: discord.Interaction, select: Select):
+        self.main_position = select.values[0]
+        self.clear_items()
+        sub_select = Select(
+            placeholder="부 라인을 선택하세요.",
+            options=[discord.SelectOption(label=lane, value=lane) for lane in SUB_POSITIONS],
+        )
+        sub_select.callback = self.final_callback
+        self.add_item(sub_select)
+        await interaction.response.edit_message(content="부 라인을 선택하세요.", view=self)
+
+    async def final_callback(self, interaction: discord.Interaction):
+        success, message = manager.register_player(
+            self.match_id,
+            interaction.user.id,
+            interaction.user.display_name,
+            self.tier,
+            self.main_position or POSITIONS[0],
+            interaction.data["values"][0],
+        )
+        if success:
+            await refresh_match_announcement(self.match_id)
+        await interaction.response.edit_message(content=message, view=None)
+
+
+class TierSelectView(View):
+    def __init__(self, match_id: int):
+        super().__init__(timeout=120)
+        self.match_id = match_id
+
+    @discord.ui.select(
+        placeholder="티어를 선택하세요.",
+        options=[discord.SelectOption(label=tier, value=tier) for tier in TIER_ORDER],
+    )
+    async def tier_callback(self, interaction: discord.Interaction, select: Select):
+        await interaction.response.send_message(
+            "라인을 선택해주세요.",
+            view=PositionSelectView(self.match_id, select.values[0]),
+            ephemeral=True,
+        )
+
+
+class EditListView(View):
+    def __init__(self, match_id: int):
+        super().__init__(timeout=300)
+        self.match_id = match_id
+        self.update_buttons()
+
+    def update_buttons(self):
+        self.clear_items()
+        match = manager.get_match(self.match_id)
+        if not match:
+            return
+        for player in match.list_players():
+            button = Button(
+                label=f"제외 {player.name}"[:80],
+                style=discord.ButtonStyle.danger,
+                custom_id=player.entry_id,
+            )
+            button.callback = self.delete_player
+            self.add_item(button)
+
+    async def delete_player(self, interaction: discord.Interaction):
+        match = manager.get_match(self.match_id)
+        entry_id = interaction.data["custom_id"]
+        if not match or entry_id not in match.waiting_list:
+            await interaction.response.edit_message(content="이미 제거되었거나 존재하지 않는 참가자입니다.", view=None)
+            return
+
+        removed_name = match.waiting_list.pop(entry_id).name
+        await refresh_match_announcement(self.match_id)
+        self.update_buttons()
+        if self.children:
+            await interaction.response.edit_message(content=f"{removed_name}님을 명단에서 제외했습니다.", view=self)
+        else:
+            await interaction.response.edit_message(content=f"{removed_name}님을 명단에서 제외했습니다. 현재 참가자가 없습니다.", view=None)
+
+
+class DraftView(View):
+    def __init__(self, match_id: int, captain1: discord.Member, captain2: discord.Member, players: List[PlayerEntry]):
+        super().__init__(timeout=900)
+        self.match_id = match_id
+        self.captains = [captain1, captain2]
+        self.players = players
+        self.teams: List[List[PlayerEntry]] = [[], []]
+        self.pick_seq = [0, 1, 1, 0, 0, 1, 1, 0]
+        self.step = 0
+        self.update_buttons()
+
+    def make_embed(self) -> discord.Embed:
+        match = manager.get_match(self.match_id)
+        title = match.title if match else f"{self.match_id}번 내전"
+        embed = discord.Embed(title=f"{title} 드래프트", color=0x5865F2)
+        team1 = [f"캡틴: **{self.captains[0].display_name}**"] + [f"• {format_player(player)}" for player in self.teams[0]]
+        team2 = [f"캡틴: **{self.captains[1].display_name}**"] + [f"• {format_player(player)}" for player in self.teams[1]]
+        embed.add_field(name="1팀", value="\n".join(team1), inline=True)
+        embed.add_field(name="2팀", value="\n".join(team2), inline=True)
+        if self.step < len(self.pick_seq):
+            embed.set_footer(text=f"{self.captains[self.pick_seq[self.step]].display_name}님의 차례입니다.")
+        else:
+            embed.set_footer(text="팀 구성이 완료되었습니다.")
+        return embed
+
+    def update_buttons(self):
+        self.clear_items()
+        picked_ids = {player.entry_id for player in self.teams[0] + self.teams[1]}
+        for index, player in enumerate(self.players):
+            if player.entry_id in picked_ids:
+                continue
+            button = Button(
+                label=format_button_label(player),
+                style=discord.ButtonStyle.secondary,
+                custom_id=str(index),
+            )
+            button.callback = self.pick_callback
+            self.add_item(button)
+
+    async def pick_callback(self, interaction: discord.Interaction):
+        current_captain = self.captains[self.pick_seq[self.step]]
+        if interaction.user.id != current_captain.id:
+            await interaction.response.send_message("지금은 본인 차례가 아닙니다.", ephemeral=True)
+            return
+
+        selected_player = self.players[int(interaction.data["custom_id"])]
+        if any(selected_player.entry_id == player.entry_id for player in self.teams[0] + self.teams[1]):
+            await interaction.response.send_message("이미 선택된 플레이어입니다.", ephemeral=True)
+            return
+
+        self.teams[self.pick_seq[self.step]].append(selected_player)
+        self.step += 1
+
+        if self.step >= len(self.pick_seq):
+            team1 = [{"name": self.captains[0].display_name, "user_id": self.captains[0].id}] + [
+                {"name": player.name, "user_id": player.user_id} for player in self.teams[0]
+            ]
+            team2 = [{"name": self.captains[1].display_name, "user_id": self.captains[1].id}] + [
+                {"name": player.name, "user_id": player.user_id} for player in self.teams[1]
+            ]
+            manager.last_teams[self.match_id] = {"team1": team1, "team2": team2}
+            await interaction.response.edit_message(content="드래프트가 완료되었습니다.", embed=self.make_embed(), view=None)
+            return
+
+        self.update_buttons()
+        await interaction.response.edit_message(embed=self.make_embed(), view=self)
+
+
+class MatchPickerView(View):
+    def __init__(self, mode: str, user_id: Optional[int] = None):
+        super().__init__(timeout=180)
+        self.mode = mode
+        self.user_id = user_id
+
+        matches = manager.list_matches()
+        if mode == "cancel" and user_id is not None:
+            matches = [match for match in matches if match.has_user(user_id)]
+
+        options = [
+            discord.SelectOption(
+                label=f"[{match.match_id}] {match.title} ({len(match.waiting_list)}/{MATCH_CAPACITY})",
+                value=str(match.match_id),
+            )
+            for match in matches[:MAX_SELECT_OPTIONS]
+        ]
+
+        if not options:
+            placeholder = {
+                "join": "참여할 내전이 없습니다.",
+                "cancel": "취소할 신청 내역이 없습니다.",
+                "view": "조회할 내전이 없습니다.",
+                "manage": "관리할 내전이 없습니다.",
+            }[mode]
+            select = Select(
+                placeholder=placeholder,
+                options=[discord.SelectOption(label="선택 가능한 내전이 없습니다.", value="0")],
+                disabled=True,
+            )
+            self.add_item(select)
+            return
+
+        placeholder = {
+            "join": "참여할 내전을 선택하세요.",
+            "cancel": "신청을 취소할 내전을 선택하세요.",
+            "view": "명단을 볼 내전을 선택하세요.",
+            "manage": "관리할 내전을 선택하세요.",
+        }[mode]
+        select = Select(placeholder=placeholder, options=options)
+        select.callback = self.handle_select
+        self.add_item(select)
+
+    async def handle_select(self, interaction: discord.Interaction):
+        match_id = int(interaction.data["values"][0])
+        match = manager.get_match(match_id)
+        if not match:
+            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
+            return
+
+        if self.mode == "join":
+            await interaction.response.send_message(
+                f"`{match_id}`번 내전 신청을 진행합니다. 티어를 선택해주세요.",
+                view=TierSelectView(match_id),
+                ephemeral=True,
+            )
+            return
+
+        if self.mode == "cancel":
+            success, message = manager.unregister_player(match_id, interaction.user.id)
+            if success:
+                await refresh_match_announcement(match_id)
+            await interaction.response.send_message(message, ephemeral=True)
+            return
+
+        if self.mode == "view":
+            await interaction.response.send_message(embed=build_match_embed(match), ephemeral=True)
+            return
+
+        await interaction.response.send_message(
+            embed=build_manage_embed(match),
+            view=MatchManageView(match_id),
+            ephemeral=True,
+        )
+
+
+class ConfirmDeleteMatchView(View):
+    def __init__(self, match_id: int):
+        super().__init__(timeout=120)
+        self.match_id = match_id
+
+    @discord.ui.button(label="삭제 확인", style=discord.ButtonStyle.danger, row=0)
+    async def confirm(self, interaction: discord.Interaction, button: Button):
+        match = manager.get_match(self.match_id)
+        if not match:
+            await interaction.response.edit_message(content="이미 삭제된 내전입니다.", view=None)
+            return
+
+        await close_match_announcement(match, "이 내전 모집은 종료되었습니다.")
+        manager.close_match(self.match_id)
+        await interaction.response.edit_message(content=f"`{self.match_id}`번 내전을 삭제했습니다.", view=None)
+
+    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary, row=0)
+    async def cancel(self, interaction: discord.Interaction, button: Button):
+        await interaction.response.edit_message(content="내전 삭제를 취소했습니다.", view=None)
+
+
+class CaptainOneSelectView(View):
+    def __init__(self, match_id: int):
+        super().__init__(timeout=180)
+        self.match_id = match_id
+        match = manager.get_match(match_id)
+        users = match.unique_users() if match else []
+        options = [discord.SelectOption(label=name, value=str(user_id)) for user_id, name in users[:MAX_SELECT_OPTIONS]]
+        select = Select(
+            placeholder="첫 번째 캡틴을 선택하세요.",
+            options=options or [discord.SelectOption(label="선택 가능한 인원이 없습니다.", value="0")],
+            disabled=not options,
+        )
+        select.callback = self.select_first
+        self.add_item(select)
+
+    async def select_first(self, interaction: discord.Interaction):
+        captain1_id = int(interaction.data["values"][0])
+        await interaction.response.send_message(
+            "두 번째 캡틴을 선택하세요.",
+            view=CaptainTwoSelectView(self.match_id, captain1_id),
+            ephemeral=True,
+        )
+
+
+class CaptainTwoSelectView(View):
+    def __init__(self, match_id: int, captain1_id: int):
+        super().__init__(timeout=180)
+        self.match_id = match_id
+        self.captain1_id = captain1_id
+        match = manager.get_match(match_id)
+        users = [user for user in match.unique_users() if user[0] != captain1_id] if match else []
+        options = [discord.SelectOption(label=name, value=str(user_id)) for user_id, name in users[:MAX_SELECT_OPTIONS]]
+        select = Select(
+            placeholder="두 번째 캡틴을 선택하세요.",
+            options=options or [discord.SelectOption(label="선택 가능한 인원이 없습니다.", value="0")],
+            disabled=not options,
+        )
+        select.callback = self.select_second
+        self.add_item(select)
+
+    async def select_second(self, interaction: discord.Interaction):
+        match = manager.get_match(self.match_id)
+        if not match:
+            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
+            return
+
+        captain2_id = int(interaction.data["values"][0])
+        if captain2_id == self.captain1_id:
+            await interaction.response.send_message("캡틴 두 명은 서로 달라야 합니다.", ephemeral=True)
+            return
+
+        captain1 = interaction.guild.get_member(self.captain1_id) if interaction.guild else None
+        captain2 = interaction.guild.get_member(captain2_id) if interaction.guild else None
+        if captain1 is None and interaction.guild is not None:
+            try:
+                captain1 = await interaction.guild.fetch_member(self.captain1_id)
+            except discord.HTTPException:
+                captain1 = None
+        if captain2 is None and interaction.guild is not None:
+            try:
+                captain2 = await interaction.guild.fetch_member(captain2_id)
+            except discord.HTTPException:
+                captain2 = None
+        if captain1 is None or captain2 is None:
+            await interaction.response.send_message("캡틴 정보를 불러오지 못했습니다.", ephemeral=True)
+            return
+
+        all_players = match.list_players()
+        if len(all_players) != MATCH_CAPACITY:
+            await interaction.response.send_message(
+                f"드래프트는 정확히 {MATCH_CAPACITY}명이 모였을 때만 시작할 수 있습니다. 현재 {len(all_players)}명입니다.",
+                ephemeral=True,
+            )
+            return
+
+        draft_pool = build_draft_pool(all_players, captain1.id, captain2.id)
+        if len(draft_pool) != 8:
+            await interaction.response.send_message(
+                f"드래프트 대상 인원 수가 올바르지 않습니다. 현재 드래프트 가능 인원은 {len(draft_pool)}명입니다.",
+                ephemeral=True,
+            )
+            return
+
+        draft_view = DraftView(self.match_id, captain1, captain2, draft_pool)
+        await interaction.response.send_message(
+            f"{captain1.display_name}님과 {captain2.display_name}님의 드래프트를 시작합니다.",
+            embed=draft_view.make_embed(),
+            view=draft_view,
+        )
+
+
+class ResultTeamSelectView(View):
+    def __init__(self, match_id: int):
+        super().__init__(timeout=180)
+        self.match_id = match_id
+
+    @discord.ui.button(label="1팀 승리", style=discord.ButtonStyle.success, row=0)
+    async def team1_win(self, interaction: discord.Interaction, button: Button):
+        await self.record(interaction, 1)
+
+    @discord.ui.button(label="2팀 승리", style=discord.ButtonStyle.success, row=0)
+    async def team2_win(self, interaction: discord.Interaction, button: Button):
+        await self.record(interaction, 2)
+
+    async def record(self, interaction: discord.Interaction, win_team: int):
+        success, message = await record_match_result(self.match_id, win_team)
+        if success:
+            await interaction.response.send_message(message, view=MatchManageView(self.match_id))
+        else:
+            await interaction.response.send_message(message, ephemeral=True)
+
+
+class MatchManageView(View):
+    def __init__(self, match_id: int):
+        super().__init__(timeout=300)
+        self.match_id = match_id
+
+    @discord.ui.button(label="명단 보기", style=discord.ButtonStyle.secondary, row=0)
+    async def view_roster(self, interaction: discord.Interaction, button: Button):
+        match = manager.get_match(self.match_id)
+        if not match:
+            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
+            return
+        await interaction.response.send_message(embed=build_match_embed(match), ephemeral=True)
+
+    @discord.ui.button(label="명단 수정", style=discord.ButtonStyle.primary, row=0)
+    async def edit_roster(self, interaction: discord.Interaction, button: Button):
+        match = manager.get_match(self.match_id)
+        if not match:
+            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
+            return
+        if not match.list_players():
+            await interaction.response.send_message("현재 참가자가 없어 수정할 명단이 없습니다.", ephemeral=True)
+            return
+        await interaction.response.send_message("제외할 인원을 누르세요.", view=EditListView(self.match_id), ephemeral=True)
+
+    @discord.ui.button(label="공지 새로고침", style=discord.ButtonStyle.secondary, row=0)
+    async def refresh_notice(self, interaction: discord.Interaction, button: Button):
+        match = manager.get_match(self.match_id)
+        if not match:
+            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
+            return
+        await refresh_match_announcement(self.match_id)
+        await interaction.response.send_message("공지 메시지를 새로고침했습니다.", ephemeral=True)
+
+    @discord.ui.button(label="드래프트", style=discord.ButtonStyle.success, row=1)
+    async def start_draft(self, interaction: discord.Interaction, button: Button):
+        match = manager.get_match(self.match_id)
+        if not match:
+            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
+            return
+        if len(match.unique_users()) < 2:
+            await interaction.response.send_message("캡틴으로 선택할 수 있는 인원이 부족합니다.", ephemeral=True)
+            return
+        await interaction.response.send_message("첫 번째 캡틴을 선택하세요.", view=CaptainOneSelectView(self.match_id), ephemeral=True)
+
+    @discord.ui.button(label="결과 기록", style=discord.ButtonStyle.success, row=1)
+    async def record_result(self, interaction: discord.Interaction, button: Button):
+        if self.match_id not in manager.last_teams:
+            await interaction.response.send_message("먼저 드래프트를 완료해주세요.", ephemeral=True)
+            return
+        await interaction.response.send_message("승리 팀을 선택하세요.", view=ResultTeamSelectView(self.match_id), ephemeral=True)
+
+    @discord.ui.button(label="내전 삭제", style=discord.ButtonStyle.danger, row=1)
+    async def delete_match(self, interaction: discord.Interaction, button: Button):
+        await interaction.response.send_message(
+            f"`{self.match_id}`번 내전을 삭제할까요?",
+            view=ConfirmDeleteMatchView(self.match_id),
+            ephemeral=True,
+        )
+
+
+class MatchAnnouncementView(View):
+    def __init__(self, match_id: int):
+        super().__init__(timeout=None)
+        self.match_id = match_id
+        match = manager.get_match(match_id)
+        if match and len(match.waiting_list) >= MATCH_CAPACITY:
+            self.join_match.disabled = True
+
+    @discord.ui.button(label="참여하기", style=discord.ButtonStyle.success, custom_id="volt_join_match", row=0)
+    async def join_match(self, interaction: discord.Interaction, button: Button):
+        match = manager.get_match(self.match_id)
+        if not match:
+            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
+            return
+        if len(match.waiting_list) >= MATCH_CAPACITY:
+            await interaction.response.send_message("정원이 가득 찼습니다.", ephemeral=True)
+            return
+        if not ALLOW_DUPLICATE_SIGNUPS and match.has_user(interaction.user.id):
+            await interaction.response.send_message("이미 이 내전에 신청되어 있습니다.", ephemeral=True)
+            return
+        await interaction.response.send_message(
+            f"`{self.match_id}`번 내전 신청을 진행합니다. 티어를 선택해주세요.",
+            view=TierSelectView(self.match_id),
+            ephemeral=True,
+        )
+
+    @discord.ui.button(label="신청취소", style=discord.ButtonStyle.danger, custom_id="volt_cancel_match", row=0)
+    async def cancel_match(self, interaction: discord.Interaction, button: Button):
+        success, message = manager.unregister_player(self.match_id, interaction.user.id)
+        if success:
+            await refresh_match_announcement(self.match_id)
+        await interaction.response.send_message(message, ephemeral=True)
+
+    @discord.ui.button(label="명단 보기", style=discord.ButtonStyle.secondary, custom_id="volt_view_match", row=0)
+    async def view_match(self, interaction: discord.Interaction, button: Button):
+        match = manager.get_match(self.match_id)
+        if not match:
+            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
+            return
+        await interaction.response.send_message(embed=build_match_embed(match), ephemeral=True)
+
+    @discord.ui.button(label="운영 메뉴", style=discord.ButtonStyle.primary, custom_id="volt_manage_match", row=1)
+    async def manage_match(self, interaction: discord.Interaction, button: Button):
+        if not interaction.user.guild_permissions.administrator:
+            await interaction.response.send_message("운영 메뉴는 관리자만 사용할 수 있습니다.", ephemeral=True)
+            return
+        match = manager.get_match(self.match_id)
+        if not match:
+            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
+            return
+        await interaction.response.send_message(embed=build_manage_embed(match), view=MatchManageView(self.match_id), ephemeral=True)
+
+
+class CreateMatchModal(Modal, title="내전 생성"):
+    title_input = TextInput(label="내전 제목", placeholder="예: 저녁 8시 내전", max_length=100)
+
+    async def on_submit(self, interaction: discord.Interaction):
+        title = str(self.title_input).strip()
+        if not title:
+            await interaction.response.send_message("내전 제목을 입력해주세요.", ephemeral=True)
+            return
+        if interaction.channel is None:
+            await interaction.response.send_message("채널 정보를 찾을 수 없습니다.", ephemeral=True)
+            return
+
+        match_id = manager.create_match(title)
+        match = manager.get_match(match_id)
+        if not match:
+            await interaction.response.send_message("내전 생성 중 오류가 발생했습니다.", ephemeral=True)
+            return
+
+        await interaction.response.defer(ephemeral=True)
+        announcement = await interaction.channel.send(
+            content=build_match_announcement_content(match),
+            embed=build_match_announcement_embed(match),
+            view=MatchAnnouncementView(match_id),
+        )
+        match.announcement_channel_id = announcement.channel.id
+        match.announcement_message_id = announcement.id
+        await interaction.followup.send(f"`{match_id}`번 내전 공지를 생성했습니다.", ephemeral=True)
+
+
+class AdminPanelView(View):
+    def __init__(self):
+        super().__init__(timeout=300)
+
+    @discord.ui.button(label="내전 생성", style=discord.ButtonStyle.success, row=0)
+    async def create_match(self, interaction: discord.Interaction, button: Button):
+        if not interaction.user.guild_permissions.administrator:
+            await interaction.response.send_message("이 기능은 관리자만 사용할 수 있습니다.", ephemeral=True)
+            return
+        await interaction.response.send_modal(CreateMatchModal())
+
+    @discord.ui.button(label="내전 관리", style=discord.ButtonStyle.primary, row=0)
+    async def manage_match(self, interaction: discord.Interaction, button: Button):
+        if not interaction.user.guild_permissions.administrator:
+            await interaction.response.send_message("이 기능은 관리자만 사용할 수 있습니다.", ephemeral=True)
+            return
+        await interaction.response.send_message(
+            "관리할 내전을 선택하세요.",
+            view=MatchPickerView("manage"),
+            ephemeral=True,
+        )
+
+    @discord.ui.button(label="내전 목록", style=discord.ButtonStyle.secondary, row=0)
+    async def list_matches(self, interaction: discord.Interaction, button: Button):
+        await interaction.response.send_message(embed=build_match_list_embed(manager.list_matches()), ephemeral=True)
+
+    @discord.ui.button(label="도움말", style=discord.ButtonStyle.secondary, row=0)
+    async def show_help(self, interaction: discord.Interaction, button: Button):
+        await interaction.response.send_message(embed=build_help_embed(), ephemeral=True)
+
+
+class RankMenuView(View):
+    def __init__(self):
+        super().__init__(timeout=180)
+
+    @discord.ui.button(label="내 랭킹", style=discord.ButtonStyle.primary, row=0)
+    async def my_rank(self, interaction: discord.Interaction, button: Button):
+        await interaction.response.send_message("조회 기준을 선택하세요.", view=RankSelectView(is_all=False), ephemeral=True)
+
+    @discord.ui.button(label="전체 랭킹", style=discord.ButtonStyle.primary, row=0)
+    async def all_rank(self, interaction: discord.Interaction, button: Button):
+        await interaction.response.send_message("정렬 기준을 선택하세요.", view=RankSelectView(is_all=True), ephemeral=True)
+
+    @discord.ui.button(label="점수표", style=discord.ButtonStyle.secondary, row=0)
+    async def score_info(self, interaction: discord.Interaction, button: Button):
+        await interaction.response.send_message(embed=build_score_embed(), ephemeral=True)
+
+
+class MainPanelView(View):
+    def __init__(self):
+        super().__init__(timeout=300)
+
+    @discord.ui.button(label="참여 신청", style=discord.ButtonStyle.success, row=0)
+    async def apply_match(self, interaction: discord.Interaction, button: Button):
+        await interaction.response.send_message("참여할 내전을 선택하세요.", view=MatchPickerView("join"), ephemeral=True)
+
+    @discord.ui.button(label="신청 취소", style=discord.ButtonStyle.danger, row=0)
+    async def cancel_match(self, interaction: discord.Interaction, button: Button):
+        await interaction.response.send_message(
+            "신청 취소할 내전을 선택하세요.",
+            view=MatchPickerView("cancel", user_id=interaction.user.id),
+            ephemeral=True,
+        )
+
+    @discord.ui.button(label="명단 보기", style=discord.ButtonStyle.secondary, row=0)
+    async def view_roster(self, interaction: discord.Interaction, button: Button):
+        await interaction.response.send_message("명단을 볼 내전을 선택하세요.", view=MatchPickerView("view"), ephemeral=True)
+
+    @discord.ui.button(label="내전 목록", style=discord.ButtonStyle.secondary, row=1)
+    async def list_matches(self, interaction: discord.Interaction, button: Button):
+        await interaction.response.send_message(embed=build_match_list_embed(manager.list_matches()), ephemeral=True)
+
+    @discord.ui.button(label="랭킹", style=discord.ButtonStyle.primary, row=1)
+    async def rank_menu(self, interaction: discord.Interaction, button: Button):
+        await interaction.response.send_message("랭킹 메뉴입니다.", view=RankMenuView(), ephemeral=True)
+
+    @discord.ui.button(label="도움말", style=discord.ButtonStyle.secondary, row=1)
+    async def help_menu(self, interaction: discord.Interaction, button: Button):
+        await interaction.response.send_message(embed=build_help_embed(), ephemeral=True)
+
+    @discord.ui.button(label="운영 패널", style=discord.ButtonStyle.primary, row=2)
+    async def admin_menu(self, interaction: discord.Interaction, button: Button):
+        if not interaction.user.guild_permissions.administrator:
+            await interaction.response.send_message("운영 패널은 관리자만 사용할 수 있습니다.", ephemeral=True)
+            return
+        await interaction.response.send_message("운영 패널을 열었습니다.", view=AdminPanelView(), ephemeral=True)
+
+
+class VoltBot(commands.Bot):
+    def __init__(self):
+        intents = discord.Intents.default()
+        intents.message_content = True
+        intents.members = True
+        super().__init__(command_prefix="!", intents=intents, help_command=None)
+
+    async def setup_hook(self):
+        init_db()
+
+    async def on_ready(self):
+        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")
+
+
+bot = VoltBot()
+
+
+@bot.command(name="1")
+async def open_panel(ctx: commands.Context):
+    await ctx.send(
+        embed=build_main_panel_embed(ctx.author, ctx.author.guild_permissions.administrator),
+        view=MainPanelView(),
+    )
+
+
+@bot.command(name="도움말")
+async def help_command(ctx: commands.Context):
+    await ctx.send(embed=build_help_embed(), view=MainPanelView())
+
+
+@bot.command(name="신청")
+async def join_command(ctx: commands.Context):
+    await ctx.send("참여할 내전을 선택하세요.", view=MatchPickerView("join"))
+
+
+@bot.command(name="신청취소")
+async def cancel_command(ctx: commands.Context, match_id: Optional[int] = None):
+    if match_id is None:
+        await ctx.send("신청 취소할 내전을 선택하세요.", view=MatchPickerView("cancel", user_id=ctx.author.id))
+        return
+    success, message = manager.unregister_player(match_id, ctx.author.id)
+    if success:
+        await refresh_match_announcement(match_id)
+    await ctx.send(message)
+
+
+@bot.command(name="내전목록")
+async def list_command(ctx: commands.Context):
+    await ctx.send(embed=build_match_list_embed(manager.list_matches()))
+
+
+@bot.command(name="명단")
+async def roster_command(ctx: commands.Context, match_id: Optional[int] = None):
+    if match_id is None:
+        await ctx.send("명단을 볼 내전을 선택하세요.", view=MatchPickerView("view"))
+        return
+    match = manager.get_match(match_id)
+    if not match:
+        await ctx.send("해당 번호의 내전이 없습니다.")
+        return
+    await ctx.send(embed=build_match_embed(match))
+
+
+@bot.command(name="내랭킹")
+async def my_rank_command(ctx: commands.Context):
+    await ctx.send("조회 기준을 선택하세요.", view=RankSelectView(is_all=False))
+
+
+@bot.command(name="전체랭킹")
+async def all_rank_command(ctx: commands.Context):
+    await ctx.send("정렬 기준을 선택하세요.", view=RankSelectView(is_all=True))
+
+
+@bot.command(name="점수표")
+async def score_command(ctx: commands.Context):
+    await ctx.send(embed=build_score_embed())
+
+
+@bot.command(name="내전생성")
+@commands.has_permissions(administrator=True)
+async def create_match_command(ctx: commands.Context, *, title: str):
+    match_id = manager.create_match(title)
+    match = manager.get_match(match_id)
+    if not match:
+        await ctx.send("내전 생성 중 오류가 발생했습니다.")
+        return
+
+    announcement = await ctx.send(
+        content=build_match_announcement_content(match),
+        embed=build_match_announcement_embed(match),
+        view=MatchAnnouncementView(match_id),
+    )
+    match.announcement_channel_id = announcement.channel.id
+    match.announcement_message_id = announcement.id
+
+
+@bot.command(name="내전삭제")
+@commands.has_permissions(administrator=True)
+async def delete_match_command(ctx: commands.Context, match_id: int):
+    match = manager.get_match(match_id)
+    if not match:
+        await ctx.send("해당 번호의 내전이 없습니다.")
+        return
+    await close_match_announcement(match, "이 내전 모집은 종료되었습니다.")
+    manager.close_match(match_id)
+    await ctx.send(f"`{match_id}`번 내전을 삭제했습니다.")
+
+
+@bot.command(name="내전종료")
+@commands.has_permissions(administrator=True)
+async def close_match_command(ctx: commands.Context, match_id: int):
+    await delete_match_command(ctx, match_id)
+
+
+@bot.command(name="명단수정")
+@commands.has_permissions(administrator=True)
+async def edit_roster_command(ctx: commands.Context, match_id: Optional[int] = None):
+    if match_id is None:
+        await ctx.send("관리할 내전을 선택하세요.", view=MatchPickerView("manage"))
+        return
+    match = manager.get_match(match_id)
+    if not match:
+        await ctx.send("해당 번호의 내전이 없습니다.")
+        return
+    if not match.list_players():
+        await ctx.send("현재 참가자가 없어 수정할 명단이 없습니다.")
+        return
+    await ctx.send("제외할 인원을 누르세요.", view=EditListView(match_id))
+
+
+@bot.command(name="드래프트")
+@commands.has_permissions(administrator=True)
+async def draft_command(ctx: commands.Context, match_id: int, captain1_raw: str, captain2_raw: str):
+    match = manager.get_match(match_id)
+    if not match:
+        await ctx.send("해당 번호의 내전이 없습니다.")
+        return
+
+    captain1 = await resolve_member_from_text(ctx.guild, captain1_raw)
+    captain2 = await resolve_member_from_text(ctx.guild, captain2_raw)
+    if captain1 is None or captain2 is None:
+        await ctx.send("캡틴을 찾지 못했습니다. 실제 멘션, 닉네임, 유저 ID 중 하나를 사용해주세요.")
+        return
+    if captain1.id == captain2.id:
+        await ctx.send("캡틴 두 명은 서로 다른 사람이어야 합니다.")
+        return
+    if not match.has_user(captain1.id) or not match.has_user(captain2.id):
+        await ctx.send("캡틴은 반드시 해당 내전 신청자여야 합니다.")
+        return
+
+    all_players = match.list_players()
+    if len(all_players) != MATCH_CAPACITY:
+        await ctx.send(f"드래프트는 정확히 {MATCH_CAPACITY}명이 모였을 때만 시작할 수 있습니다. 현재 {len(all_players)}명입니다.")
+        return
+
+    draft_pool = build_draft_pool(all_players, captain1.id, captain2.id)
+    if len(draft_pool) != 8:
+        await ctx.send(f"드래프트 대상 인원 수가 올바르지 않습니다. 현재 드래프트 가능 인원은 {len(draft_pool)}명입니다.")
+        return
+
+    draft_view = DraftView(match_id, captain1, captain2, draft_pool)
+    await ctx.send(
+        f"{captain1.display_name}님과 {captain2.display_name}님의 드래프트를 시작합니다.",
+        embed=draft_view.make_embed(),
+        view=draft_view,
+    )
+
+
+@bot.command(name="결과기록")
+@commands.has_permissions(administrator=True)
+async def result_command(ctx: commands.Context, match_id: int, win_team: int):
+    success, message = await record_match_result(match_id, win_team)
+    if success:
+        await ctx.send(message, view=MatchManageView(match_id))
+    else:
+        await ctx.send(message)
+
+
+@bot.event
+async def on_command_error(ctx: commands.Context, error):
+    if isinstance(error, commands.MissingRequiredArgument):
+        await ctx.send(f"입력값이 부족합니다. `!도움말` 또는 `!1`로 사용법을 확인해주세요. ({error.param.name})")
+        return
+    if isinstance(error, commands.BadArgument):
+        await ctx.send("입력 형식이 올바르지 않습니다. 멘션 또는 숫자 값을 다시 확인해주세요.")
+        return
+    if isinstance(error, commands.MissingPermissions):
+        await ctx.send("이 명령어를 사용할 권한이 없습니다.")
+        return
+    if isinstance(error, commands.CommandNotFound):
+        return
+
+    logger.exception("Unhandled command error", exc_info=error)
+    await ctx.send("명령 처리 중 오류가 발생했습니다. 로그를 확인해주세요.")
+
+
+def validate_environment() -> bool:
+    required = {"DISCORD_TOKEN": TOKEN, "DATABASE_URL": DATABASE_URL}
+    missing = [name for name, value in required.items() if not value]
+    if missing:
+        logger.error("Missing required environment variables: %s", ", ".join(missing))
+        return False
+    return True
+
+
+if __name__ == "__main__":
+    if not validate_environment():
+        raise SystemExit(1)
+    init_db()
+    keep_alive()
+    bot.run(TOKEN)
