import discord
from discord.ext import commands
from discord.ui import Button, View, Select
import os
import psycopg2
import random
from flask import Flask
from threading import Thread

# --- [1] Render 잠자기 방지용 웹 서버 (Flask) ---
app = Flask('')

@app.route('/')
def home():
    return "⚡ VOLT Bot is Running 24/7!"

def run():
    # Render는 기본적으로 8080 포트를 사용합니다.
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --- [2] 환경 변수 및 DB 설정 ---
TOKEN = os.getenv('DISCORD_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')

def get_db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS lol_stats (
            user_id TEXT PRIMARY KEY,
            display_name TEXT,
            games INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()

# --- [3] 봇 설정 ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# 신청자 명단 (휘발성)
waiting_list = {}

# --- [4] UI 컴포넌트 (신청 버튼) ---
class RegisterView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.temp_data = {}

    @discord.ui.select(placeholder="티어를 선택하세요", options=[
        discord.SelectOption(label="아이언~브론즈", value="I-B"),
        discord.SelectOption(label="실버~골드", value="S-G"),
        discord.SelectOption(label="플래티넘~에메랄드", value="P-E"),
        discord.SelectOption(label="다이아 이상", value="D+")
    ])
    async def select_tier(self, interaction: discord.Interaction, select: Select):
        self.temp_data[interaction.user.id] = {'tier': select.values[0]}
        await interaction.response.send_message(f"티어 [{select.values[0]}] 확인! 라인을 골라주세요.", ephemeral=True)

    @discord.ui.select(placeholder="주 라인을 선택하세요", options=[
        discord.SelectOption(label="탑", emoji="⚔️"),
        discord.SelectOption(label="정글", emoji="🌲"),
        discord.SelectOption(label="미드", emoji="🧙"),
        discord.SelectOption(label="원딜", emoji="🏹"),
        discord.SelectOption(label="서폿", emoji="🛡️")
    ])
    async def select_lane(self, interaction: discord.Interaction, select: Select):
        uid = interaction.user.id
        if uid not in self.temp_data:
            return await interaction.response.send_message("티어를 먼저 선택해주세요!", ephemeral=True)
        
        tier = self.temp_data[uid]['tier']
        lane = select.values[0]
        
        waiting_list[uid] = {"name": interaction.user.display_name, "tier": tier, "lane": lane}
        await interaction.response.send_message(f"✅ 신청 완료! ({tier} / {lane})", ephemeral=True)

# --- [5] 명령어 구현 ---
@bot.event
async def on_ready():
    init_db()
    print(f'Logged in as {bot.user.name}')

@bot.command()
async def 도움말(ctx):
    embed = discord.Embed(title="⚡ VOLT 내전 시스템 도움말", color=0x00ffff)
    embed.add_field(name="!신청", value="버튼으로 티어/라인 선택 (중복 체크 포함)", inline=False)
    embed.add_field(name="!명단", value="현재 대기 중인 인원 확인", inline=False)
    embed.add_field(name="!전적", value="본인의 참여 횟수와 승률 확인", inline=False)
    embed.add_field(name="🛠️ 관리자 명령어", value="!팀뽑 / !결과기록 [승리팀번호] / !초기화", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def 신청(ctx):
    await ctx.send("내전 참가를 위해 정보를 입력해주세요!", view=RegisterView())

@bot.command()
async def 명단(ctx):
    if not waiting_list: return await ctx.send("현재 신청자가 없습니다.")
    text = "\n".join([f"• {p['name']} ({p['tier']}/{p['lane']})" for p in waiting_list.values()])
    await ctx.send(f"📋 **현재 신청 명단 ({len(waiting_list)}명)**\n{text}")

@bot.command()
@commands.has_permissions(administrator=True)
async def 팀뽑(ctx):
    if len(waiting_list) < 10:
        return await ctx.send(f"인원이 부족합니다. (현재 {len(waiting_list)}/10)")

    uids = list(waiting_list.keys())
    random.shuffle(uids)
    cap1, cap2 = uids[0], uids[1]
    others = uids[2:]
    
    embed = discord.Embed(title="🎲 팀 드래프트 시작", color=0xffaa00)
    embed.add_field(name="1팀 주장", value=waiting_list[cap1]['name'])
    embed.add_field(name="2팀 주장", value=waiting_list[cap2]['name'])
    embed.add_field(name="남은 인원", value=", ".join([waiting_list[uid]['name'] for uid in others]), inline=False)
    embed.add_field(name="📌 드래프트 순서 (1-2-2-2-1)", value="1팀(1) -> 2팀(2) -> 1팀(2) -> 2팀(2) -> 1팀(1)")
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def 결과기록(ctx, winner: int):
    conn = get_db_conn()
    cur = conn.cursor()
    for uid, data in waiting_list.items():
        cur.execute('''
            INSERT INTO lol_stats (user_id, display_name, games, wins)
            VALUES (%s, %s, 1, 0)
            ON CONFLICT (user_id) DO UPDATE SET games = lol_stats.games + 1
        ''', (str(uid), data['name']))
    conn.commit()
    cur.close()
    conn.close()
    await ctx.send(f"🏆 {winner}팀 승리! 참가자 전원의 판수가 기록되었습니다.")

@bot.command()
@commands.has_permissions(administrator=True)
async def 초기화(ctx):
    waiting_list.clear()
    await ctx.send("🧹 명단이 초기화되었습니다.")

# --- [6] 실행 ---
if __name__ == "__main__":
    keep_alive() # Flask 서버 시작
    bot.run(TOKEN)
