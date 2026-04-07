import discord
from discord.ext import commands
from discord.ui import View, Select, Button
import os, psycopg2, uuid, pytz
from flask import Flask
from threading import Thread
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime

# --- [1] 서버 및 DB 설정 ---
app = Flask('')
@app.route('/')
def home(): return "⚡ VOLT System is Online!"
def run(): app.run(host='0.0.0.0', port=8080)
def keep_alive(): Thread(target=run).start()

DATABASE_URL = os.getenv('DATABASE_URL')
TOKEN = os.getenv('DISCORD_TOKEN')
KST = pytz.timezone('Asia/Seoul')

TIER_DATA = {"아이언":1, "브론즈":2, "실버":3, "골드":4, "플래티넘":5, "에메랄드":6, "다이아몬드":8, "마스터":10, "그랜드마스터":12, "챌린저":15}

def get_db_conn():
    try: return psycopg2.connect(DATABASE_URL, connect_timeout=5) if DATABASE_URL else None
    except: return None

def init_db():
    conn = get_db_conn()
    if conn:
        cur = conn.cursor()
        cur.execute('CREATE TABLE IF NOT EXISTS volt_rank (user_id TEXT PRIMARY KEY, name TEXT, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0, points INTEGER DEFAULT 0)')
        conn.commit(); cur.close(); conn.close()

# --- [2] 데이터 관리자 ---
class MatchManager:
    def __init__(self):
        self.matches = {}
        self.match_count = 0
        self.last_teams = {}
    def create_match(self, title):
        self.match_count += 1
        self.matches[self.match_count] = {"title": title, "waiting_list": {}}
        return self.match_count

manager = MatchManager()

# --- [3] UI: 명단 개별 수정 (제외 기능) ---
class EditListView(View):
    def __init__(self, mid):
        super().__init__(timeout=300); self.mid = mid
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        m = manager.matches.get(self.mid)
        if not m: return
        for eid, p in m['waiting_list'].items():
            btn = Button(label=f"❌ {p['name']} [{p['tier']}]", style=discord.ButtonStyle.danger, custom_id=eid)
            btn.callback = self.delete_player
            self.add_item(btn)

    async def delete_player(self, interaction):
        eid = interaction.data['custom_id']
        m = manager.matches.get(self.mid)
        if m and eid in m['waiting_list']:
            p_name = m['waiting_list'].pop(eid)['name']
            self.update_buttons()
            await interaction.response.edit_message(content=f"✅ **{p_name}** 님이 명단에서 제외되었습니다.\n현재 인원: **{len(m['waiting_list'])}/10**", view=self)

# --- [4] UI: 결과 기록 후 선택지 ---
class PostGameView(View):
    def __init__(self, mid):
        super().__init__(timeout=None); self.mid = mid
    
    @discord.ui.button(label="🔄 명단 수정 (인원 교체)", style=discord.ButtonStyle.primary, emoji="👥")
    async def edit_list_btn(self, interaction, button):
        await interaction.response.send_message("제외할 인원을 클릭하세요. 그 외 인원은 유지됩니다.", view=EditListView(self.mid), ephemeral=True)

    @discord.ui.button(label="❌ 내전 종료", style=discord.ButtonStyle.danger, emoji="🏁")
    async def close_match(self, interaction, button):
        manager.matches.pop(self.mid, None)
        await interaction.response.edit_message(content="🏁 내전 방이 폐쇄되었습니다. 수고하셨습니다!", view=None)

# --- [5] UI: 드래프트 (1-2-2-2-1) ---
class DraftView(View):
    def __init__(self, mid, cap1, cap2, players):
        super().__init__(timeout=600)
        self.mid, self.captains = mid, [cap1, cap2]
        self.players, self.teams = players, [[], []]
        self.pick_seq, self.step = [0, 1, 1, 0, 0, 1, 1, 0], 0
        self.update_buttons()

    def make_embed(self):
        m_title = manager.matches[self.mid]['title']
        embed = discord.Embed(title=f"⚔️ {m_title} 드래프트", color=0x5865F2)
        t1 = [f"🟦 **{self.captains[0].display_name}** (주장)"] + [f"• {p['name']} ({p['main']})" for p in self.teams[0]]
        t2 = [f"🟥 **{self.captains[1].display_name}** (주장)"] + [f"• {p['name']} ({p['main']})" for p in self.teams[1]]
        embed.add_field(name="1팀 (Blue)", value="\n".join(t1), inline=True)
        embed.add_field(name="2팀 (Red)", value="\n".join(t2), inline=True)
        if self.step < len(self.pick_seq):
            embed.set_footer(text=f"지명 순서: [{self.captains[self.pick_seq[self.step]].display_name}] 차례")
        return embed

    def update_buttons(self):
        self.clear_items()
        for i, p in enumerate(self.players):
            if any(p is t for t in self.teams[0] + self.teams[1]): continue
            label = f"[{p['tier']}] {p['name']} ({p['main']}/{p['sub']})"
            btn = Button(label=label, style=discord.ButtonStyle.secondary, custom_id=str(i))
            btn.callback = self.pick_callback; self.add_item(btn)

    async def pick_callback(self, interaction):
        if interaction.user.id != self.captains[self.pick_seq[self.step]].id:
            return await interaction.response.send_message("본인 차례가 아닙니다!", ephemeral=True)
        self.teams[self.pick_seq[self.step]].append(self.players[int(interaction.data['custom_id'])])
        self.step += 1
        if self.step >= len(self.pick_seq):
            f1 = [{"name":self.captains[0].display_name,"user_id":self.captains[0].id}] + self.teams[0]
            f2 = [{"name":self.captains[1].display_name,"user_id":self.captains[1].id}] + self.teams[1]
            manager.last_teams[self.mid] = {"team1": f1, "team2": f2}
            await interaction.response.edit_message(content="✅ **팀 구성 완료!**", embed=self.make_embed(), view=None)
        else:
            self.update_buttons(); await interaction.response.edit_message(embed=self.make_embed(), view=self)

# --- [6] UI: 신청 프로세스 ---
class PosView(View):
    def __init__(self, mid, tier):
        super().__init__(timeout=120); self.mid, self.tier = mid, tier
    @discord.ui.select(placeholder="주 라인 선택", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿"]])
    async def main_callback(self, interaction, select):
        self.main = select.values[0]; self.clear_items()
        sub = Select(placeholder="부 라인 선택", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿","상관없음"]])
        sub.callback = self.final_callback; self.add_item(sub)
        await interaction.response.edit_message(content=f"부 라인을 선택하세요. (주라인: {self.main})", view=self)
    async def final_callback(self, interaction):
        m = manager.matches.get(self.mid)
        if m:
            eid = str(uuid.uuid4())
            m['waiting_list'][eid] = {"user_id": interaction.user.id, "name": interaction.user.display_name, "tier": self.tier, "main": self.main, "sub": interaction.data['values'][0], "time": datetime.now()}
            await interaction.response.edit_message(content=f"🎉 {m['title']} 신청 완료!", view=None)

class TierSelectView(View):
    def __init__(self, mid):
        super().__init__(timeout=60); self.mid = mid
    @discord.ui.select(placeholder="티어 선택", options=[discord.SelectOption(label=t, value=t) for t in TIER_DATA.keys()])
    async def tier_callback(self, interaction, select):
        await interaction.response.send_message("주 라인 선택", view=PosView(self.mid, select.values[0]), ephemeral=True)

class MatchSelectView(View):
    def __init__(self):
        super().__init__(timeout=60)
        options = [discord.SelectOption(label=f"[{id}] {m['title']}", value=str(id)) for id, m in manager.matches.items()]
        if options:
            select = Select(placeholder="내전 선택", options=options)
            select.callback = self.match_selected; self.add_item(select)
    async def match_selected(self, interaction):
        await interaction.response.send_message(f"✅ {interaction.data['values'][0]}번 선택!", view=TierSelectView(int(interaction.data['values'][0])), ephemeral=True)

# --- [7] 봇 본체 및 강화된 명령어 ---
class VoltBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default(); intents.message_content = True; intents.members = True
        super().__init__(command_prefix='!', intents=intents, help_command=None)
    async def setup_hook(self): init_db(); self.scheduler = AsyncIOScheduler(timezone=KST); self.scheduler.start()

bot = VoltBot()

@bot.command()
async def 신청(ctx):
    if not manager.matches: return await ctx.send("현재 열려있는 내전이 없습니다.")
    await ctx.send("참여할 내전 번호를 선택하세요.", view=MatchSelectView())

@bot.command()
@commands.has_permissions(administrator=True)
async def 내전생성(ctx, *, title):
    mid = manager.create_match(title)
    await ctx.send(embed=discord.Embed(title="🔥 내전 생성 완료", description=f"**방 번호: {mid}**\n**제목: {title}**\n`!신청`으로 참여하세요!", color=0x00ff00))

@bot.command()
@commands.has_permissions(administrator=True)
async def 드래프트(ctx, mid: int, cap1: discord.Member, cap2: discord.Member):
    m = manager.matches.get(mid)
    if not m: return await ctx.send("방이 없습니다.")
    all_p = list(m['waiting_list'].values())
    if len(all_p) < 10: return await ctx.send(f"인원 부족 ({len(all_p)}/10)")
    dp = [p for p in all_p if p['user_id'] not in [cap1.id, cap2.id]]
    await ctx.send(f"🗳️ {cap1.display_name} VS {cap2.display_name} 드래프트!", view=DraftView(mid, cap1, cap2, dp))

@bot.command()
@commands.has_permissions(administrator=True)
async def 결과기록(ctx, mid: int, win_team: int):
    teams = manager.last_teams.get(mid)
    if not teams: return await ctx.send("기록할 데이터가 없습니다.")
    conn = get_db_conn(); cur = conn.cursor()
    for p in teams[f'team{win_team}']: cur.execute("INSERT INTO volt_rank (user_id, name, wins, points) VALUES (%s,%s,1,10) ON CONFLICT (user_id) DO UPDATE SET wins=volt_rank.wins+1, points=volt_rank.points+10", (str(p['user_id']), p['name']))
    for p in teams[f'team{3-win_team}']: cur.execute("INSERT INTO volt_rank (user_id, name, losses, points) VALUES (%s,%s,0,5) ON CONFLICT (user_id) DO UPDATE SET losses=volt_rank.losses+1, points=volt_rank.points+5", (str(p['user_id']), p['name']))
    conn.commit(); cur.close(); conn.close()
    await ctx.send(embed=discord.Embed(title="🏆 기록 완료", description=f"{win_team}팀 승리 반영됨. 다음 단계를 선택하세요.", color=0xFFD700), view=PostGameView(mid))

@bot.command()
async def 내랭킹(ctx):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id, name, points, wins, losses FROM volt_rank ORDER BY points DESC")
    rows = cur.fetchall()
    for i, row in enumerate(rows, 1):
        if row[0] == str(ctx.author.id):
            total = row[3] + row[4]
            wr = (row[3]/total*100) if total > 0 else 0
            embed = discord.Embed(title=f"👤 {row[1]}님의 전적", color=0x3498db)
            embed.add_field(name="순위", value=f"{i}위", inline=True)
            embed.add_field(name="포인트", value=f"{row[2]}pt", inline=True)
            embed.add_field(name="승패 (승률)", value=f"{row[3]}승 {row[4]}패 ({wr:.1f}%)", inline=False)
            cur.close(); conn.close(); return await ctx.send(embed=embed)
    await ctx.send("기록이 없습니다."); cur.close(); conn.close()

@bot.command()
async def 전체랭킹(ctx):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT name, points, wins, losses FROM volt_rank ORDER BY points DESC")
    rows = cur.fetchall()
    if not rows: return await ctx.send("데이터가 없습니다.")
    rank_text = "\n".join([f"**{i}. {r[0]}** | {r[1]}pt ({r[2]}승 {r[3]}패)" for i, r in enumerate(rows, 1)])
    await ctx.send(embed=discord.Embed(title="🏆 VOLT 전체 랭킹 현황", description=rank_text[:2000], color=0xFFD700))
    cur.close(); conn.close()

@bot.command()
async def 도움말(ctx):
    embed = discord.Embed(title="⚡ VOLT 가이드", color=0x2ecc71)
    embed.add_field(name="👤 유저", value="`!신청`, `!내랭킹`, `!전체랭킹`", inline=False)
    embed.add_field(name="🛠️ 운영진", value="`!내전생성`, `!드래프트 [번] @주장1 @주장2`, `!결과기록 [번] [1or2]`", inline=False)
    await ctx.send(embed=embed)

if __name__ == "__main__":
    init_db(); keep_alive(); bot.run(TOKEN)
