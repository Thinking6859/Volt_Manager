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
import random
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

def get_db_conn(): return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    conn = get_db_conn()
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
    conn.commit()
    cur.close()
    conn.close()

# --- [2] 드래프트 전 전용 View (실시간 버튼 선택) ---
class DraftView(View):
    def __init__(self, cap1, cap2, players):
        super().__init__(timeout=600)
        self.captains = [cap1, cap2]
        self.turn = 0 # 0: 주장1, 1: 주장2
        self.players = players
        self.teams = [[], []]
        self.update_buttons()

    def make_embed(self):
        embed = discord.Embed(title="⚔️ VOLT 리얼타임 드래프트", color=0x9b59b6)
        t1_names = "\n".join([f"• {p['name']} ({p['main']})" for p in self.teams[0]]) or "지명 대기 중..."
        t2_names = "\n".join([f"• {p['name']} ({p['main']})" for p in self.teams[1]]) or "지명 대기 중..."
        embed.add_field(name=f"🔵 1팀 ({self.captains[0].display_name})", value=t1_names, inline=True)
        embed.add_field(name=f"🔴 2팀 ({self.captains[1].display_name})", value=t2_names, inline=True)
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
        picked = self.players[p_idx]
        self.teams[self.turn].append(picked)
        
        self.turn = 1 - self.turn # 턴 교체
        
        if (len(self.teams[0]) + len(self.teams[1])) >= 10:
            bot.last_teams = {"team1": self.teams[0], "team2": self.teams[1]}
            embed = self.make_embed()
            embed.title = "✅ 드래프트 종료 - 팀 확정"
            embed.color = 0x2ecc71
            mentions = " ".join([p['mention'] for p in self.teams[0] + self.teams[1]])
            await interaction.response.edit_message(content=f"📢 **팀 구성 완료! 전장으로!**\n{mentions}", embed=embed, view=None)
        else:
            self.update_buttons()
            await interaction.response.edit_message(content=f"🗳️ **{self.captains[self.turn].mention}** 주장님 차례!", embed=self.make_embed(), view=self)

# --- [3] 기존 신청 UI (티어/라인 선택) ---
class VoltMatchView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.user_data = {}

    @discord.ui.select(placeholder="1단계: 티어 선택", options=[discord.SelectOption(label=t, value=t) for t in TIER_DATA.keys()])
    async def select_tier(self, interaction: discord.Interaction, select: Select):
        await interaction.response.defer(ephemeral=True)
        self.user_data[interaction.user.id] = {'tier': select.values[0]}
        await interaction.followup.send("확인! **주 라인**을 고르세요.", ephemeral=True)

    @discord.ui.select(placeholder="2단계: 주 라인", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿"]])
    async def select_main(self, interaction: discord.Interaction, select: Select):
        await interaction.response.defer(ephemeral=True)
        self.user_data[interaction.user.id]['main'] = select.values[0]
        await interaction.followup.send("마지막! **부 라인**을 고르세요.", ephemeral=True)

    @discord.ui.select(placeholder="3단계: 부 라인", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿","상관없음"]])
    async def select_sub(self, interaction: discord.Interaction, select: Select):
        await interaction.response.defer(ephemeral=True)
        uid = interaction.user.id
        data = self.user_data.get(uid)
        bot.waiting_list[uid] = {
            "name": interaction.user.display_name, "mention": interaction.user.mention,
            "tier": data['tier'], "main": data['main'], "sub": select.values[0],
            "score": TIER_DATA[data['tier']], "time": datetime.now()
        }
        await interaction.followup.send(f"✅ 신청 완료! ({len(bot.waiting_list)}명 대기)", ephemeral=True)

# --- [4] 봇 본체 ---
class VoltBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix='!', intents=intents, help_command=None)
        self.waiting_list = {}
        self.last_teams = None

    async def setup_hook(self):
        init_db()
        self.scheduler = AsyncIOScheduler(timezone=KST)
        self.scheduler.add_job(self.auto_match_open, CronTrigger(day_of_week='tue,thu,sat', hour=11, minute=0))
        self.scheduler.start()

    async def auto_match_open(self):
        channel = discord.utils.get(self.get_all_channels(), name="내전-신청")
        if channel:
            self.waiting_list.clear()
            embed = discord.Embed(title="⚡ VOLT 정기 내전 모집", description="화/목/토 11시 정기 내전입니다!", color=0x00FFFF)
            await channel.send(content="@everyone 신청 시작!", embed=embed, view=VoltMatchView())

bot = VoltBot()

# --- [5] 모든 명령어 집합 ---
@bot.command()
@commands.has_permissions(administrator=True)
async def 드래프트(ctx, cap1: discord.Member, cap2: discord.Member):
    """지정한 두 주장이 클릭으로 팀원을 데려감"""
    if len(bot.waiting_list) < 10: return await ctx.send("신청자가 10명 미만입니다.")
    players = sorted(bot.waiting_list.values(), key=lambda x: x['time'])[:10]
    view = DraftView(cap1, cap2, players)
    await ctx.send(content=f"🗳️ **{cap1.mention}** 주장님부터 지명을 시작하세요!", embed=view.make_embed(), view=view)

@bot.command()
@commands.has_permissions(administrator=True)
async def 결과기록(ctx, winner: int):
    """승리팀 전적 반영 (DB 저장)"""
    if not bot.last_teams: return await ctx.send("확정된 팀 정보가 없습니다.")
    conn = get_db_conn(); cur = conn.cursor()
    win_t = bot.last_teams[f"team{winner}"]; lose_t = bot.last_teams[f"team{3-winner}"]
    for p in win_t:
        cur.execute("INSERT INTO volt_rank (user_id, name, wins, points) VALUES (%s,%s,1,10) ON CONFLICT (user_id) DO UPDATE SET wins=volt_rank.wins+1, points=volt_rank.points+10", (p['mention'], p['name']))
    for p in lose_t:
        cur.execute("INSERT INTO volt_rank (user_id, name, losses, points) VALUES (%s,%s,0,5) ON CONFLICT (user_id) DO UPDATE SET losses=volt_rank.losses+1, points=volt_rank.points+5", (p['mention'], p['name']))
    conn.commit(); cur.close(); conn.close()
    bot.last_teams = None
    await ctx.send(f"🏆 {winner}팀 승리 기록 완료! (포인트 반영됨)")

@bot.command()
async def 랭킹(ctx):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT name, wins, losses, points FROM volt_rank ORDER BY points DESC LIMIT 10")
    rows = cur.fetchall(); cur.close(); conn.close()
    if not rows: return await ctx.send("기록이 없습니다.")
    embed = discord.Embed(title="🏅 VOLT 랭킹 TOP 10", color=0xFFD700)
    for i, (n, w, l, p) in enumerate(rows, 1):
        rate = (w/(w+l)*100) if (w+l)>0 else 0
        embed.add_field(name=f"{i}위: {n}", value=f"{p}pt | {w}승 {l}패 ({rate:.1f}%)", inline=False)
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def 내전생성(ctx):
    bot.waiting_list.clear()
    await ctx.send(embed=discord.Embed(title="🔥 긴급 내전 모집", color=0xFF0000), view=VoltMatchView())

@bot.command()
async def 명단(ctx):
    if not bot.waiting_list: return await ctx.send("신청자가 없습니다.")
    msg = "\n".join([f"- {p['name']} [{p['tier']}]" for p in bot.waiting_list.values()])
    await ctx.send(f"📋 **현재 대기 명단**\n{msg}")

if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
