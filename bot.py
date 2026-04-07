import logging
import os
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from threading import Thread
from typing import Dict, List, Optional

import discord
import psycopg2
import pytz
from discord.ext import commands
from discord.ui import Button, Select, View
from flask import Flask


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("volt-bot")


app = Flask(__name__)


@app.route("/")
def home():
    return "VOLT System is Online!"


def run_web_server():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), use_reloader=False)


def keep_alive():
    Thread(target=run_web_server, daemon=True).start()


DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("DISCORD_TOKEN")
KST = pytz.timezone("Asia/Seoul")

TIER_DATA = {
    "아이언": 1,
    "브론즈": 2,
    "실버": 3,
    "골드": 4,
    "플래티넘": 5,
    "에메랄드": 6,
    "다이아몬드": 8,
    "마스터": 10,
    "그랜드마스터": 12,
    "챌린저": 15,
}
POSITIONS = ["탑", "정글", "미드", "원딜", "서폿"]
SUB_POSITIONS = [*POSITIONS, "상관없음"]
POINTS_WIN = 10
POINTS_ACTIVITY = 10
MATCH_CAPACITY = 10


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


ALLOW_DUPLICATE_SIGNUPS = env_flag("ALLOW_DUPLICATE_SIGNUPS", default=True)


def normalize_database_url(url: Optional[str]) -> Optional[str]:
    if url and url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
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

    with closing(conn), closing(conn.cursor()) as cur:
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
        conn.commit()
    return True


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
    created_at: str = field(default_factory=lambda: datetime_now_kst())
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


def datetime_now_kst():
    return discord.utils.utcnow().astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")


class MatchManager:
    def __init__(self):
        self.matches: Dict[int, Match] = {}
        self.match_count = 0
        self.last_teams = {}

    def create_match(self, title: str) -> int:
        self.match_count += 1
        self.matches[self.match_count] = Match(match_id=self.match_count, title=title)
        return self.match_count

    def get_match(self, match_id: int) -> Optional[Match]:
        return self.matches.get(match_id)

    def close_match(self, match_id: int) -> bool:
        removed = self.matches.pop(match_id, None)
        self.last_teams.pop(match_id, None)
        return removed is not None

    def register_player(
        self, match_id: int, user_id: int, name: str, tier: str, main: str, sub: str
    ):
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
        return True, "신청 완료"

    def unregister_player(self, match_id: int, user_id: int):
        match = self.get_match(match_id)
        if not match:
            return False, "존재하지 않는 내전입니다."
        entry_id = match.get_entry_id_by_user(user_id)
        if not entry_id:
            return False, "이 내전에 신청된 기록이 없습니다."
        player_name = match.waiting_list.pop(entry_id).name
        return True, f"{player_name}님의 신청이 취소되었습니다."


manager = MatchManager()


def build_draft_pool(players: List[PlayerEntry], captain1_id: int, captain2_id: int) -> List[PlayerEntry]:
    if not ALLOW_DUPLICATE_SIGNUPS:
        return [player for player in players if player.user_id not in {captain1_id, captain2_id}]

    pool = list(players)
    removed_captains = set()
    draft_pool = []

    for player in pool:
        if player.user_id == captain1_id and captain1_id not in removed_captains:
            removed_captains.add(captain1_id)
            continue
        if player.user_id == captain2_id and captain2_id not in removed_captains:
            removed_captains.add(captain2_id)
            continue
        draft_pool.append(player)

    return draft_pool


class RankSelectView(View):
    def __init__(self, is_all=False):
        super().__init__(timeout=60)
        self.is_all = is_all

    async def process_rank(self, interaction: discord.Interaction, sort_col: str, label: str):
        conn = get_db_conn()
        if not conn:
            await interaction.response.edit_message(
                content="DB 연결에 실패했습니다. 잠시 후 다시 시도해주세요.",
                view=None,
            )
            return

        order_by = sort_col if sort_col != "total" else "(points + activity_points)"
        with closing(conn), closing(conn.cursor()) as cur:
            cur.execute(
                f"""
                SELECT user_id, name, points, activity_points, wins, losses
                FROM volt_rank
                ORDER BY {order_by} DESC, wins DESC, name ASC
                """
            )
            rows = cur.fetchall()

        if not rows:
            await interaction.response.edit_message(
                content="아직 기록된 데이터가 없습니다.",
                view=None,
            )
            return

        if self.is_all:
            rank_list = []
            for i, row in enumerate(rows, start=1):
                value = (
                    row[2]
                    if sort_col == "points"
                    else row[3]
                    if sort_col == "activity_points"
                    else row[2] + row[3]
                )
                rank_list.append(f"**{i}위 {row[1]}** | {value}pt")
            embed = discord.Embed(
                title=f"VOLT 전체 랭킹 ({label} 기준)",
                description="\n".join(rank_list[:25]),
                color=0xFFD700,
            )
        else:
            user_data = next(
                ((i, row) for i, row in enumerate(rows, start=1) if row[0] == str(interaction.user.id)),
                None,
            )
            if not user_data:
                await interaction.response.edit_message(
                    content="해당 부문의 기록이 없습니다.",
                    view=None,
                )
                return

            rank_idx, row = user_data
            total_pts = row[2] + row[3]
            total_games = row[4] + row[5]
            win_rate = (row[4] / total_games * 100) if total_games else 0

            embed = discord.Embed(
                title=f"{row[1]}님의 랭킹 정보",
                color=0x3498DB,
            )
            embed.add_field(name=f"{label} 순위", value=f"**{rank_idx}위**", inline=True)
            embed.add_field(name="종합 점수", value=f"**{total_pts}pt**", inline=True)
            embed.add_field(
                name="상세 점수",
                value=f"승리: {row[2]}pt / 참여: {row[3]}pt",
                inline=False,
            )
            embed.add_field(
                name="전적",
                value=f"{row[4]}승 {row[5]}패 (승률 {win_rate:.1f}%)",
                inline=False,
            )

        await interaction.response.edit_message(content=None, embed=embed, view=None)

    @discord.ui.button(label="승리 점수", style=discord.ButtonStyle.success)
    async def victory_rank(self, interaction: discord.Interaction, button: Button):
        await self.process_rank(interaction, "points", "승리 점수")

    @discord.ui.button(label="참여 점수", style=discord.ButtonStyle.primary)
    async def activity_rank(self, interaction: discord.Interaction, button: Button):
        await self.process_rank(interaction, "activity_points", "참여 점수")

    @discord.ui.button(label="종합 점수", style=discord.ButtonStyle.secondary)
    async def total_rank(self, interaction: discord.Interaction, button: Button):
        await self.process_rank(interaction, "total", "종합 점수")


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
        for entry_id, player in match.waiting_list.items():
            button = Button(
                label=f"제외 {player.name}",
                style=discord.ButtonStyle.danger,
                custom_id=entry_id,
            )
            button.callback = self.delete_player
            self.add_item(button)

    async def delete_player(self, interaction: discord.Interaction):
        entry_id = interaction.data["custom_id"]
        match = manager.get_match(self.match_id)
        if not match or entry_id not in match.waiting_list:
            await interaction.response.edit_message(
                content="이미 제거되었거나 존재하지 않는 참가자입니다.",
                view=None,
            )
            return

        player_name = match.waiting_list.pop(entry_id).name
        await refresh_match_announcement(self.match_id)
        self.update_buttons()
        if self.children:
            await interaction.response.edit_message(
                content=f"{player_name}님이 명단에서 제외되었습니다.",
                view=self,
            )
        else:
            await interaction.response.edit_message(
                content=f"{player_name}님이 명단에서 제외되었습니다. 현재 남은 참가자가 없습니다.",
                view=None,
            )


class PostGameView(View):
    def __init__(self, match_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id

    @discord.ui.button(label="명단 수정", style=discord.ButtonStyle.primary)
    async def edit_list_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "제외할 인원을 클릭하세요.",
            view=EditListView(self.match_id),
            ephemeral=True,
        )

    @discord.ui.button(label="내전 종료", style=discord.ButtonStyle.danger)
    async def close_match(self, interaction: discord.Interaction, button: Button):
        match = manager.get_match(self.match_id)
        if match and match.announcement_channel_id and match.announcement_message_id:
            channel = interaction.client.get_channel(match.announcement_channel_id)
            if channel is None:
                try:
                    channel = await interaction.client.fetch_channel(match.announcement_channel_id)
                except Exception:
                    channel = None
            if channel is not None:
                try:
                    message = await channel.fetch_message(match.announcement_message_id)
                    await message.edit(content="이 내전 모집은 종료되었습니다.", view=None)
                except Exception:
                    logger.exception("Failed to close announcement message for match %s", self.match_id)

        manager.close_match(self.match_id)
        await interaction.response.edit_message(content="내전이 종료되었습니다.", view=None)


class DraftView(View):
    def __init__(self, match_id: int, captain1: discord.Member, captain2: discord.Member, players: List[PlayerEntry]):
        super().__init__(timeout=600)
        self.match_id = match_id
        self.captains = [captain1, captain2]
        self.players = players
        self.teams = [[], []]
        self.pick_seq = [0, 1, 1, 0, 0, 1, 1, 0]
        self.step = 0
        self.update_buttons()

    def make_embed(self):
        match = manager.get_match(self.match_id)
        title = match.title if match else f"{self.match_id}번 내전"
        embed = discord.Embed(title=f"{title} 드래프트", color=0x5865F2)
        team1 = [f"캡틴: **{self.captains[0].display_name}**"] + [f"• {player.name}" for player in self.teams[0]]
        team2 = [f"캡틴: **{self.captains[1].display_name}**"] + [f"• {player.name}" for player in self.teams[1]]
        embed.add_field(name="1팀", value="\n".join(team1), inline=True)
        embed.add_field(name="2팀", value="\n".join(team2), inline=True)
        if self.step < len(self.pick_seq):
            embed.set_footer(text=f"{self.captains[self.pick_seq[self.step]].display_name}님의 차례입니다.")
        return embed

    def update_buttons(self):
        self.clear_items()
        picked_ids = {player.entry_id for player in self.teams[0] + self.teams[1]}
        for index, player in enumerate(self.players):
            if player.entry_id in picked_ids:
                continue
            button = Button(
                label=f"[{player.tier}] {player.name}",
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
        if any(selected_player.entry_id == p.entry_id for p in self.teams[0] + self.teams[1]):
            await interaction.response.send_message("이미 선택된 플레이어입니다.", ephemeral=True)
            return

        self.teams[self.pick_seq[self.step]].append(selected_player)
        self.step += 1

        if self.step >= len(self.pick_seq):
            final_team1 = [
                {"name": self.captains[0].display_name, "user_id": self.captains[0].id}
            ] + [{"name": p.name, "user_id": p.user_id} for p in self.teams[0]]
            final_team2 = [
                {"name": self.captains[1].display_name, "user_id": self.captains[1].id}
            ] + [{"name": p.name, "user_id": p.user_id} for p in self.teams[1]]
            manager.last_teams[self.match_id] = {"team1": final_team1, "team2": final_team2}
            await interaction.response.edit_message(
                content="팀 구성이 완료되었습니다.",
                embed=self.make_embed(),
                view=None,
            )
            return

        self.update_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)


class PositionSelectView(View):
    def __init__(self, match_id: int, tier: str):
        super().__init__(timeout=120)
        self.match_id = match_id
        self.tier = tier
        self.main_position = None

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
        sub_position = interaction.data["values"][0]
        success, message = manager.register_player(
            self.match_id,
            interaction.user.id,
            interaction.user.display_name,
            self.tier,
            self.main_position,
            sub_position,
        )
        if success:
            await refresh_match_announcement(self.match_id)
            await interaction.response.edit_message(
                content=f"{interaction.user.display_name}님 신청이 완료되었습니다.",
                view=None,
            )
        else:
            await interaction.response.edit_message(content=message, view=None)


class MatchSelectView(View):
    def __init__(self):
        super().__init__(timeout=60)
        options = [
            discord.SelectOption(
                label=f"[{match_id}] {match.title} ({len(match.waiting_list)}/{MATCH_CAPACITY})",
                value=str(match_id),
            )
            for match_id, match in manager.matches.items()
        ]
        if options:
            select = Select(placeholder="참여할 내전을 선택하세요.", options=options)
            select.callback = self.match_selected
            self.add_item(select)

    async def match_selected(self, interaction: discord.Interaction):
        match_id = int(interaction.data["values"][0])
        await interaction.response.send_message(
            f"{match_id}번 내전을 선택했습니다. 티어를 골라주세요.",
            view=TierSelectView(match_id),
            ephemeral=True,
        )


class TierSelectView(View):
    def __init__(self, match_id: int):
        super().__init__(timeout=60)
        self.match_id = match_id

    @discord.ui.select(
        placeholder="티어를 선택하세요.",
        options=[discord.SelectOption(label=tier, value=tier) for tier in TIER_DATA.keys()],
    )
    async def tier_callback(self, interaction: discord.Interaction, select: Select):
        await interaction.response.send_message(
            "라인을 선택해주세요.",
            view=PositionSelectView(self.match_id, select.values[0]),
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


def build_match_embed(match: Match):
    embed = discord.Embed(
        title=f"{match.title} 명단",
        description=f"방 번호: {match.match_id} | 생성 시간: {match.created_at}",
        color=0x3498DB,
    )
    players = match.list_players()
    if not players:
        embed.add_field(name="참여자", value="아직 참여자가 없습니다.", inline=False)
    else:
        player_lines = [
            f"• **{player.name}** [{player.tier}] ({player.main}/{player.sub})"
            for player in players
        ]
        embed.add_field(
            name=f"참여자 ({len(players)}/{MATCH_CAPACITY})",
            value="\n".join(player_lines),
            inline=False,
        )
    return embed


def build_match_announcement_embed(match: Match):
    is_full = len(match.waiting_list) >= MATCH_CAPACITY
    embed = discord.Embed(
        title=f"내전 모집 | {match.title}",
        description=(
            f"방 번호: `{match.match_id}`\n"
            f"생성 시간: {match.created_at}\n"
            + (
                "현재 정원이 가득 차 모집이 마감되었습니다."
                if is_full
                else "아래 `참여하기` 버튼을 눌러 바로 신청할 수 있습니다."
            )
        ),
        color=0x5865F2,
    )
    players = match.list_players()
    if players:
        preview = "\n".join(
            f"• {player.name} [{player.tier}] ({player.main}/{player.sub})"
            for player in players[:10]
        )
        embed.add_field(
            name=f"현재 참가자 ({len(players)}/{MATCH_CAPACITY})",
            value=preview,
            inline=False,
        )
    else:
        embed.add_field(
            name=f"현재 참가자 (0/{MATCH_CAPACITY})",
            value="아직 신청자가 없습니다.",
            inline=False,
        )
    footer = "테스트 모드: 중복 신청 허용 중" if ALLOW_DUPLICATE_SIGNUPS else "중복 신청은 자동으로 막힙니다."
    if is_full:
        footer += " | 신청취소 시 다시 열립니다."
    embed.set_footer(text=footer)
    return embed


def build_match_announcement_content(match: Match):
    status = "모집 마감" if len(match.waiting_list) >= MATCH_CAPACITY else "모집 중"
    return f"@here 내전 모집이 시작되었습니다. [{status}]"


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


class MatchAnnouncementView(View):
    def __init__(self, match_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        match = manager.get_match(match_id)
        if match and len(match.waiting_list) >= MATCH_CAPACITY:
            self.join_match.disabled = True

    @discord.ui.button(label="참여하기", style=discord.ButtonStyle.success, custom_id="volt_join_match")
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

    @discord.ui.button(label="신청취소", style=discord.ButtonStyle.danger, custom_id="volt_cancel_match")
    async def cancel_match(self, interaction: discord.Interaction, button: Button):
        success, message = manager.unregister_player(self.match_id, interaction.user.id)
        if success:
            await refresh_match_announcement(self.match_id)
        await interaction.response.send_message(
            message if success else f"취소 실패: {message}",
            ephemeral=True,
        )

    @discord.ui.button(label="명단 보기", style=discord.ButtonStyle.secondary, custom_id="volt_view_match_list")
    async def view_list(self, interaction: discord.Interaction, button: Button):
        match = manager.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("이미 종료되었거나 존재하지 않는 내전입니다.", ephemeral=True)
            return
        await interaction.response.send_message(embed=build_match_embed(match), ephemeral=True)


@bot.command()
async def 신청(ctx: commands.Context):
    if not manager.matches:
        await ctx.send("현재 열린 내전이 없습니다.")
        return
    await ctx.send("참여할 내전을 선택해주세요.", view=MatchSelectView())


@bot.command()
async def 신청취소(ctx: commands.Context, match_id: int):
    success, message = manager.unregister_player(match_id, ctx.author.id)
    if success:
        await refresh_match_announcement(match_id)
    await ctx.send(message if success else f"취소 실패: {message}")


@bot.command()
async def 내전목록(ctx: commands.Context):
    if not manager.matches:
        await ctx.send("현재 진행 중인 내전이 없습니다.")
        return

    lines = [
        f"• `{match_id}`번 | {match.title} | {len(match.waiting_list)}/{MATCH_CAPACITY}명"
        for match_id, match in manager.matches.items()
    ]
    await ctx.send("진행 중인 내전 목록입니다.\n" + "\n".join(lines))


@bot.command()
async def 명단(ctx: commands.Context, match_id: int):
    match = manager.get_match(match_id)
    if not match:
        await ctx.send("해당 번호의 내전이 없습니다.")
        return
    await ctx.send(embed=build_match_embed(match))


@bot.command()
async def 내랭킹(ctx: commands.Context):
    await ctx.send("조회 기준을 선택해주세요.", view=RankSelectView(is_all=False))


@bot.command()
async def 전체랭킹(ctx: commands.Context):
    await ctx.send("정렬 기준을 선택해주세요.", view=RankSelectView(is_all=True))


@bot.command()
async def 점수표(ctx: commands.Context):
    await ctx.send(
        f"승리 시 `{POINTS_WIN}`점, 참여 시 `{POINTS_ACTIVITY}`점이 지급됩니다. 종합 점수는 승리 점수와 참여 점수의 합산입니다."
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def 내전생성(ctx: commands.Context, *, title: str):
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

    await ctx.send(f"내전이 생성되었습니다. 방 번호: `{match_id}` | 제목: `{title}`")


@bot.command()
@commands.has_permissions(administrator=True)
async def 내전종료(ctx: commands.Context, match_id: int):
    match = manager.get_match(match_id)
    if not match:
        await ctx.send("해당 번호의 내전이 없습니다.")
        return

    if match.announcement_channel_id and match.announcement_message_id:
        channel = bot.get_channel(match.announcement_channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(match.announcement_channel_id)
            except Exception:
                channel = None
        if channel is not None:
            try:
                message = await channel.fetch_message(match.announcement_message_id)
                await message.edit(content="이 내전 모집은 종료되었습니다.", view=None)
            except Exception:
                logger.exception("Failed to close announcement message for match %s", match_id)

    manager.close_match(match_id)
    await ctx.send(f"`{match_id}`번 내전이 종료되었습니다.")


@bot.command()
@commands.has_permissions(administrator=True)
async def 드래프트(ctx: commands.Context, match_id: int, captain1: discord.Member, captain2: discord.Member):
    match = manager.get_match(match_id)
    if not match:
        await ctx.send("해당 번호의 내전이 없습니다.")
        return
    if captain1.id == captain2.id:
        await ctx.send("캡틴 두 명은 서로 다른 사람이어야 합니다.")
        return

    all_players = match.list_players()
    if len(all_players) != MATCH_CAPACITY:
        await ctx.send(f"드래프트는 정확히 {MATCH_CAPACITY}명이 모였을 때만 시작할 수 있습니다. 현재 {len(all_players)}명입니다.")
        return

    if not match.has_user(captain1.id) or not match.has_user(captain2.id):
        await ctx.send("캡틴은 반드시 해당 내전 신청자여야 합니다.")
        return

    draft_pool = build_draft_pool(all_players, captain1.id, captain2.id)
    if len(draft_pool) != 8:
        await ctx.send(
            f"드래프트 대상 인원 수가 올바르지 않습니다. 현재 드래프트 가능 인원은 {len(draft_pool)}명입니다. 신청 명단을 다시 확인해주세요."
        )
        return

    draft_view = DraftView(match_id, captain1, captain2, draft_pool)
    await ctx.send(
        f"{captain1.display_name}님과 {captain2.display_name}님의 드래프트를 시작합니다.",
        embed=draft_view.make_embed(),
        view=draft_view,
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def 결과기록(ctx: commands.Context, match_id: int, win_team: int):
    if win_team not in (1, 2):
        await ctx.send("승리 팀 번호는 1 또는 2만 입력할 수 있습니다.")
        return

    teams = manager.last_teams.get(match_id)
    if not teams:
        await ctx.send("기록할 팀 데이터가 없습니다. 먼저 드래프트를 완료해주세요.")
        return

    conn = get_db_conn()
    if not conn:
        await ctx.send("DB 연결 실패로 결과를 기록하지 못했습니다.")
        return

    lose_team = 2 if win_team == 1 else 1
    with closing(conn), closing(conn.cursor()) as cur:
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

    await ctx.send("결과 기록이 완료되었습니다.", view=PostGameView(match_id))


@bot.command()
async def 도움말(ctx: commands.Context):
    embed = discord.Embed(title="VOLT 봇 도움말", color=0x2ECC71)
    embed.add_field(
        name="유저 명령어",
        value="`!신청` `!신청취소 <방번호>` `!내전목록` `!명단 <방번호>` `!내랭킹` `!전체랭킹` `!점수표`",
        inline=False,
    )
    embed.add_field(
        name="운영진 명령어",
        value="`!내전생성 <제목>` `!드래프트 <방번호> <캡틴1> <캡틴2>` `!결과기록 <방번호> <승리팀번호>` `!내전종료 <방번호>`",
        inline=False,
    )
    embed.set_footer(text="예시: !결과기록 1 2")
    await ctx.send(embed=embed)


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"입력값이 부족합니다. `!도움말`로 사용법을 확인해주세요. ({error.param.name})")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send("입력 형식이 올바르지 않습니다. 멘션 또는 숫자 값을 다시 확인해주세요.")
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("이 명령어를 사용할 권한이 없습니다.")
        return
    if isinstance(error, commands.CommandNotFound):
        return

    logger.exception("Unhandled command error", exc_info=error)
    await ctx.send("명령 처리 중 오류가 발생했습니다. 로그를 확인해주세요.")


def validate_environment():
    missing = [name for name, value in {"DISCORD_TOKEN": TOKEN, "DATABASE_URL": DATABASE_URL}.items() if not value]
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
