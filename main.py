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
def home(): return "⚡ VOLT Multi-Match System is Online!"
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

# --- [2] 멀티 내전 관리 클래스 ---
class MatchManager:
    def __init__(self):
        self.matches = {} # {match_id: {title, created_at, waiting_list}}
        self.match_count = 0

    def create_match(self, title):
        self.match_count += 1
        self.matches[self.match_count] = {
            "title": title,
            "created_at": datetime.now(KST),
            "waiting_list": {}
        }
        return self.match_count

manager = MatchManager()

# --- [3] 신청 프로세스 UI (방 선택 -> 티어 -> 라인) ---
class MatchSelectView(View):
    """현재 열려있는 내전 목록 중 선택"""
    def __init__(self):
        super().__init__(timeout=60)
        options = [
            discord.SelectOption(label=f"[{id}] {m['title']}", value=str(id), description=f"현재 {len(m['waiting_list'])}명 신청 중")
            for id, m in manager.matches.items()
        ]
        if not options:
            self.add_item(Button(label="현재 열린 내전이 없습니다.", disabled=True))
        else:
            select = Select(placeholder="어떤 내전에 신청하시겠습니까?", options=options)
            select.callback = self.match_selected
            self.add_item(select)

    async def match_selected(self, interaction: discord.Interaction):
        match_id = int(interaction.data['values'][0])
        await interaction.response.send_message(f"✅ {match_id}번 내전을 선택했습니다.", view=TierSelectView(match_id), ephemeral=True)

class TierSelectView(View):
    """티어 선택"""
    def __init__(self, match_id):
        super().__init__(timeout=60)
        self.match_id = match_id

    @discord.ui.select(placeholder="티어를 선택하세요", options=[discord.SelectOption(label=t, value=t) for t in TIER_DATA.keys()])
    async def tier_callback(self, interaction: discord.Interaction, select: Select):
        tier = select.values[0]
        await interaction.response.send_message(f"티어 [{tier}] 확인! **주 라인**을 골라주세요.", view=PositionSelectView(self.match_id, tier), ephemeral=True)

class PositionSelectView(View):
    """포지션 선택"""
    def __init__(self, match_id, tier):
        super().__init__(timeout=60)
        self.match_id = match_id
        self.tier = tier
        self.main_pos = None

    @discord.ui.select(placeholder="주 라인을 선택하세요", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿"]])
    async def main_callback(self, interaction: discord.Interaction, select: Select):
        self.main_pos = select.values[0]
        await interaction.response.edit_message(content=f"주 라인 [{self.main_pos}] 확인! **부 라인**을 골라주세요.", view=self)
        # 여기서 부라인 선택기로 넘기지 않고 간단하게 버튼으로 처리 (코드 길이 최적화)
        self.clear_items()
        sub_select = Select(placeholder="부 라인을 선택하세요", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿","상관없음"]])
        sub_select.callback = self.final_callback
        self.add_item(sub_select)

    async def final_callback(self, interaction: discord.Interaction):
        sub_pos = interaction.data['values'][0]
        match = manager.matches.get(self.match_id)
        if match:
            match['waiting_list'][interaction.user.id] = {
                "name": interaction.user.display_name, "mention": interaction.user.mention,
                "tier": self.tier, "main": self.main_pos, "sub": sub_pos,
                "time": datetime.now()
            }
            await interaction.response.send_message(f"🎉 **{match['title']}** 신청 완료! 현재 {len(match['waiting_list'])}명.", ephemeral=True)

# --- [4] 실시간 드래프트 View (V5.0 로직 유지) ---
class DraftView(View):
    def __init__(self, match_id, cap1, cap2, players):
        super().__init__(timeout=600)
        self.match_id = match_id
        self.captains = [cap1, cap2]; self.turn = 0
        self.players = players; self.teams = [[], []]
        self.update_buttons()

    def make_embed(self):
        embed = discord.Embed(title=f"⚔️ {manager.matches[self.match_id]['title']} 드래프트", color=0x9b59b6)
        t1 = "\n".join([f"• {p['name']} ({p['main']})" for p in self.teams[0]]) or "지명 중..."
        t2 = "\n".join([f"• {p['name']} ({p['main']})" for p in self.teams[1]]) or "지명 중..."
        embed.add_field(name=f"🔵 1팀 ({self.captains[0].display_name})", value=t1, inline=True)
        embed.add_field(name=f"🔴 2팀 ({self.captains[1].display_name})", value=t2, inline=True)
        return embed

    def update_buttons(self):
        self.clear_items()
        for i, p in enumerate(self.players):
            if any(p in t for t in self.teams): continue
            btn = Button(label=f"{p['name']} ({p['main']})", style=discord.ButtonStyle.secondary, custom_id=str(i))
            btn.callback = self.pick_callback
            self.add_item(btn)

    async def pick_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.captains[self.turn].id: return
        p_idx = int(interaction.data['custom_id'])
        self.teams[self.turn].append(self.players[p_idx])
        self.turn = 1 - self.turn
        if (len(self.teams[0]) + len(self.teams[1])) >= 10:
            bot.last_teams = {"team1": self.teams[0], "team2": self.teams[1]}
            await interaction.response.edit_message(content="✅ 팀 구성 완료!", embed=self.make_embed(), view=None)
        else:
            self.update_buttons(); await interaction.response.edit_message(embed=self.make_embed(), view=self)

# --- [5] 봇 메인 ---
class VoltBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default(); intents.message_content = True; intents.members = True
        super().__init__(command_prefix='!', intents=intents, help_command=None)
        self.last_teams = None

    async def setup_hook(self):
        self.scheduler = AsyncIOScheduler(timezone=KST)
        self.scheduler.add_job(self.auto_match_open, CronTrigger(day_of_week='tue,thu,sat', hour=11, minute=0))
        self.scheduler.start()

    async def auto_match_open(self):
        channel = discord.utils.get(self.get_all_channels(), name="내전-신청")
        if channel:
            mid = manager.create_match(f"정기 내전 ({datetime.now(KST).strftime('%m/%d')})")
            await channel.send(f"📢 **{mid}번 정기 내전**이 생성되었습니다! `!신청`으로 참여하세요.")

bot = VoltBot()

# --- [6] 명령어 ---
@bot.command()
async def 신청(ctx):
    """현재 열려있는 모든 내전 목록을 보여주고 신청 시작"""
    if not manager.matches: return await ctx.send("현재 열려있는 내전이 없습니다.")
    await ctx.send("원하시는 내전을 선택해 주세요!", view=MatchSelectView())

@bot.command()
@commands.has_permissions(administrator=True)
async def 내전생성(ctx, *, title: str):
    """수동으로 새로운 내전 방 생성 (예: !내전생성 2차 내전 가실분)"""
    mid = manager.create_match(title)
    await ctx.send(f"🔥 **{mid}번 내전: {title}** 방이 생성되었습니다! `!신청` 명령어를 사용하세요.")

@bot.command()
async def 명단(ctx, match_id: int):
    """특정 내전의 신청자 확인 (예: !명단 1)"""
    match = manager.matches.get(match_id)
    if not match: return await ctx.send("해당 내전이 존재하지 않습니다.")
    msg = "\n".join([f"- {p['name']} [{p['tier']}]" for p in match['waiting_list'].values()])
    await ctx.send(f"📋 **{match_id}번 내전 명단 ({len(match['waiting_list'])}명)**\n{msg}")

@bot.command()
@commands.has_permissions(administrator=True)
async def 드래프트(ctx, match_id: int, cap1: discord.Member, cap2: discord.Member):
    """특정 내전 방의 인원으로 드래프트 시작 (예: !드래프트 1 @주장1 @주장2)"""
    match = manager.matches.get(match_id)
    if len(match['waiting_list']) < 10: return await ctx.send("인원이 부족합니다.")
    players = sorted(match['waiting_list'].values(), key=lambda x: x['time'])[:10]
    await ctx.send(f"🗳️ {match_id}번 내전 드래프트 시작!", view=DraftView(match_id, cap1, cap2, players))

@bot.command()
async def 도움말(ctx):
    embed = discord.Embed(title="⚡ VOLT 멀티 내전 가이드", color=0x00FF00)
    embed.add_field(name="👤 유저", value="`!신청` - 내전 목록 확인 및 신청\n`!명단 [번호]` - 특정 내전 명단 확인", inline=False)
    embed.add_field(name="🛠️ 운영진", value="`!내전생성 [제목]` - 새로운 내전 방 열기\n`!드래프트 [번호] @주장1 @주장2` - 팀 지명 시작", inline=False)
    await ctx.send(embed=embed)

if __name__ == "__main__":
    keep_alive(); bot.run(TOKEN)
