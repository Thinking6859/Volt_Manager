import discord
from discord.ext import commands
from discord.ui import View, Select
import os
import psycopg2
from flask import Flask
from threading import Thread
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# --- [1] 잠자기 방지용 웹 서버 (Flask) ---
app = Flask('')
@app.route('/')
def home(): return "⚡ VOLT Clan System is Online!"

def run(): app.run(host='0.0.0.0', port=8080)
def keep_alive(): Thread(target=run).start()

# --- [2] DB 및 환경 설정 ---
DATABASE_URL = os.getenv('DATABASE_URL')
TOKEN = os.getenv('DISCORD_TOKEN')
KST = pytz.timezone('Asia/Seoul')

# 티어별 가중치 (팀 밸런스용 점수)
TIER_DATA = {
    "아이언": 1, "브론즈": 2, "실버": 3, "골드": 4, "플래티넘": 5,
    "에메랄드": 6, "다이아몬드": 8, "마스터": 10, "그랜드마스터": 12, "챌린저": 15
}

def get_db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

# --- [3] 신청 UI 컴포넌트 ---
class VoltMatchView(View):
    def __init__(self):
        super().__init__(timeout=None) # 24시간 유지
        self.user_data = {}

    @discord.ui.select(
        placeholder="1단계: 본인의 티어를 선택하세요",
        options=[discord.SelectOption(label=t, value=t) for t in TIER_DATA.keys()]
    )
    async def select_tier(self, interaction: discord.Interaction, select: Select):
        self.user_data[interaction.user.id] = {'tier': select.values[0]}
        await interaction.response.send_message(f"[{select.values[0]}] 확인! 이제 **주 라인**을 선택하세요.", ephemeral=True)

    @discord.ui.select(
        placeholder="2단계: 주 라인 (1순위) 선택",
        options=[discord.SelectOption(label=l, value=l) for l in ["탑", "정글", "미드", "원딜", "서폿"]]
    )
    async def select_main(self, interaction: discord.Interaction, select: Select):
        if interaction.user.id not in self.user_data:
            return await interaction.response.send_message("티어를 먼저 선택해주세요!", ephemeral=True)
        self.user_data[interaction.user.id]['main'] = select.values[0]
        await interaction.response.send_message(f"주 라인 [{select.values[0]}] 확인! 마지막으로 **부 라인**을 선택하세요.", ephemeral=True)

    @discord.ui.select(
        placeholder="3단계: 부 라인 (2순위) 선택",
        options=[discord.SelectOption(label=l, value=l) for l in ["탑", "정글", "미드", "원딜", "서폿", "상관없음"]]
    )
    async def select_sub(self, interaction: discord.Interaction, select: Select):
        uid = interaction.user.id
        if 'main' not in self.user_data.get(uid, {}):
            return await interaction.response.send_message("이전 단계를 먼저 완료해주세요!", ephemeral=True)
        
        tier = self.user_data[uid]['tier']
        main = self.user_data[uid]['main']
        sub = select.values[0]
        
        # 전역 명단에 저장 (메모리 방식)
        bot.waiting_list[uid] = {
            "name": interaction.user.display_name,
            "tier": tier,
            "main": main,
            "sub": sub,
            "score": TIER_DATA[tier]
        }
        await interaction.response.send_message(f"✅ **신청 완료!**\n티어: {tier} / 라인: {main}(주), {sub}(부)", ephemeral=True)

# --- [4] 봇 클래스 및 스케줄러 ---
class VoltBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix='!', intents=intents, help_command=None)
        self.waiting_list = {} # {user_id: data}

    async def setup_hook(self):
        # 스케줄러 설정 (화, 목, 토 오전 11시)
        self.scheduler = AsyncIOScheduler(timezone=KST)
        self.scheduler.add_job(self.auto_match_open, CronTrigger(day_of_week='tue,thu,sat', hour=11, minute=0))
        self.scheduler.start()
        print("Scheduler Started (Tue, Thu, Sat 11:00 AM KST)")

    async def auto_match_open(self):
        # '내전-신청' 채널을 찾아서 공지 발송
        channel = discord.utils.get(self.get_all_channels(), name="내전-신청")
        if channel:
            self.waiting_list.clear() # 기존 명단 초기화
            embed = discord.Embed(
                title="⚡ VOLT 정기 내전 모집 (화/목/토)",
                description="오늘 오전 11시 정기 내전 신청을 시작합니다!\n아래 드롭다운 메뉴를 통해 정보를 입력해주세요.",
                color=0x00FFFF
            )
            await channel.send(embed=embed, view=VoltMatchView())

bot = VoltBot()

# --- [5] 명령어 설정 ---
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")

@bot.command()
@commands.has_permissions(administrator=True)
async def 내전생성(ctx):
    """운영진 전용 수동 내전 생성"""
    bot.waiting_list.clear()
    embed = discord.Embed(
        title="🔥 VOLT 긴급 내전 모집",
        description="운영진이 생성한 내전입니다. 지금 바로 신청하세요!",
        color=0xFF0000
    )
    await ctx.send(embed=embed, view=VoltMatchView())

@bot.command()
async def 명단(ctx):
    if not bot.waiting_list:
        return await ctx.send("현재 신청자가 없습니다. `!신청` 또는 공지 버튼을 이용하세요!")
    
    embed = discord.Embed(title=f"📋 현재 내전 대기열 ({len(bot.waiting_list)}명)", color=0x00FF00)
    for uid, p in bot.waiting_list.items():
        embed.add_field(name=p['name'], value=f"T: {p['tier']} | {p['main']}/{p['sub']}", inline=False)
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def 초기화(ctx):
    bot.waiting_list.clear()
    await ctx.send("🧹 대기 명단이 초기화되었습니다.")

@bot.command()
async def 도움말(ctx):
    embed = discord.Embed(title="⚡ VOLT 내전 봇 사용법", color=0xFFFFFF)
    embed.add_field(name="!명단", value="현재 신청한 인원 확인", inline=True)
    embed.add_field(name="🛠️ 운영진용", value="!내전생성 / !초기화 / !팀뽑", inline=False)
    await ctx.send(embed=embed)

# --- [6] 실행 ---
if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
