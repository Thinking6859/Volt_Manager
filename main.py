import discord
from discord.ext import commands
from discord.ui import Button, View, Select
import os
import psycopg2
import random

# 1. 환경 변수 로드 (Koyeb 설정값)
TOKEN = os.getenv('DISCORD_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')

# 2. DB 연결 및 초기화 함수
def get_db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    conn = get_db_conn()
    cur = conn.cursor()
    # 전적 저장용 테이블
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

# 봇 설정
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# 현재 신청자 명단 (휘발성 - 팀 구성용)
waiting_list = {}

# --- [UI] 신청 버튼 및 선택 메뉴 ---
class RegisterView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.user_data = {}

    @discord.ui.select(placeholder="티어를 선택하세요", options=[
        discord.SelectOption(label="아이언~브론즈", value="I-B"),
        discord.SelectOption(label="실버~골드", value="S-G"),
        discord.SelectOption(label="플래티넘~에메랄드", value="P-E"),
        discord.SelectOption(label="다이아 이상", value="D+")
    ])
    async def select_tier(self, interaction: discord.Interaction, select: Select):
        self.user_data[interaction.user.id] = {'tier': select.values[0]}
        await interaction.response.send_message(f"티어 [{select.values[0]}] 선택! 이제 라인을 골라주세요.", ephemeral=True)

    @discord.ui.select(placeholder="주 라인을 선택하세요", options=[
        discord.SelectOption(label="탑", emoji="⚔️"),
        discord.SelectOption(label="정글", emoji="🌲"),
        discord.SelectOption(label="미드", emoji="🧙"),
        discord.SelectOption(label="원딜", emoji="🏹"),
        discord.SelectOption(label="서폿", emoji="🛡️")
    ])
    async def select_lane(self, interaction: discord.Interaction, select: Select):
        uid = interaction.user.id
        if uid not in self.user_data:
            return await interaction.response.send_message("티어를 먼저 선택해주세요!", ephemeral=True)
        
        tier = self.user_data[uid]['tier']
        lane = select.values[0]
        
        # 명단에 추가
        waiting_list[uid] = {
            "name": interaction.user.display_name,
            "tier": tier,
            "lane": lane
        }
        await interaction.response.send_message(f"✅ 신청 완료! ({tier} / {lane})", ephemeral=True)

# --- [명령어] 일반 유저 ---
@bot.event
async def on_ready():
    init_db()
    print(f'Logged in as {bot.user.name}')

@bot.command()
async def 도움말(ctx):
    embed = discord.Embed(title="⚡ VOLT 내전 관리 시스템", color=0x00ff00)
    embed.add_field(name="!신청", value="버튼으로 티어/라인을 선택해 신청합니다.", inline=False)
    embed.add_field(name="!명단", value="현재 대기 중인 인원을 확인합니다.", inline=False)
    embed.add_field(name="!전적", value="본인의 참여 횟수와 승률을 확인합니다.", inline=False)
    embed.add_field(name="🛠️ 관리자 전용", value="!팀뽑 / !결과기록 / !초기화", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def 신청(ctx):
    await ctx.send("내전 참가를 위해 정보를 입력해주세요!", view=RegisterView())

@bot.command()
async def 명단(ctx):
    if not waiting_list:
        return await ctx.send("현재 신청자가 없습니다.")
    
    text = "\n".join([f"• {p['name']} ({p['tier']} / {p['lane']})" for p in waiting_list.values()])
    await ctx.send(f"📋 **현재 신청 명단 ({len(waiting_list)}명)**\n{text}")

@bot.command()
async def 전적(ctx):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT games, wins FROM lol_stats WHERE user_id = %s", (str(ctx.author.id),))
    res = cur.fetchone()
    cur.close()
    conn.close()

    if not res:
        return await ctx.send("아직 참여 기록이 없습니다.")
    
    games, wins = res
    wr = (wins / games * 100) if games > 0 else 0
    await ctx.send(f"📊 **{ctx.author.display_name}**님: {games}전 {wins}승 (승률 {wr:.1f}%)")

# --- [명령어] 관리자 전용 ---
@bot.command()
@commands.has_permissions(administrator=True)
async def 팀뽑(ctx):
    if len(waiting_list) < 10:
        return await ctx.send(f"인원이 부족합니다. (현재 {len(waiting_list)}/10)")

    uids = list(waiting_list.keys())
    random.shuffle(uids)
    
    # 주장 2명 선정
    cap1_id, cap2_id = uids[0], uids[1]
    players = uids[2:]
    
    embed = discord.Embed(title="🎲 팀 드래프트 시작", color=0xffaa00)
    embed.add_field(name="1팀 주장", value=waiting_list[cap1_id]['name'])
    embed.add_field(name="2팀 주장", value=waiting_list[cap2_id]['name'])
    embed.add_field(name="남은 인원", value=", ".join([waiting_list[uid]['name'] for uid in players]), inline=False)
    embed.add_field(name="📌 드래프트 순서", value="1팀(1) -> 2팀(2) -> 1팀(2) -> 2팀(2) -> 1팀(1)")
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def 결과기록(ctx, win_team: int, *winner_names):
    """사용법: !결과기록 1 이름1 이름2 이름3 이름4 이름5"""
    conn = get_db_conn()
    cur = conn.cursor()
    
    # 현재 신청 명단에 있는 모든 사람의 판수 +1 (참여 횟수 기록)
    for uid, data in waiting_list.items():
        cur.execute('''
            INSERT INTO lol_stats (user_id, display_name, games, wins)
            VALUES (%s, %s, 1, 0)
            ON CONFLICT (user_id) DO UPDATE 
            SET games = lol_stats.games + 1, display_name = EXCLUDED.display_name
        ''', (str(uid), data['name']))

    # 승리한 사람만 승수 +1 (여기서는 수동으로 이름을 언급하거나, 로직을 추가할 수 있습니다)
    # 간단하게 현재 채널에 언급된 승리자들의 승률을 올리는 로직으로 확장 가능합니다.
    
    conn.commit()
    cur.close()
    conn.close()
    await ctx.send("✅ 모든 참가자의 판수가 기록되었습니다! (승리 기록은 수동 DB 관리를 권장합니다)")

@bot.command()
@commands.has_permissions(administrator=True)
async def 초기화(ctx):
    waiting_list.clear()
    await ctx.send("🧹 신청 명단이 초기화되었습니다.")

bot.run(TOKEN)