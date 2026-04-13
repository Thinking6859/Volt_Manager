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

# 📢 선우 님 서버의 실제 채널 ID로 수정 필수!
RECRUIT_CHANNEL_ID = 1493210332598894684  # 내전 모집 및 드래프트 채널
REGISTER_CHANNEL_ID = 1493205766209933404 # 소환사 등록 채널
RANKING_CHANNEL_ID = 1493205910892318850  # 랭킹 게시판 채널

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

# 글로벌 데이터 저장소
TIER_SCORE = {"아이언": 1, "브론즈": 2, "실버": 3, "골드": 4, "플래티넘": 5, "에메랄드": 6, "다이아몬드": 7, "마스터+": 9}
TIER_EMOJI = {"아이언": "⚪", "브론즈": "🟤", "실버": "⚪", "골드": "🟡", "플래티넘": "🟢", "에메랄드": "✳️", "다이아몬드": "💎", "마스터+": "🔮"}
LINE_OPTIONS = [discord.SelectOption(label="탑", value="TOP"), discord.SelectOption(label="정글", value="JUG"), discord.SelectOption(label="미드", value="MID"), discord.SelectOption(label="원딜", value="ADC"), discord.SelectOption(label="서포터", value="SUP")]

# 현재 진행 중인 매치 및 모집 정보
active_recruitment = {"target": 10, "participants": [], "message": None}
current_match = {"ids": [], "team1": [], "team2": [], "names1": [], "names2": [], "scores": [0,0]}

# --- [유틸리티: 권한 체크] ---
async def is_admin(interaction: discord.Interaction):
    if interaction.user.guild_permissions.administrator: return True
    res = supabase.table("users").select("is_admin").eq("discord_id", interaction.user.id).execute()
    if res.data and res.data[0].get("is_admin"): return True
    await interaction.response.send_message("🚫 운영진 권한이 없습니다.", ephemeral=True); return False

# --- [Views: 소환사 등록] ---
class RegisterFlow(ui.View):
    def __init__(self, rid):
        super().__init__(timeout=180); self.rid = rid; self.t, self.m = None, None
    @ui.select(placeholder="티어 선택", options=[discord.SelectOption(label=k) for k in TIER_SCORE.keys()])
    async def s_t(self, i, s):
        self.t = s.values[0]; await i.response.edit_message(content=f"✅ 티어: {self.t}\n주라인을 선택하세요.", view=self)
    @ui.select(placeholder="주라인 선택", options=LINE_OPTIONS)
    async def s_m(self, i, s):
        self.m = s.values[0]; sub_opts = [o for o in LINE_OPTIONS if o.value != self.m]
        sub = ui.Select(placeholder="부라인 선택", options=sub_opts)
        async def cb(i2):
            supabase.table("users").upsert({"discord_id": i2.user.id, "discord_name": i2.user.display_name, "riot_id": self.rid, "tier": self.t, "main_line": self.m, "sub_line": i2.data['values'][0]}).execute()
            await i2.response.edit_message(content="✅ 등록 완료!", view=None)
        sub.callback = cb; self.add_item(sub); await i.response.edit_message(content=f"✅ 주라인: {self.m}\n부라인을 선택하세요.", view=self)

# --- [Views: 내전 모집 및 신청] ---
class JoinView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    async def update_msg(self, interaction):
        names = "\n".join([f"· {m.display_name}" for m in active_recruitment["participants"]])
        content = f"🎮 **VOLT {active_recruitment['target']}인 내전 모집 중**\n\n**신청자 ({len(active_recruitment['participants'])}/{active_recruitment['target']}):**\n{names if names else '신청자가 없습니다.'}"
        await interaction.message.edit(content=content, view=self)

    @ui.button(label="내전 참여 신청/취소", style=discord.ButtonStyle.success)
    async def join(self, i, b):
        res = supabase.table("users").select("discord_id").eq("discord_id", i.user.id).execute()
        if not res.data: return await i.response.send_message("❗ 먼저 소환사 등록을 해주세요!", ephemeral=True)
        if i.user in active_recruitment["participants"]: active_recruitment["participants"].remove(i.user); msg = "❌ 취소됨"
        else:
            if len(active_recruitment["participants"]) >= active_recruitment["target"]: return await i.response.send_message("❗ 정원 초과", ephemeral=True)
            active_recruitment["participants"].append(i.user); msg = "✅ 신청됨"
        await i.response.send_message(msg, ephemeral=True); await self.update_msg(i)

# --- [Views: 랭킹 게시판] ---
class RankingBoardView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    async def show_rank(self, i, col, title, unit):
        res = supabase.table("users").select("discord_id, discord_name, win_count, participation_count").order(col, desc=True).execute()
        all_u = res.data
        my_rank = next((idx+1 for idx, u in enumerate(all_u) if u['discord_id']==i.user.id), "미등록")
        my_score = next((u[col] for u in all_u if u['discord_id']==i.user.id), 0)
        embed = discord.Embed(title=title, color=0xFFD700 if col=="win_count" else 0x5865F2)
        embed.description = f"👤 **{i.user.display_name}** 순위: **{my_rank}위** ({my_score}{unit})"
        rank_list = [f"{'🥇' if idx==0 else '🥈' if idx==1 else '🥉' if idx==2 else f'**{idx+1}위**'} {u['discord_name']} : {u[col]}{unit}" for idx, u in enumerate(all_u[:10])]
        embed.add_field(name="──────────────", value="\n".join(rank_list) if rank_list else "내역 없음", inline=False)
        await i.response.send_message(embed=embed, ephemeral=True)

    @ui.button(label="🏆 승리점수 확인", style=discord.ButtonStyle.success)
    async def b1(self, i, b): await self.show_rank(i, "win_count", "🏆 승리점수 TOP 10", "점")
    @ui.button(label="⚔️ 참여점수 확인", style=discord.ButtonStyle.primary)
    async def b2(self, i, b): await self.show_rank(i, "participation_count", "⚔️ 참여점수 TOP 10", "회")

# --- [Views: 드래프트 로직] ---
class DraftView(ui.View):
    def __init__(self, p, l1, l2, ids):
        super().__init__(timeout=None); self.p, self.l, self.ids = p, {1: l1, 2: l2}, ids
        self.teams, self.t_ids, self.t_scores = {1: [], 2: []}, {1: [l1.id], 2: [l2.id]}, {1: 0, 2: 0}
        self.order, self.idx = [1, 2, 2, 1, 1, 2, 2, 1], 0
        for idx, leader in enumerate([l1, l2], 1):
            d = supabase.table("users").select("*").eq("discord_id", leader.id).execute().data[0]
            self.t_scores[idx] += TIER_SCORE.get(d['tier'], 3)
            self.teams[idx].append(f"👑 **[{d['tier'][0]}] {leader.display_name}** ({d['main_line']}/{d['sub_line']})")
        self.create_buttons()

    def create_buttons(self):
        self.clear_items()
        for d_id, d in self.p.items():
            btn = ui.Button(label=f"[{d['t_short']}] {d['n']} ({d['lines']})", custom_id=str(d_id))
            btn.callback = self.pick_callback; self.add_item(btn)

    async def pick_callback(self, i):
        if i.user.id != self.l[self.order[self.idx]].id: return await i.response.send_message("차례가 아닙니다.", ephemeral=True)
        p_id = int(i.data['custom_id']); p = self.p.pop(p_id); t_num = self.order[self.idx]
        self.teams[t_num].append(f"· **[{p['t_short']}] {p['n']}** ({p['lines']})")
        self.t_ids[t_num].append(p_id); self.t_scores[t_num] += p['score']; self.idx += 1
        if not self.p or self.idx >= len(self.order): await self.finish(i)
        else: self.create_buttons(); await i.response.edit_message(content=f"🔵 다음 선택: {self.l[self.order[self.idx]].mention}", view=self)

    async def finish(self, i):
        global current_match
        for pid in self.ids: supabase.rpc('increment_participation', {'user_id': pid}).execute()
        s1, s2 = self.t_scores[1], self.t_scores[2]; prob = round((s1/(s1+s2))*100) if (s1+s2)>0 else 50
        current_match = {"ids": self.ids, "team1": self.t_ids[1], "team2": self.t_ids[2], "names1": [t.split("**")[1].split("] ")[1] for t in self.teams[1]], "names2": [t.split("**")[1].split("] ")[1] for t in self.teams[2]]}
        embed = discord.Embed(title="⚔️ VOLT 라인업", description=f"💡 분석: {'🟦 1팀' if s1>=s2 else '🟥 2팀'} 우세 ({prob if s1>=s2 else 100-prob}%)", color=0x5865F2)
        embed.add_field(name=f"🟦 1팀 ({s1}점)", value="\n".join(self.teams[1]), inline=False)
        embed.add_field(name=f"🟥 2팀 ({s2}점)", value="\n".join(self.teams[2]), inline=False)
        await bot.get_channel(RECRUIT_CHANNEL_ID).send(embed=embed)
        await i.response.edit_message(content="✅ 드래프트 종료!", view=None)

# --- [Views: 명단 수정] ---
class ParticipantEditView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        if active_recruitment["participants"]:
            opts = [discord.SelectOption(label=m.display_name, value=str(m.id)) for m in active_recruitment["participants"][:25]]
            sel = ui.Select(placeholder="제외할 인원 선택", min_values=1, max_values=len(opts), options=opts)
            async def cb(i):
                to_rem = [int(v) for v in sel.values]
                active_recruitment["participants"] = [m for m in active_recruitment["participants"] if m.id not in to_rem]
                if active_recruitment["message"]:
                    names = "\n".join([f"· {m.display_name}" for m in active_recruitment["participants"]])
                    await active_recruitment["message"].edit(content=f"🎮 **VOLT 모집 중**\n\n**신청자 ({len(active_recruitment['participants'])}/10):**\n{names}")
                await i.response.send_message("✅ 명단 수정 완료", ephemeral=True)
            sel.callback = cb; self.add_item(sel)

# --- [메인 통제실 패널] ---
class MasterDashboardView(ui.View):
    def __init__(self): super().__init__(timeout=None)

    @ui.button(label="📢 모집/등록/랭킹 배포", style=discord.ButtonStyle.primary, row=0)
    async def b_notice(self, i, b):
        if not await is_admin(i): return
        v = ui.View(); sel = ui.Select(placeholder="채널 배포 선택", options=[
            discord.SelectOption(label="소환사 등록 센터", value="reg"),
            discord.SelectOption(label="내전 모집 시작 (10인)", value="rec"),
            discord.SelectOption(label="실시간 랭킹 게시판", value="rank")
        ])
        async def cb(i2):
            val = sel.values[0]
            if val == "reg":
                bv = ui.View(timeout=None); btn = ui.Button(label="소환사 등록/수정", style=discord.ButtonStyle.primary)
                async def rcb(i3):
                    m = ui.Modal(title="VOLT 등록"); rid = ui.TextInput(label="라이엇 ID", placeholder="닉네임#TAG"); m.add_item(rid)
                    async def os(i4): await i4.response.send_message("정보 입력", view=RegisterFlow(rid.value), ephemeral=True)
                    m.on_submit = os; await i3.response.send_modal(m)
                btn.callback = rcb; bv.add_item(btn)
                await bot.get_channel(REGISTER_CHANNEL_ID).send("⚡ **VOLT 소환사 등록 센터**", view=bv)
            elif val == "rec":
                active_recruitment["participants"] = []
                jv = JoinView()
                msg = await bot.get_channel(RECRUIT_CHANNEL_ID).send("🎮 **내전 모집 시작!**", view=jv)
                active_recruitment["message"] = msg
            elif val == "rank":
                await bot.get_channel(RANKING_CHANNEL_ID).send("📊 **VOLT 실시간 랭킹**", view=RankingBoardView())
            await i2.response.edit_message(content="✅ 배포 완료", view=None)
        sel.callback = cb; v.add_item(sel); await i.response.send_message("배포 선택:", view=v, ephemeral=True)

    @ui.button(label="📝 명단 수정", style=discord.ButtonStyle.secondary, row=0)
    async def b_edit(self, i, b):
        if not await is_admin(i): return
        if not active_recruitment["participants"]: return await i.response.send_message("신청자가 없습니다.", ephemeral=True)
        await i.response.send_message("제외할 인원을 골라주세요:", view=ParticipantEditView(), ephemeral=True)

    @ui.button(label="⚔️ 드래프트 시작 (명단기반)", style=discord.ButtonStyle.danger, row=1)
    async def b_df(self, i, b):
        if not await is_admin(i): return
        p_list = active_recruitment["participants"]
        if len(p_list) < 10: return await i.response.send_message(f"인원 부족 ({len(p_list)}/10)", ephemeral=True)
        v = ui.View(); opts = [discord.SelectOption(label=m.display_name, value=str(m.id)) for m in p_list]
        sel = ui.Select(placeholder="주장 2명 선택", min_values=2, max_values=2, options=opts)
        async def cb(i2):
            l_ids = [int(v) for v in sel.values]; leaders = [m for m in p_list if m.id in l_ids]
            remains = [m for m in p_list if m.id not in l_ids]
            res = supabase.table("users").select("*").in_("discord_id", [m.id for m in p_list]).execute(); db = {r['discord_id']: r for r in res.data}
            pool = {m.id: {"n": m.display_name, "t_short": db[m.id]['tier'][0], "lines": f"{db[m.id]['main_line']}/{db[m.id]['sub_line']}", "score": TIER_SCORE.get(db[m.id]['tier'], 3)} for m in remains}
            await i2.channel.send(f"⚔️ **드래프트 시작!**", view=DraftView(pool, leaders[0], leaders[1], [m.id for m in p_list]))
            await i2.response.edit_message(content="시작됨", view=None)
        sel.callback = cb; v.add_item(sel); await i.response.send_message("주장을 선택하세요:", view=v, ephemeral=True)

    @ui.button(label="🏅 승리 기록", style=discord.ButtonStyle.success, row=1)
    async def b_win(self, i, b):
        if not await is_admin(i): return
        if not current_match["team1"]: return await i.response.send_message("데이터 없음", ephemeral=True)
        v = ui.View(); sel = ui.Select(placeholder="승리팀 선택", options=[discord.SelectOption(label="1팀", value="1"), discord.SelectOption(label="2팀", value="2")])
        async def cb(i2):
            idx = int(sel.values[0]); win_ids = current_match[f"team{idx}"]; lose_ids = current_match["team2" if idx==1 else "team1"]
            for pid in win_ids: 
                supabase.rpc('increment_win', {'user_id': pid}).execute()
                try: supabase.rpc('increment_streak', {'user_id': pid}).execute()
                except: pass
                u = supabase.table("users").select("current_streak, discord_name").eq("discord_id", pid).execute().data[0]
                if u['current_streak']>=3: await bot.get_channel(RECRUIT_CHANNEL_ID).send(f"🔥 **{u['discord_name']}**님 {u['current_streak']}연승 중!")
            for pid in lose_ids: supabase.table("users").update({"current_streak": 0}).eq("discord_id", pid).execute()
            await i2.response.edit_message(content="✅ 완료", view=None)
        sel.callback = cb; v.add_item(sel); await i.response.send_message("승리팀?", v, ephemeral=True)

    @ui.button(label="⚙️ 운영진 관리", style=discord.ButtonStyle.secondary, row=2)
    async def b_adm(self, i, b):
        if not i.user.guild_permissions.administrator: return
        await i.response.defer(ephemeral=True); res = supabase.table("users").select("discord_id, discord_name, is_admin").order("discord_name").execute()
        v = ui.View(); opts = [discord.SelectOption(label=u['discord_name'], value=str(u['discord_id']), default=u['is_admin']) for u in res.data[:25]]
        sel = ui.Select(placeholder="운영진 체크", min_values=0, max_values=len(opts), options=opts)
        async def cb(i2):
            await i2.response.defer(ephemeral=True); selected = [int(v) for v in sel.values]
            supabase.table("users").update({"is_admin": False}).neq("discord_id", 0).execute()
            if selected: supabase.table("users").update({"is_admin": True}).in_("discord_id", selected).execute()
            await i2.followup.send("✅ 동기화 완료", ephemeral=True)
        sel.callback = cb; v.add_item(sel); await i.followup.send("운영진 관리:", view=v, ephemeral=True)

@bot.command(name="내정보")
async def info(ctx):
    res = supabase.table("users").select("*").eq("discord_id", ctx.author.id).execute()
    if not res.data: return await ctx.send("미등록 유저")
    u = res.data[0]; wr = (u['win_count']/u['participation_count']*100) if u['participation_count']>0 else 0
    e = discord.Embed(title=f"👤 {u['discord_name']} 리포트", color=0x5865F2)
    e.add_field(name="티어", value=f"{TIER_EMOJI.get(u['tier'], '⚪')} {u['tier']}")
    e.add_field(name="전적", value=f"🏆 {u['win_count']}승 / ⚔️ {u['participation_count']}회")
    e.add_field(name="연승", value=f"🔥 {u['current_streak']} (최고 {u.get('max_streak',0)})")
    await ctx.send(embed=e)

@bot.command(name="1")
async def master(ctx): await ctx.send("⚡ **VOLT 클랜 통제실**", view=MasterDashboardView())

keep_alive(); bot.run(BOT_TOKEN)
