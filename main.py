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

# --- [1] 기본 설정 ---
app = Flask('')
@app.route('/')
def home(): return "⚡ VOLT V3 System is Active!"
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

# --- [2] UI 컴포넌트 (멀티 스텝 & 실시간 반영) ---
class VoltMatchView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.user_selections = {}

    async def update_status_embed(self, interaction):
        """현재 신청 인원 현황을 실시간으로 업데이트"""
        count = len(bot.waiting_list)
        lanes = {"탑": 0, "정글": 0, "미드": 0, "원딜": 0, "서폿": 0}
        for p in bot.waiting_list.values():
            lanes[p['main']] += 1
        
        status_text = " | ".join([f"{k}:{v}" for k, v in lanes.items()])
        embed = interaction.message.embeds[0]
        embed.set_footer(text=f"현재 신청: {count}명 ({status_text})")
        await interaction.message.edit(embed=embed)

    @discord.ui.select(placeholder="1. 본인 티어", options=[discord.SelectOption(label=t, value=t) for t in TIER_DATA.keys()])
    async def select_tier(self, interaction: discord.Interaction, select: Select):
        await interaction.response.defer(ephemeral=True)
        self.user_selections[interaction.user.id] = {'tier': select.values[0]}
        await interaction.followup.send("확인되었습니다. **주 라인**을 선택하세요.", ephemeral=True)

    @discord.ui.select(placeholder="2. 주 라인 (1순위)", options=[discord.SelectOption(label=l, emoji=e, value=l) for l, e in zip(["탑","정글","미드","원딜","서폿"], ["⚔️","🌲","🧙","🏹","🛡️"])])
    async def select_main(self, interaction: discord.Interaction, select: Select):
        await interaction.response.defer(ephemeral=True)
        self.user_selections[interaction.user.id]['main'] = select.values[0]
        await interaction.followup.send("마지막으로 **부 라인**을 선택하세요.", ephemeral=True)

    @discord.ui.select(placeholder="3. 부 라인 (2순위)", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿","상관없음"]])
    async def select_sub(self, interaction: discord.Interaction, select: Select):
        await interaction.response.defer(ephemeral=True)
        uid = interaction.user.id
        data = self.user_selections.get(uid)
        
        bot.waiting_list[uid] = {
            "name": interaction.user.display_name,
            "mention": interaction.user.mention,
            "tier": data['tier'],
            "main": data['main'],
            "sub": select.values[0],
            "score": TIER_DATA[data['tier']],
            "time": interaction.created_at
        }
        await interaction.followup.send(f"✅ **신청 완료!** ({len(bot.waiting_list)}번째)", ephemeral=True)
        await self.update_status_embed(interaction)

# --- [3] 봇 메인 로직 ---
class VoltBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix='!', intents=intents, help_command=None)
        self.waiting_list = {}
        self.last_teams = None

    async def setup_hook(self):
        self.scheduler = AsyncIOScheduler(timezone=KST)
        self.scheduler.add_job(self.auto_match_open, CronTrigger(day_of_week='tue,thu,sat', hour=11, minute=0))
        self.scheduler.start()

    async def auto_match_open(self):
        channel = discord.utils.get(self.get_all_channels(), name="내전-신청")
        if channel:
            self.waiting_list.clear()
            embed = discord.Embed(title="⚡ VOLT 정기 내전 모집", description="**화/목/토 11:00 정기 내전**\n아래 메뉴를 선택해 신청하세요!", color=0x00FF00)
            embed.set_image(url="https://i.imgur.com/your_volt_logo.png") # 로고가 있다면 주소 입력
            await channel.send(content="@everyone 내전 신청이 시작되었습니다!", embed=embed, view=VoltMatchView())

bot = VoltBot()

# --- [4] 고성능 명령어 세트 ---

@bot.command()
@commands.has_permissions(administrator=True)
async def 팀뽑(ctx):
    """포지션과 티어 점수를 모두 고려한 팀 빌딩"""
    if len(bot.waiting_list) < 10:
        return await ctx.send(f"⚠️ 인원이 부족합니다! 현재 {len(bot.waiting_list)}/10명")

    # 선착순 10명만 자르기
    sorted_players = sorted(bot.waiting_list.values(), key=lambda x: x['time'])[:10]
    random.shuffle(sorted_players)
    
    team1 = sorted_players[:5]
    team2 = sorted_players[5:10]
    
    # 밸런스 점수 계산
    s1 = sum(p['score'] for p in team1)
    s2 = sum(p['score'] for p in team2)
    
    bot.last_teams = {"1": team1, "2": team2}

    embed = discord.Embed(title="🎲 VOLT 내전 대진표", color=0xFFFFFF)
    embed.add_field(name=f"🔵 1팀 (총점:{s1})", value="\n".join([f"**{p['main']}** {p['name']} ({p['tier']})" for p in team1]), inline=True)
    embed.add_field(name=f"🔴 2팀 (총점:{s2})", value="\n".join([f"**{p['main']}** {p['name']} ({p['tier']})" for p in team2]), inline=True)
    
    # 지각 방지용 멘션 소환
    mentions = " ".join([p['mention'] for p in sorted_players])
    await ctx.send(content=f"📢 **선발 명단 소환!**\n{mentions}", embed=embed)

@bot.command()
async def 내정보(ctx):
    """개인별 상세 전적 확인"""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT wins, losses, points FROM volt_rank WHERE name = %s", (ctx.author.display_name,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row: return await ctx.send("기록된 데이터가 없습니다.")
    w, l, p = row
    wr = (w / (w+l) * 100) if (w+l) > 0 else 0
    
    embed = discord.Embed(title=f"📊 {ctx.author.display_name}님의 리포트", color=0x3498db)
    embed.add_field(name="승률", value=f"{wr:.1f}% ({w}승 {l}패)")
    embed.add_field(name="포인트", value=f"{p} LP")
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def 강제취소(ctx, target_name: str):
    """노쇼나 갑작스러운 사정으로 빠진 사람 제거"""
    found = None
    for uid, p in bot.waiting_list.items():
        if p['name'] == target_name:
            found = uid
            break
    
    if found:
        del bot.waiting_list[found]
        await ctx.send(f"🧹 {target_name}님을 명단에서 제외했습니다.")
    else:
        await ctx.send("해당 이름의 신청자를 찾을 수 없습니다.")

@bot.command()
@commands.has_permissions(administrator=True)
async def 공지(ctx, *, message: str):
    """신청자 전원에게 멘션 공지 (지각 체크용)"""
    if not bot.waiting_list: return await ctx.send("신청자가 없습니다.")
    mentions = " ".join([p['mention'] for p in bot.waiting_list.values()])
    await ctx.send(f"📢 **VOLT 운영진 공지**\n{message}\n\n{mentions}")

if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
