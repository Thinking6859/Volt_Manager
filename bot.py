import os
import discord
from discord.ext import commands
from discord import ui
from supabase import create_client, Client
from keep_alive import keep_alive 

# --- [1. 설정 및 환경변수] ---
try: from dotenv import load_dotenv; load_dotenv()
except: pass

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BOT_TOKEN = os.getenv("DISCORD_TOKEN")

# 📢 실제 서버 채널 ID로 수정 필수!
RECRUIT_CHANNEL_ID = 1493210332598894684 #모집
REGISTER_CHANNEL_ID = 1493205766209933404 #소환사
RANKING_CHANNEL_ID = 1493205910892318850 #랭킹

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

TIER_SCORE = {"아이언": 1, "브론즈": 2, "실버": 3, "골드": 4, "플래티넘": 5, "에메랄드": 6, "다이아몬드": 7, "마스터+": 9}
active_recruitment = {"target": 10, "participants": [], "message": None}
current_match = {"ids": [], "team1": [], "team2": [], "names1": [], "names2": []}

# --- [가짜 유저 클래스: 테스트 지원용] ---
class MockUser:
    def __init__(self, id, name):
        self.id = id; self.display_name = name; self.mention = f"<@{id}>"; self.bot = False

# --- [유틸리티] ---
async def is_admin(interaction: discord.Interaction):
    if interaction.user.guild_permissions.administrator: return True
    res = supabase.table("users").select("is_admin").eq("discord_id", interaction.user.id).execute()
    if res.data and res.data[0].get("is_admin"): return True
    await interaction.response.send_message("🚫 운영진 권한이 없습니다.", ephemeral=True); return False

async def update_recruitment_msg():
    if active_recruitment["message"]:
        names = "\n".join([f"· {getattr(m, 'display_name', str(m.id))}" for m in active_recruitment["participants"]])
        content = f"🎮 **VOLT {active_recruitment['target']}인 내전 모집 중**\n\n**신청자 ({len(active_recruitment['participants'])}/10):**\n{names if names else '현재 신청자가 없습니다.'}"
        try: await active_recruitment["message"].edit(content=content)
        except: pass

# --- [Views: 드래프트 (5명씩 2줄 정렬)] ---
class DraftView(ui.View):
    def __init__(self, p, l1, l2, ids):
        super().__init__(timeout=None)
        self.p, self.l, self.ids = p, {1: l1, 2: l2}, ids
        self.teams, self.t_ids, self.t_scores = {1: [], 2: []}, {1: [l1.id], 2: [l2.id]}, {1: 0, 2: 0}
        self.order, self.idx = [1, 2, 2, 1, 1, 2, 2, 1], 0
        for idx, ldr in enumerate([l1, l2], 1):
            res = supabase.table("users").select("*").eq("discord_id", ldr.id).execute()
            tier = res.data[0]['tier'] if res.data else "실버"
            self.t_scores[idx] += TIER_SCORE.get(tier, 3)
            self.teams[idx].append(f"👑 **{ldr.display_name}** ({tier[0]})")
        self.create_buttons()

    def create_buttons(self):
        self.clear_items()
        for i, (d_id, d) in enumerate(self.p.items()):
            row_idx = 0 if i < 5 else 1
            btn = ui.Button(label=f"[{d['t_short']}] {d['n']}", custom_id=str(d_id), row=row_idx, style=discord.ButtonStyle.secondary)
            btn.callback = self.pick_callback; self.add_item(btn)

    async def pick_callback(self, i):
        curr_lds = self.l[self.order[self.idx]]
        if i.user.id != curr_lds.id: return await i.response.send_message(f"{curr_lds.display_name} 차례!", ephemeral=True)
        p_id = int(i.data['custom_id']); p_data = self.p.pop(p_id); t_num = self.order[self.idx]
        self.teams[t_num].append(f"· **{p_data['n']}** ({p_data['t_short']})")
        self.t_ids[t_num].append(p_id); self.t_scores[t_num] += p_data['score']; self.idx += 1
        if not self.p or self.idx >= len(self.order): await self.finish(i)
        else: self.create_buttons(); await i.response.edit_message(content=f"🔵 **{self.l[self.order[self.idx]].display_name}** 선택!", view=self)

    async def finish(self, i):
        global current_match
        current_match = {"ids": self.ids, "team1": self.t_ids[1], "team2": self.t_ids[2]}
        embed = discord.Embed(title="⚔️ 드래프트 완료", color=0x5865F2)
        embed.add_field(name="🟦 1팀", value="\n".join(self.teams[1]), inline=False)
        embed.add_field(name="🟥 2팀", value="\n".join(self.teams[2]), inline=False)
        await bot.get_channel(RECRUIT_CHANNEL_ID).send(embed=embed)
        await i.response.edit_message(content="✅ 드래프트 종료!", view=None)

# --- [Views: 다음 액션 패널] ---
class NextActionView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    @ui.button(label="♻️ 재드래프트", style=discord.ButtonStyle.primary)
    async def rd(self, i, b):
        p_list = active_recruitment["participants"]
        v = ui.View(); opts = [discord.SelectOption(label=m.display_name, value=str(m.id)) for m in p_list]
        sel = ui.Select(placeholder="새 주장 2명", min_values=2, max_values=2, options=opts)
        async def cb(i2):
            lds = [m for m in p_list if m.id in [int(v) for v in sel.values]]
            db = {r['discord_id']: r for r in supabase.table("users").select("*").in_("discord_id", [m.id for m in p_list]).execute().data}
            pool = {m.id: {"n": m.display_name, "t_short": db[m.id]['tier'][0], "score": TIER_SCORE.get(db[m.id]['tier'], 3)} for m in p_list if m.id not in [l.id for l in lds]}
            await i2.channel.send("⚔️ 다시 드래프트!", view=DraftView(pool, lds[0], lds[1], [m.id for m in p_list]))
            await i2.response.edit_message(content="✅ 생성됨", view=None)
        sel.callback = cb; v.add_item(sel); await i.response.edit_message(content="주장 선택:", view=v)
    
    @ui.button(label="🏁 오늘 내전 종료", style=discord.ButtonStyle.danger)
    async def end(self, i, b):
        active_recruitment["participants"] = []
        await i.response.edit_message(content="🏁 종료! 명단이 초기화되었습니다.", view=None)

# --- [Views: 명단 관리] ---
class ParticipantEditRootView(ui.View):
    def __init__(self): super().__init__(timeout=60)
    @ui.button(label="➕ 추가", style=discord.ButtonStyle.success)
    async def add(self, i, b):
        res = supabase.table("users").select("discord_id, discord_name").execute()
        v = ui.View(); opts = [discord.SelectOption(label=u['discord_name'], value=str(u['discord_id'])) for u in res.data[:25]]
        sel = ui.Select(placeholder="추가", options=opts)
        async def cb(i2):
            uid = int(sel.values[0]); name = next(u['discord_name'] for u in res.data if u['discord_id']==uid)
            active_recruitment["participants"].append(MockUser(uid, name)); await update_recruitment_msg()
            await i2.response.send_message("✅ 추가됨", ephemeral=True)
        sel.callback = cb; v.add_item(sel); await i.response.edit_message(content="유저 선택:", view=v)
    @ui.button(label="➖ 제외", style=discord.ButtonStyle.danger)
    async def rem(self, i, b):
        v = ui.View(); opts = [discord.SelectOption(label=m.display_name, value=str(m.id)) for m in active_recruitment["participants"]]
        sel = ui.Select(placeholder="제외", options=opts)
        async def cb(i2):
            active_recruitment["participants"] = [m for m in active_recruitment["participants"] if m.id != int(sel.values[0])]
            await update_recruitment_msg(); await i2.response.send_message("✅ 제외됨", ephemeral=True)
        sel.callback = cb; v.add_item(sel); await i.response.edit_message(content="제외 선택:", view=v)

# --- [Views: 소환사 등록] ---
class RegisterFlow(ui.View):
    def __init__(self, rid): super().__init__(timeout=180); self.rid = rid; self.t = None
    @ui.select(placeholder="티어 선택", options=[discord.SelectOption(label=k) for k in TIER_SCORE.keys()])
    async def s_t(self, i, s): self.t = s.values[0]; await i.response.edit_message(content=f"티어: {self.t}\n라인 선택", view=self)
    @ui.select(placeholder="라인 선택", options=[discord.SelectOption(label=l, value=l) for l in ["TOP", "JUG", "MID", "ADC", "SUP"]])
    async def s_m(self, i, s):
        supabase.table("users").upsert({"discord_id": i.user.id, "discord_name": i.user.display_name, "riot_id": self.rid, "tier": self.t, "main_line": s.values[0]}).execute()
        await i.response.edit_message(content="✅ 등록 완료!", view=None)

# --- [마스터 대시보드] ---
class MasterDashboardView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    
    @ui.button(label="📢 공지 배포", style=discord.ButtonStyle.primary, row=0)
    async def b_n(self, i, b):
        v = ui.View(); sel = ui.Select(placeholder="배포 선택", options=[
            discord.SelectOption(label="등록 센터", value="reg"),
            discord.SelectOption(label="모집 시작", value="rec"),
            discord.SelectOption(label="랭킹 보드", value="rank")
        ])
        async def cb(i2):
            val = sel.values[0]
            if val == "reg":
                bv = ui.View(timeout=None); btn = ui.Button(label="소환사 등록", style=discord.ButtonStyle.primary)
                async def rcb(i3):
                    m = ui.Modal(title="등록"); rid = ui.TextInput(label="ID", placeholder="닉네임#TAG"); m.add_item(rid)
                    async def os(i4): await i4.response.send_message("입력", view=RegisterFlow(rid.value), ephemeral=True)
                    m.on_submit = os; await i3.response.send_modal(m)
                btn.callback = rcb; bv.add_item(btn); await bot.get_channel(REGISTER_CHANNEL_ID).send("⚡ 등록 센터", view=bv)
            elif val == "rec":
                active_recruitment["participants"] = []; msg = await bot.get_channel(RECRUIT_CHANNEL_ID).send("🎮 모집!", view=JoinView())
                active_recruitment["message"] = msg; await update_recruitment_msg()
            elif val == "rank": await bot.get_channel(RANKING_CHANNEL_ID).send("📊 실시간 랭킹", view=RankingBoardView())
            await i2.response.edit_message(content="✅ 완료", view=None)
        sel.callback = cb; v.add_item(sel); await i.response.send_message("배포 선택:", view=v, ephemeral=True)

    @ui.button(label="⚔️ 드래프트 시작", style=discord.ButtonStyle.danger, row=0)
    async def b_d(self, i, b):
        p_list = active_recruitment["participants"]
        if len(p_list) < 2: return await i.response.send_message("인원 부족", ephemeral=True)
        v = ui.View(); opts = [discord.SelectOption(label=m.display_name, value=str(m.id)) for m in p_list]
        sel = ui.Select(placeholder="주장 2명 선택", min_values=2, max_values=2, options=opts)
        async def cb(i2):
            lds = [m for m in p_list if m.id in [int(v) for v in sel.values]]
            db = {r['discord_id']: r for r in supabase.table("users").select("*").in_("discord_id", [m.id for m in p_list]).execute().data}
            pool = {m.id: {"n": m.display_name, "t_short": db[m.id]['tier'][0], "score": TIER_SCORE.get(db[m.id]['tier'], 3)} for m in p_list if m.id not in [l.id for l in lds]}
            await i2.channel.send("⚔️ 드래프트!", view=DraftView(pool, lds[0], lds[1], [m.id for m in p_list]))
            await i2.response.edit_message(content="✅ 생성됨", view=None)
        sel.callback = cb; v.add_item(sel); await i.response.send_message("주장 선택:", view=v, ephemeral=True)

    @ui.button(label="🏅 승리 기록", style=discord.ButtonStyle.success, row=1)
    async def b_w(self, i, b):
        v = ui.View(); sel = ui.Select(placeholder="승리팀?", options=[discord.SelectOption(label="1팀", value="1"), discord.SelectOption(label="2팀", value="2")])
        async def cb(i2):
            idx = int(sel.values[0]); win_ids = current_match[f"team{idx}"]; lose_ids = current_match["team2" if idx==1 else "team1"]
            for pid in win_ids:
                try:
                    supabase.rpc('increment_win', {'user_id': pid}).execute(); supabase.rpc('increment_streak', {'user_id': pid}).execute()
                    u = supabase.table("users").select("current_streak, discord_name").eq("discord_id", pid).execute().data[0]
                    if u['current_streak']>=3: await bot.get_channel(RECRUIT_CHANNEL_ID).send(f"🔥 **{u['discord_name']}**님 {u['current_streak']}연승 중!")
                except: pass
            for pid in lose_ids:
                try: supabase.table("users").update({"current_streak": 0}).eq("discord_id", pid).execute()
                except: pass
            await i2.response.edit_message(content="✅ 기록 완료!", view=NextActionView())
        sel.callback = cb; v.add_item(sel); await i.response.send_message("승리팀?", view=v, ephemeral=True)

    @ui.button(label="📝 명단 수정", style=discord.ButtonStyle.secondary, row=1)
    async def b_e(self, i, b): await i.response.send_message("관리:", view=ParticipantEditRootView(), ephemeral=True)

    @ui.button(label="⚙️ 운영진 관리", style=discord.ButtonStyle.secondary, row=1)
    async def b_a(self, i, b):
        res = supabase.table("users").select("discord_id, discord_name, is_admin").order("discord_name").execute()
        v = ui.View(); opts = [discord.SelectOption(label=u['discord_name'], value=str(u['discord_id']), default=u['is_admin']) for u in res.data[:25]]
        sel = ui.Select(placeholder="운영진 체크", min_values=0, max_values=len(opts), options=opts)
        async def acb(i2):
            s_ids = [int(v) for v in sel.values]; supabase.table("users").update({"is_admin": False}).neq("discord_id", 0).execute()
            if s_ids: supabase.table("users").update({"is_admin": True}).in_("discord_id", s_ids).execute()
            await i2.response.send_message("✅ 완료", ephemeral=True)
        sel.callback = acb; v.add_item(sel); await i.response.send_message("설정:", view=v, ephemeral=True)

class JoinView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    @ui.button(label="신청/취소", style=discord.ButtonStyle.success)
    async def j(self, i, b):
        if i.user in active_recruitment["participants"]: active_recruitment["participants"].remove(i.user)
        else: active_recruitment["participants"].append(i.user)
        await i.response.send_message("✅ 완료", ephemeral=True); await update_recruitment_msg()

class RankingBoardView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    @ui.button(label="🏆 TOP 10 확인", style=discord.ButtonStyle.success)
    async def b1(self, i, b):
        res = supabase.table("users").select("discord_id, discord_name, win_count").order("win_count", desc=True).execute()
        list_str = "\n".join([f"{idx+1}위 {u['discord_name']} : {u['win_count']}점" for idx, u in enumerate(res.data[:10])])
        await i.response.send_message(embed=discord.Embed(title="🏆 TOP 10", description=list_str), ephemeral=True)

@bot.command(name="1")
async def m(ctx): await ctx.send("⚡ VOLT 통제실", view=MasterDashboardView())

keep_alive(); bot.run(BOT_TOKEN)
