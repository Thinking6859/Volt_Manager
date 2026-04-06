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

# --- [1] 서버 및 DB 설정 ---
app = Flask('')
@app.route('/')
def home(): return "⚡ VOLT Omni-System is Online!"
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

# --- [2] 실시간 드래프트 View ---
class DraftView(View):
    def __init__(self, cap1, cap2, players):
        super().__init__(timeout=600)
        self.captains = [cap1, cap2]
        self.turn = 0
        self.players = players
        self.teams = [[], []]
        self.update_buttons()

    def make_embed(self):
        embed = discord.Embed(title="⚔️ VOLT 실시간 드래프트", color=0x9b59b6)
        t1 = "\n".join([f"• {p['name']} ({p['main']})" for p in self.teams[0]]) or "지명 중..."
        t2 = "\n".join([f"• {p['name']} ({p['main']})" for p in self.teams[1]]) or "지명 중..."
        embed.add_field(name=f"🔵 1팀 ({self.captains[0].display_name})", value=t1, inline=True)
        embed.add_field(name=f"🔴 2팀 ({self.captains[1].display_name})", value=t2, inline=True)
        embed.set_footer(text=f"현재 차례: {self.captains[self.turn].display_name}")
        return embed

    def update_buttons(self):
        self.clear_items()
        for i, p in enumerate(self.players):
            if any(p in t for t in self.teams): continue
            btn = Button(label=f"{p['name']} ({p['main']})", style=discord.ButtonStyle.secondary, custom_id=str(i))
            btn.callback = self.pick_callback
            self.add_item(btn)

    async def pick_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.captains[self.turn].id:
            return await interaction.response.send_message("본인 차례가 아닙니다!", ephemeral=True)
        p_idx = int(interaction.data['custom_id'])
        self.teams[self.turn].append(self.players[p_idx])
        self.turn = 1 - self.turn
        if (len(self.teams[0]) + len(self.teams[1])) >= 10:
            bot.last_teams = {"team1": self.teams[0], "team2": self.teams[1]}
            mentions = " ".join([p['mention'] for p in self.teams[0] + self.teams[1]])
            await interaction.response.edit_message(content=f"✅ **팀 구성 완료!**\n{mentions}", embed=self.make_embed(), view=None)
        else:
            self.update_buttons()
            await interaction.response.edit_message(content=f"🗳️ **{self.captains[self.turn].mention}**님 차례!", embed=self.make_embed(), view=self)

# --- [3] 신청 UI ---
class VoltMatchView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.temp = {}

    @discord.ui.select(placeholder="1단계: 티어 선택", options=[discord.SelectOption(label=t, value=t) for t in TIER_DATA.keys()])
    async def select_tier(self, interaction: discord.Interaction, select: Select):
        await interaction.response.defer(ephemeral=True)
        self.temp[interaction.user.id] = {'tier': select.values[0]}
        await interaction.followup.send("확인! **주 라인**을 선택하세요.", ephemeral=True)

    @discord.ui.select(placeholder="2단계: 주 라인", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿"]])
    async def select_main(self, interaction: discord.Interaction, select: Select):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id not in self.temp: return
        self.temp[interaction.user.id]['main'] = select.values[0]
        await interaction.followup.send("마지막! **부 라인**을 선택하세요.", ephemeral=True)

    @discord.ui.select(placeholder="3단계: 부 라인", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿","상관없음"]])
    async def select_sub(self, interaction: discord.Interaction, select: Select):
        await interaction.response.defer(ephemeral=True)
        uid = interaction.user.id
        data = self.temp.get(uid)
        bot.waiting_list[uid] = {
            "name": interaction.user.display_name, "mention": interaction.user.mention,
            "tier": data['tier'], "main": data['main'], "sub": select.values[0],
            "score": TIER_DATA[data['tier']], "time": datetime.now()
        }
        await interaction.followup.send(f"✅ 신청 완료! ({len(bot.waiting_list)}명 대기)", ephemeral=True)

# --- [4] 봇 본체 ---
class VoltBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default(); intents.message_content = True; intents.members = True
        super().__init__(command_prefix='!', intents=intents, help_command=None)
        self.waiting_list = {}; self.last_teams = None

    async def setup_hook(self):
        init_db()
        self.scheduler = AsyncIOScheduler(timezone=KST)
        self.scheduler.add_job(self.auto_match_open, CronTrigger(day_of_week='tue,thu,sat', hour=11, minute=0))
        self.scheduler.start()

    async def auto_match_open(self):
        channel = discord.utils.get(self.get_all_channels(), name="내전-신청")
        if channel:
            self.waiting_list.clear()
            await channel.send(content="@everyone 정기 내전 모집 시작!", embed=discord.Embed(title="⚡ VOLT 정기 내전", color=0x00FFFF), view=VoltMatchView())

bot = VoltBot()

# --- [5] 모든 명령어 ---
@bot.command()
async def 도움말(ctx):
    embed = discord.Embed(title="⚡ VOLT 클랜 봇 가이드", color=0x00FF00)
    embed.add_field(name="👤 일반 유저", value="`!명단` - 현재 신청 인원 확인\n`!랭킹` - 클랜 랭킹 TOP 10\n`!내정보` - 본인 전적 확인\n`!도움말` - 명령어 안내", inline=False)
    embed.add_field(name="🛠️ 운영진 전용", value="`!내전생성` - 수동 모집 공지\n`!드래프트 @주장1 @주장2` - 실시간 지명 시작\n`!결과기록 [1/2]` - 승리팀 반영\n`!강제취소 [이름]` - 신청자 제거\n`!초기화` - 신청 명단 싹 비우기", inline=False)
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def 드래프트(ctx, cap1: discord.Member, cap2: discord.Member):
    if len(bot.waiting_list) < 10: return await ctx.send("신청자가 10명 미만이라 드래프트를 할 수 없습니다.")
    players = sorted(bot.waiting_list.values(), key=lambda x: x['time'])[:10]
    view = DraftView(cap1, cap2, players)
    await ctx.send(content=f"🗳️ **{cap1.mention}**님부터 지명 시작!", embed=view.make_embed(), view=view)

@bot.command()
@commands.has_permissions(administrator=True)
async def 결과기록(ctx, winner: int):
    if not bot.last_teams: return await ctx.send("확정된 팀 정보가 없습니다. 드래프트를 먼저 하세요.")
    conn = get_db_conn()
    if not conn: return await ctx.send("❌ DB 연결 실패로 전적을 기록할 수 없습니다.")
    cur = conn.cursor()
    win_t = bot.last_teams[f"team{winner}"]; lose_t = bot.last_teams[f"team{3-winner}"]
    for p in win_t: cur.execute("INSERT INTO volt_rank (user_id, name, wins, points) VALUES (%s,%s,1,10) ON CONFLICT (user_id) DO UPDATE SET wins=volt_rank.wins+1, points=volt_rank.points+10", (p['mention'], p['name']))
    for p in lose_t: cur.execute("INSERT INTO volt_rank (user_id, name, losses, points) VALUES (%s,%s,0,5) ON CONFLICT (user_id) DO UPDATE SET losses=volt_rank.losses+1, points=volt_rank.points+5", (p['mention'], p['name']))
    conn.commit(); cur.close(); conn.close(); bot.last_teams = None
    await ctx.send(f"🏆 {winner}팀 승리 기록 완료! (승리 10pt / 패배 5pt 반영)")

@bot.command()
async def 랭킹(ctx):
    conn = get_db_conn()
    if not conn: return await ctx.send("❌ DB 연결 실패")
    cur = conn.cursor()
    cur.execute("SELECT name, wins, losses, points FROM volt_rank ORDER BY points DESC LIMIT 10")
    rows = cur.fetchall(); cur.close(); conn.close()
    if not rows: return await ctx.send("아직 기록된 랭킹이 없습니다.")
    embed = discord.Embed(title="🏅 VOLT 클랜 TOP 10 랭킹", color=0xFFD700)
    for i, (n, w, l, p) in enumerate(rows, 1):
        rate = (w / (w + l) * 100) if (w + l) > 0 else 0
        embed.add_field(name=f"{i}위: {n}", value=f"{p}pt | {w}승 {l}패 ({rate:.1f}%)", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def 내정보(ctx):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT wins, losses, points FROM volt_rank WHERE user_id = %s", (ctx.author.mention,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row: return await ctx.send("등록된 전적이 없습니다.")
    w, l, p = row
    rate = (w / (w + l) * 100) if (w + l) > 0 else 0
    await ctx.send(f"📊 **{ctx.author.display_name}**님의 전적\n- 포인트: {p}pt\n- 승패: {w}승 {l}패 (승률 {rate:.1f}%)")

@bot.command()
@commands.has_permissions(administrator=True)
async def 내전생성(ctx):
    bot.waiting_list.clear(); await ctx.send(embed=discord.Embed(title="🔥 긴급 내전 모집", color=0xFF0000), view=VoltMatchView())

@bot.command()
async def 명단(ctx):
    if not bot.waiting_list: return await ctx.send("현재 신청자가 없습니다.")
    msg = "\n".join([f"- {p['name']} [{p['tier']}] {p['main']}/{p['sub']}" for p in bot.waiting_list.values()])
    await ctx.send(f"📋 **현재 신청 명단 ({len(bot.waiting_list)}명)**\n{msg}")

@bot.command()
@commands.has_permissions(administrator=True)
async def 강제취소(ctx, name: str):
    found = next((u for u, p in bot.waiting_list.items() if p['name'] == name), None)
    if found: del bot.waiting_list[found]; await ctx.send(f"🧹 `{name}`님을 명단에서 제외했습니다.")
    else: await ctx.send("명단에서 해당 이름을 찾을 수 없습니다.")

@bot.command()
@commands.has_permissions(administrator=True)
async def 초기화(ctx):
    bot.waiting_list.clear(); await ctx.send("✅ 신청 명단이 초기화되었습니다.")

if __name__ == "__main__":
    keep_alive(); bot.run(TOKEN)
