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
def home(): return "⚡ VOLT Test Mode is Online!"
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
        embed = discord.Embed(title="⚔️ VOLT 실시간 드래프트 (테스트)", color=0x9b59b6)
        t1 = "\n".join([f"• {p['name']}" for p in self.teams[0]]) or "지명 중..."
        t2 = "\n".join([f"• {p['name']}" for p in self.teams[1]]) or "지명 중..."
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
            return await interaction.response.send_message(f"❌ {self.captains[self.turn].display_name} 차례입니다!", ephemeral=True)
        
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

# --- [3] 신청 UI (중복 신청 허용 로직) ---
class VoltMatchView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.temp_user_data = {}

    @discord.ui.select(placeholder="1단계: 티어 선택", options=[discord.SelectOption(label=t, value=t) for t in TIER_DATA.keys()])
    async def select_tier(self, interaction: discord.Interaction, select: Select):
        await interaction.response.defer(ephemeral=True)
        # 테스트를 위해 유저별 고유 세션 ID 생성
        session_id = f"{interaction.user.id}_{uuid.uuid4().hex[:4]}"
        self.temp_user_data[interaction.user.id] = {'tier': select.values[0], 'session': session_id}
        await interaction.followup.send(f"티어 확인! ({len(bot.waiting_list)+1}번째 인원)", ephemeral=True)

    @discord.ui.select(placeholder="2단계: 주 라인", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿"]])
    async def select_main(self, interaction: discord.Interaction, select: Select):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id not in self.temp_user_data: return
        self.temp_user_data[interaction.user.id]['main'] = select.values[0]
        await interaction.followup.send("주 라인 확인! 부 라인을 고르세요.", ephemeral=True)

    @discord.ui.select(placeholder="3단계: 부 라인", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿","상관없음"]])
    async def select_sub(self, interaction: discord.Interaction, select: Select):
        await interaction.response.defer(ephemeral=True)
        uid = interaction.user.id
        data = self.temp_user_data.get(uid)
        session_id = data['session']
        
        # 중복 등록 가능하도록 고유 ID로 저장
        bot.waiting_list[session_id] = {
            "name": f"{interaction.user.display_name}_{len(bot.waiting_list)+1}",
            "mention": interaction.user.mention,
            "tier": data['tier'], "main": data['main'], "sub": select.values[0],
            "score": TIER_DATA[data['tier']], "time": datetime.now()
        }
        await interaction.followup.send(f"✅ 테스트 인원 추가! (총 {len(bot.waiting_list)}명)", ephemeral=True)

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
            await channel.send(content="@everyone 테스트 모집!", embed=discord.Embed(title="🧪 테스트 모드 모집", color=0x00FFFF), view=VoltMatchView())

bot = VoltBot()

# --- [5] 명령어 세트 ---
@bot.command()
async def 도움말(ctx):
    embed = discord.Embed(title="⚡ VOLT 테스트 가이드", color=0x00FF00)
    embed.add_field(name="🧪 테스트 방법", value="1. `!내전생성` 후 혼자서 10번 신청\n2. `!명단` 확인\n3. `!드래프트 @본인 @본인` 입력\n4. 버튼 번갈아 가며 누르기", inline=False)
    embed.add_field(name="🛠️ 명령어", value="`!명단`, `!랭킹`, `!내전생성`, `!드래프트 @주장1 @주장2`, `!결과기록 [1/2]`, `!초기화`", inline=False)
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def 드래프트(ctx, cap1: discord.Member, cap2: discord.Member):
    if len(bot.waiting_list) < 10: return await ctx.send(f"인원이 부족합니다! 현재 {len(bot.waiting_list)}/10")
    # 선착순 10명만 가져와서 드래프트
    players = list(bot.waiting_list.values())[:10]
    view = DraftView(cap1, cap2, players)
    await ctx.send(content=f"🗳️ **{cap1.mention}**님부터 지명 시작!", embed=view.make_embed(), view=view)

@bot.command()
@commands.has_permissions(administrator=True)
async def 결과기록(ctx, winner: int):
    if not bot.last_teams: return await ctx.send("확정된 팀 정보가 없습니다.")
    conn = get_db_conn()
    if not conn: return await ctx.send("❌ DB 연결 실패")
    cur = conn.cursor()
    win_t = bot.last_teams[f"team{winner}"]; lose_t = bot.last_teams[f"team{3-winner}"]
    for p in win_t: cur.execute("INSERT INTO volt_rank (user_id, name, wins, points) VALUES (%s,%s,1,10) ON CONFLICT (user_id) DO UPDATE SET wins=volt_rank.wins+1, points=volt_rank.points+10", (p['mention'], p['name']))
    for p in lose_t: cur.execute("INSERT INTO volt_rank (user_id, name, losses, points) VALUES (%s,%s,0,5) ON CONFLICT (user_id) DO UPDATE SET losses=volt_rank.losses+1, points=volt_rank.points+5", (p['mention'], p['name']))
    conn.commit(); cur.close(); conn.close(); bot.last_teams = None
    await ctx.send(f"🏆 {winner}팀 승리 기록 완료! (테스트)")

@bot.command()
async def 명단(ctx):
    if not bot.waiting_list: return await ctx.send("신청자가 없습니다.")
    msg = "\n".join([f"- {p['name']} [{p['tier']}]" for p in bot.waiting_list.values()])
    await ctx.send(f"📋 **현재 신청 명단 ({len(bot.waiting_list)}/10)**\n{msg}")

@bot.command()
@commands.has_permissions(administrator=True)
async def 내전생성(ctx):
    bot.waiting_list.clear(); await ctx.send(embed=discord.Embed(title="🧪 테스트 내전 생성", color=0xFF0000), view=VoltMatchView())

@bot.command()
@commands.has_permissions(administrator=True)
async def 초기화(ctx):
    bot.waiting_list.clear(); await ctx.send("✅ 명단 초기화 완료.")

if __name__ == "__main__":
    keep_alive(); bot.run(TOKEN)
