import discord
from discord.ext import commands
from discord.ui import View, Select, Button
import os
import psycopg2
from flask import Flask
from threading import Thread
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from datetime import datetime
import uuid 

# --- [1] 서버 및 DB 설정 ---
app = Flask('')
@app.route('/')
def home(): return "⚡ VOLT System is Online!"
def run(): app.run(host='0.0.0.0', port=8080)
def keep_alive(): Thread(target=run).start()

DATABASE_URL = os.getenv('DATABASE_URL')
TOKEN = os.getenv('DISCORD_TOKEN')
KST = pytz.timezone('Asia/Seoul')

TIER_DATA = {
    "아이언": 1, "브론즈": 2, "실버": 3, "골드": 4, "플래티넘": 5,
    "에메랄드": 6, "다이아몬드": 8, "마스터": 10, "그랜드마스터": 12, "챌린저": 15
}

def get_db_conn():
    try:
        if not DATABASE_URL: return None
        return psycopg2.connect(DATABASE_URL, connect_timeout=5)
    except: return None

def init_db():
    conn = get_db_conn()
    if conn:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS volt_rank (
                user_id TEXT PRIMARY KEY,
                name TEXT,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                points INTEGER DEFAULT 0
            )
        ''')
        conn.commit(); cur.close(); conn.close()

# --- [2] 멀티 내전 관리자 ---
class MatchManager:
    def __init__(self):
        self.matches = {} 
        self.match_count = 0
        self.last_teams = {} 

    def create_match(self, title):
        self.match_count += 1
        self.matches[self.match_count] = {
            "title": title,
            "created_at": datetime.now(KST),
            "waiting_list": {} 
        }
        return self.match_count

manager = MatchManager()

# --- [3] 신청 UI ---
class MatchSelectView(View):
    def __init__(self):
        super().__init__(timeout=60)
        options = [
            discord.SelectOption(label=f"[{id}] {m['title']}", value=str(id), description=f"{len(m['waiting_list'])}명 신청 중")
            for id, m in manager.matches.items()
        ]
        if options:
            select = Select(placeholder="신청할 내전을 선택하세요", options=options)
            select.callback = self.match_selected
            self.add_item(select)

    async def match_selected(self, interaction: discord.Interaction):
        match_id = int(interaction.data['values'][0])
        await interaction.response.send_message(f"✅ {match_id}번 내전 선택!", view=TierSelectView(match_id), ephemeral=True)

class TierSelectView(View):
    def __init__(self, match_id):
        super().__init__(timeout=60)
        self.match_id = match_id

    @discord.ui.select(placeholder="티어 선택", options=[discord.SelectOption(label=t, value=t) for t in TIER_DATA.keys()])
    async def tier_callback(self, interaction: discord.Interaction, select: Select):
        await interaction.response.send_message("주 라인을 선택하세요.", view=PositionSelectView(self.match_id, select.values[0]), ephemeral=True)

class PositionSelectView(View):
    def __init__(self, match_id, tier):
        super().__init__(timeout=120)
        self.match_id, self.tier, self.main_pos = match_id, tier, None

    @discord.ui.select(placeholder="주 라인", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿"]])
    async def main_callback(self, interaction: discord.Interaction, select: Select):
        await interaction.response.defer(ephemeral=True)
        self.main_pos = select.values[0]
        self.clear_items()
        sub = Select(placeholder="부 라인", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿","상관없음"]])
        sub.callback = self.final_callback
        self.add_item(sub)
        await interaction.edit_original_response(content=f"주 라인 **[{self.main_pos}]** 선택됨. 부 라인을 골라주세요.", view=self)

    async def final_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        sub_pos = interaction.data['values'][0]
        match = manager.matches.get(self.match_id)
        if match:
            entry_id = str(uuid.uuid4())
            match['waiting_list'][entry_id] = {
                "user_id": interaction.user.id,
                "name": f"{interaction.user.display_name}_{len(match['waiting_list'])+1}", 
                "mention": interaction.user.mention,
                "tier": self.tier, "main": self.main_pos, "sub": sub_pos, "time": datetime.now()
            }
            await interaction.edit_original_response(content=f"🎉 **{match['title']}** 신청 완료!", view=None)

# --- [4] 1-2-2-2-1 드래프트 시스템 ---
class DraftView(View):
    def __init__(self, match_id, cap1, cap2, players):
        super().__init__(timeout=600)
        self.match_id, self.captains = match_id, [cap1, cap2]
        self.players, self.teams = players, [[], []]
        self.pick_sequence = [0, 1, 1, 0, 0, 1, 1, 0] 
        self.current_step = 0
        self.update_buttons()

    def make_embed(self):
        embed = discord.Embed(title=f"⚔️ {manager.matches[self.match_id]['title']} 드래프트", color=0x9b59b6)
        t1_list = [f"⭐ {self.captains[0].display_name} (주장)"] + [f"• {p['name']} ({p['tier']}/{p['main']})" for p in self.teams[0]]
        t2_list = [f"⭐ {self.captains[1].display_name} (주장)"] + [f"• {p['name']} ({p['tier']}/{p['main']})" for p in self.teams[1]]
        embed.add_field(name=f"🔵 1팀 ({len(t1_list)}/5)", value="\n".join(t1_list), inline=True)
        embed.add_field(name=f"🔴 2팀 ({len(t2_list)}/5)", value="\n".join(t2_list), inline=True)
        if self.current_step < len(self.pick_sequence):
            current_cap = self.captains[self.pick_sequence[self.current_step]]
            embed.set_footer(text=f"지명 차례: {current_cap.display_name}")
        return embed

    def update_buttons(self):
        self.clear_items()
        for i, p in enumerate(self.players):
            if any(p is t for t in self.teams[0] + self.teams[1]): continue
            label_text = f"[{p['tier']}] {p['name']} ({p['main']}/{p['sub']})"
            btn = Button(label=label_text, style=discord.ButtonStyle.secondary, custom_id=str(i))
            btn.callback = self.pick_callback
            self.add_item(btn)

    async def pick_callback(self, interaction: discord.Interaction):
        current_turn_cap_idx = self.pick_sequence[self.current_step]
        if interaction.user.id != self.captains[current_turn_cap_idx].id:
            return await interaction.response.send_message("본인 차례가 아닙니다!", ephemeral=True)
        p_idx = int(interaction.data['custom_id'])
        self.teams[current_turn_cap_idx].append(self.players[p_idx])
        self.current_step += 1
        if self.current_step >= len(self.pick_sequence):
            team1_final = [{"name": self.captains[0].display_name, "mention": self.captains[0].mention, "user_id": self.captains[0].id}] + self.teams[0]
            team2_final = [{"name": self.captains[1].display_name, "mention": self.captains[1].mention, "user_id": self.captains[1].id}] + self.teams[1]
            manager.last_teams[self.match_id] = {"team1": team1_final, "team2": team2_final}
            await interaction.response.edit_message(content="✅ **팀 구성 완료!**", embed=self.make_embed(), view=None)
        else:
            self.update_buttons(); await interaction.response.edit_message(embed=self.make_embed(), view=self)

# --- [5] 봇 본체 ---
class VoltBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default(); intents.message_content = True; intents.members = True
        super().__init__(command_prefix='!', intents=intents, help_command=None)

    async def setup_hook(self):
        init_db()
        self.scheduler = AsyncIOScheduler(timezone=KST)
        self.scheduler.start()

bot = VoltBot()

# --- [6] 명령어 ---
@bot.command()
async def 신청(ctx):
    if not manager.matches: return await ctx.send("현재 열려있는 내전이 없습니다.")
    await ctx.send("참여할 내전을 선택하세요!", view=MatchSelectView())

@bot.command()
@commands.has_permissions(administrator=True)
async def 내전생성(ctx, *, title: str):
    mid = manager.create_match(title)
    await ctx.send(f"🔥 **{mid}번 내전: {title}** 생성 완료! `!신청` 하세요.")

@bot.command()
@commands.has_permissions(administrator=True)
async def 내전삭제(ctx, match_id: int):
    if manager.matches.pop(match_id, None):
        manager.last_teams.pop(match_id, None)
        await ctx.send(f"🧹 {match_id}번 내전이 삭제되었습니다.")
    else: await ctx.send("해당 번호의 내전이 없습니다.")

@bot.command()
@commands.has_permissions(administrator=True)
async def 명단초기화(ctx, match_id: int):
    m = manager.matches.get(match_id)
    if m:
        m['waiting_list'] = {}
        await ctx.send(f"🔄 {match_id}번 내전 명단이 초기화되었습니다.")
    else: await ctx.send("내전을 찾을 수 없습니다.")

@bot.command()
async def 명단(ctx, match_id: int):
    m = manager.matches.get(match_id)
    if not m: return await ctx.send("내전을 찾을 수 없습니다.")
    msg = "\n".join([f"- {p['name']} [{p['tier']}] {p['main']}/{p['sub']}" for p in m['waiting_list'].values()])
    await ctx.send(f"📋 **{match_id}번 명단 ({len(m['waiting_list'])}명)**\n{msg or '신청자 없음'}")

@bot.command()
@commands.has_permissions(administrator=True)
async def 드래프트(ctx, match_id: int, cap1: discord.Member, cap2: discord.Member):
    m = manager.matches.get(match_id)
    if not m or len(m['waiting_list']) < 8: return await ctx.send("지명할 인원이 부족합니다. (최소 8명 필요)")
    players = sorted(m['waiting_list'].values(), key=lambda x: x['time'])[:8]
    await ctx.send(f"🗳️ {match_id}번 드래프트 시작!", view=DraftView(match_id, cap1, cap2, players))

@bot.command()
@commands.has_permissions(administrator=True)
async def 결과기록(ctx, match_id: int, winner: int):
    teams = manager.last_teams.get(match_id)
    if not teams: return await ctx.send("기록할 팀 정보가 없습니다.")
    conn = get_db_conn(); cur = conn.cursor()
    win_t, lose_t = teams[f"team{winner}"], teams[f"team{3-winner}"]
    for p in win_t: cur.execute("INSERT INTO volt_rank (user_id, name, wins, points) VALUES (%s,%s,1,10) ON CONFLICT (user_id) DO UPDATE SET wins=volt_rank.wins+1, points=volt_rank.points+10", (str(p.get('user_id', p['mention'])), p['name']))
    for p in lose_t: cur.execute("INSERT INTO volt_rank (user_id, name, losses, points) VALUES (%s,%s,0,5) ON CONFLICT (user_id) DO UPDATE SET losses=volt_rank.losses+1, points=volt_rank.points+5", (str(p.get('user_id', p['mention'])), p['name']))
    conn.commit(); cur.close(); conn.close()
    await ctx.send(f"🏆 {match_id}번 결과 기록 완료!")

@bot.command()
async def 랭킹(ctx):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT name, wins, losses, points FROM volt_rank ORDER BY points DESC LIMIT 10")
    rows = cur.fetchall(); cur.close(); conn.close()
    embed = discord.Embed(title="🏅 VOLT TOP 10 (포인트 랭킹)", color=0xFFD700)
    for i, (n, w, l, p) in enumerate(rows, 1): 
        rate = (w / (w + l) * 100) if (w + l) > 0 else 0
        embed.add_field(name=f"{i}위: {n}", value=f"**{p}pt** | {w}승 {l}패 ({rate:.1f}%)", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def 도움말(ctx):
    embed = discord.Embed(
        title="⚡ VOLT 클랜 시스템 가이드",
        description="클랜 내전 및 랭킹 관리를 위한 명령어 모음입니다.",
        color=0x00FF00,
        timestamp=datetime.now(KST)
    )
    
    embed.add_field(
        name="👤 일반 유저 명령어",
        value=(
            "**`!신청`**\n└ 현재 진행 중인 내전에 참여 신청을 합니다.\n"
            "**`!명단 [번호]`**\n└ 특정 내전의 신청 현황과 티어 정보를 확인합니다.\n"
            "**`!랭킹`**\n└ 포인트 기준 클랜 상위 10명을 표시합니다.\n"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🛠️ 운영진 관리 명령어",
        value=(
            "**`!내전생성 [제목]`**\n└ 새로운 내전 방을 개설합니다.\n"
            "**`!내전삭제 [번호]`**\n└ 생성된 내전 방을 완전히 삭제합니다.\n"
            "**`!명단초기화 [번호]`**\n└ 방은 유지한 채 신청자 명단만 비웁니다.\n"
            "**`!드래프트 [번호] @주장1 @주장2`**\n└ 1-2-2-2-1 방식의 팀 지명을 시작합니다.\n"
            "**`!결과기록 [번호] [1 또는 2]`**\n└ 승리팀을 지정하여 전적과 승점을 반영합니다.\n"
        ),
        inline=False
    )
    
    embed.set_footer(text="VOLT Clan Infrastructure Team", icon_url=ctx.me.display_avatar.url)
    await ctx.send(embed=embed)

if __name__ == "__main__":
    keep_alive(); bot.run(TOKEN)
