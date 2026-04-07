import discord
from discord.ext import commands
from discord.ui import View, Select, Button
import os, psycopg2, uuid, pytz
from flask import Flask
from threading import Thread
from datetime import datetime

# --- [1] 서버 및 DB 초기 설정 ---
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
    try:
        # postgres:// 를 postgresql:// 로 자동 변환 (Render DB 이슈 방지)
        url = DATABASE_URL
        if url and url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(url, connect_timeout=5)
    except:
        return None

def init_db():
    conn = get_db_conn()
    if conn:
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS volt_rank (
            user_id TEXT PRIMARY KEY, 
            name TEXT, 
            wins INTEGER DEFAULT 0, 
            losses INTEGER DEFAULT 0, 
            points INTEGER DEFAULT 0,
            activity_points INTEGER DEFAULT 0
        )''')
        conn.commit(); cur.close(); conn.close()

# --- [2] 데이터 관리 클래스 ---
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

# --- [3] UI: 랭킹 조회 기준 선택 (!내랭킹, !전체랭킹용) ---
class RankSelectView(View):
    def __init__(self, is_all=False):
        super().__init__(timeout=60)
        self.is_all = is_all

    async def process_rank(self, interaction, sort_col, label):
        conn = get_db_conn()
        if not conn: return await interaction.response.send_message("DB 연결 실패!", ephemeral=True)
        cur = conn.cursor()
        order_by = sort_col if sort_col != "total" else "(points + activity_points)"
        cur.execute(f"SELECT user_id, name, points, activity_points, wins, losses FROM volt_rank ORDER BY {order_by} DESC")
        rows = cur.fetchall()
        
        if not rows:
            cur.close(); conn.close()
            return await interaction.response.edit_message(content="❌ 아직 기록된 데이터가 없습니다.", view=None)

        if self.is_all:
            rank_list = []
            for i, r in enumerate(rows, 1):
                val = r[2] if sort_col == "points" else r[3] if sort_col == "activity_points" else (r[2]+r[3])
                rank_list.append(f"**{i}위 {r[1]}** | {val}pt")
            embed = discord.Embed(title=f"🏆 VOLT 전체 랭킹 ({label} 기준)", description="\n".join(rank_list[:25]), color=0xFFD700)
        else:
            user_data = next(((i, r) for i, r in enumerate(rows, 1) if r[0] == str(interaction.user.id)), None)
            if not user_data:
                cur.close(); conn.close()
                return await interaction.response.edit_message(content="❓ 해당 부문의 기록이 없습니다.", view=None)
            i, r = user_data
            total_pts = r[2] + r[3]
            wr = (r[4]/(r[4]+r[5])*100) if (r[4]+r[5]) > 0 else 0
            embed = discord.Embed(title=f"👤 {r[1]}님의 랭킹 정보", color=0x3498db)
            embed.add_field(name=f"{label} 순위", value=f"**{i}위**", inline=True)
            embed.add_field(name="종합 점수", value=f"**{total_pts}pt**", inline=True)
            embed.add_field(name="상세 점수", value=f"승리: {r[2]}pt / 참여: {r[3]}pt", inline=False)
            embed.add_field(name="전적", value=f"{r[4]}승 {r[5]}패 (승률 {wr:.1f}%)", inline=False)

        cur.close(); conn.close()
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    @discord.ui.button(label="승리 점수", style=discord.ButtonStyle.success, emoji="🏆")
    async def victory_rank(self, interaction, button): await self.process_rank(interaction, "points", "승리 점수")
    @discord.ui.button(label="참여 점수", style=discord.ButtonStyle.primary, emoji="📅")
    async def activity_rank(self, interaction, button): await self.process_rank(interaction, "activity_points", "참여 점수")
    @discord.ui.button(label="종합 점수", style=discord.ButtonStyle.secondary, emoji="📊")
    async def total_rank(self, interaction, button): await self.process_rank(interaction, "total", "종합 점수")

# --- [4] UI: 명단 관리 및 드래프트 ---
class EditListView(View):
    def __init__(self, mid):
        super().__init__(timeout=300); self.mid = mid
        self.update_buttons()
    def update_buttons(self):
        self.clear_items()
        m = manager.matches.get(self.mid)
        if not m: return
        for eid, p in m['waiting_list'].items():
            btn = Button(label=f"❌ {p['name']}", style=discord.ButtonStyle.danger, custom_id=eid)
            btn.callback = self.delete_player; self.add_item(btn)
    async def delete_player(self, interaction):
        eid = interaction.data['custom_id']
        m = manager.matches.get(self.mid)
        if m and eid in m['waiting_list']:
            p_name = m['waiting_list'].pop(eid)['name']
            self.update_buttons(); await interaction.response.edit_message(content=f"✅ {p_name} 제외됨.", view=self)

class PostGameView(View):
    def __init__(self, mid):
        super().__init__(timeout=None); self.mid = mid
    @discord.ui.button(label="🔄 명단 수정", style=discord.ButtonStyle.primary, emoji="👥")
    async def edit_list_btn(self, interaction, button):
        await interaction.response.send_message("제외할 인원을 클릭하세요.", view=EditListView(self.mid), ephemeral=True)
    @discord.ui.button(label="❌ 내전 종료", style=discord.ButtonStyle.danger, emoji="🏁")
    async def close_match(self, interaction, button):
        manager.matches.pop(self.mid, None)
        await interaction.response.edit_message(content="🏁 내전 종료.", view=None)

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
        t1 = [f"🟦 **{self.captains[0].display_name}**"] + [f"• {p['name']}" for p in self.teams[0]]
        t2 = [f"🟥 **{self.captains[1].display_name}**"] + [f"• {p['name']}" for p in self.teams[1]]
        embed.add_field(name="1팀", value="\n".join(t1), inline=True)
        embed.add_field(name="2팀", value="\n".join(t2), inline=True)
        if self.step < len(self.pick_seq): embed.set_footer(text=f"[{self.captains[self.pick_seq[self.step]].display_name}] 차례")
        return embed
    def update_buttons(self):
        self.clear_items()
        for i, p in enumerate(self.players):
            if any(p is t for t in self.teams[0] + self.teams[1]): continue
            btn = Button(label=f"[{p['tier']}] {p['name']}", style=discord.ButtonStyle.secondary, custom_id=str(i))
            btn.callback = self.pick_callback; self.add_item(btn)
    async def pick_callback(self, interaction):
        if interaction.user.id != self.captains[self.pick_seq[self.step]].id: return await interaction.response.send_message("본인 차례가 아닙니다!", ephemeral=True)
        self.teams[self.pick_seq[self.step]].append(self.players[int(interaction.data['custom_id'])])
        self.step += 1
        if self.step >= len(self.pick_seq):
            f1 = [{"name":self.captains[0].display_name,"user_id":self.captains[0].id}] + self.teams[0]
            f2 = [{"name":self.captains[1].display_name,"user_id":self.captains[1].id}] + self.teams[1]
            manager.last_teams[self.mid] = {"team1": f1, "team2": f2}
            await interaction.response.edit_message(content="✅ 팀 구성 완료!", embed=self.make_embed(), view=None)
        else: self.update_buttons(); await interaction.response.edit_message(embed=self.make_embed(), view=self)

# --- [5] UI: 신청 프로세스 ---
class PosView(View):
    def __init__(self, mid, tier):
        super().__init__(timeout=120); self.mid, self.tier = mid, tier
    @discord.ui.select(placeholder="주 라인", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿"]])
    async def main_callback(self, interaction, select):
        self.main = select.values[0]; self.clear_items()
        sub = Select(placeholder="부 라인", options=[discord.SelectOption(label=l, value=l) for l in ["탑","정글","미드","원딜","서폿","상관없음"]])
        sub.callback = self.final_callback; self.add_item(sub)
        await interaction.response.edit_message(content=f"부 라인 선택", view=self)
    async def final_callback(self, interaction):
        m = manager.matches.get(self.mid)
        if m:
            eid = str(uuid.uuid4())
            m['waiting_list'][eid] = {"user_id": interaction.user.id, "name": interaction.user.display_name, "tier": self.tier, "main": self.main, "sub": interaction.data['values'][0]}
            await interaction.response.edit_message(content=f"🎉 {interaction.user.display_name}님 신청 완료!", view=None)

class MatchSelectView(View):
    def __init__(self):
        super().__init__(timeout=60)
        options = [discord.SelectOption(label=f"[{id}] {m['title']}", value=str(id)) for id, m in manager.matches.items()]
        if options:
            select = Select(placeholder="내전 선택", options=options)
            select.callback = self.match_selected; self.add_item(select)
    async def match_selected(self, interaction):
        await interaction.response.send_message(f"✅ {interaction.data['values'][0]}번 선택!", view=TierSelectView(int(interaction.data['values'][0])), ephemeral=True)

class TierSelectView(View):
    def __init__(self, mid):
        super().__init__(timeout=60); self.mid = mid
    @discord.ui.select(placeholder="티어 선택", options=[discord.SelectOption(label=t, value=t) for t in TIER_DATA.keys()])
    async def tier_callback(self, interaction, select):
        await interaction.response.send_message("라인 선택", view=PosView(self.mid, select.values[0]), ephemeral=True)

# --- [6] 봇 본체 ---
class VoltBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default(); intents.message_content = True; intents.members = True
        super().__init__(command_prefix='!', intents=intents, help_command=None)
    async def setup_hook(self): init_db()

bot = VoltBot()

@bot.command()
async def 신청(ctx):
    if not manager.matches: return await ctx.send("열린 내전이 없습니다.")
    await ctx.send("참여 신청 시작", view=MatchSelectView())

@bot.command()
async def 명단(ctx, mid: int):
    m = manager.matches.get(mid)
    if not m: return await ctx.send("방이 없습니다.")
    all_p = list(m['waiting_list'].values())
    embed = discord.Embed(title=f"📋 {m['title']} 명단 ({len(all_p)}/10)", color=0x3498db)
    if not all_p: embed.description = "참여자가 없습니다."
    else:
        p_list = "\n".join([f"• **{p['name']}** [{p['tier']}] ({p['main']}/{p['sub']})" for p in all_p])
        embed.add_field(name="참여자", value=p_list, inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def 내랭킹(ctx):
    await ctx.send("조회 기준 선택:", view=RankSelectView(is_all=False))

@bot.command()
async def 전체랭킹(ctx):
    await ctx.send("정렬 기준 선택:", view=RankSelectView(is_all=True))

@bot.command()
@commands.has_permissions(administrator=True)
async def 내전생성(ctx, *, title):
    mid = manager.create_match(title)
    await ctx.send(f"🔥 내전 생성! 방 번호: {mid} / 제목: {title}")

@bot.command()
@commands.has_permissions(administrator=True)
async def 드래프트(ctx, mid: int, cap1: discord.Member, cap2: discord.Member):
    m = manager.matches.get(mid)
    if not m: return await ctx.send("방 없음")
    all_p = list(m['waiting_list'].values())
    if len(all_p) < 10: return await ctx.send(f"인원 부족 ({len(all_p)}/10)")
    dp = [p for p in all_p if p['user_id'] not in [cap1.id, cap2.id]]
    await ctx.send(f"🗳️ {cap1.display_name} vs {cap2.display_name} 드래프트 시작!", view=DraftView(mid, cap1, cap2, dp))

@bot.command()
@commands.has_permissions(administrator=True)
async def 결과기록(ctx, mid: int, win_team: int):
    teams = manager.last_teams.get(mid)
    if not teams: return await ctx.send("데이터 없음")
    conn = get_db_conn(); cur = conn.cursor()
    # 승리팀
    for p in teams[f'team{win_team}']:
        cur.execute("INSERT INTO volt_rank (user_id, name, wins, points, activity_points) VALUES (%s,%s,1,10,10) ON CONFLICT (user_id) DO UPDATE SET wins=volt_rank.wins+1, points=volt_rank.points+10, activity_points=volt_rank.activity_points+10", (str(p['user_id']), p['name']))
    # 패배팀
    lose_idx = 2 if win_team == 1 else 1
    for p in teams[f'team{lose_idx}']:
        cur.execute("INSERT INTO volt_rank (user_id, name, losses, activity_points) VALUES (%s,%s,1,10) ON CONFLICT (user_id) DO UPDATE SET losses=volt_rank.losses+1, activity_points=volt_rank.activity_points+10", (str(p['user_id']), p['name']))
    conn.commit(); cur.close(); conn.close()
    await ctx.send("🏆 기록 완료!", view=PostGameView(mid))

@bot.command()
async def 도움말(ctx):
    await ctx.send("🎮 유저: `!신청`, `!명단`, `!내랭킹`, `!전체랭킹` / 🛠️ 운영진: `!내전생성`, `!드래프트`, `!결과기록`")

if __name__ == "__main__":
    init_db(); keep_alive(); bot.run(TOKEN)
