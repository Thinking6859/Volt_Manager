import os
import discord
from discord.ext import commands
from discord import ui
from supabase import create_client, Client
from keep_alive import keep_alive 

# --- [1. 환경 설정] ---
try: from dotenv import load_dotenv; load_dotenv()
except: pass

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BOT_TOKEN = os.getenv("DISCORD_TOKEN")

# 📢 선우 님 서버 ID로 수정 필수!
PUBLIC_CHANNEL_ID = 1493116057488199741 
RANKING_CHANNEL_ID = 1493138106868568075

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

TIER_SCORE = {"아이언": 1, "브론즈": 2, "실버": 3, "골드": 4, "플래티넘": 5, "에메랄드": 6, "다이아몬드": 7, "마스터+": 9}
TIER_EMOJI = {"아이언": "⚪", "브론즈": "🟤", "실버": "⚪", "골드": "🟡", "플래티넘": "🟢", "에메랄드": "✳️", "다이아몬드": "💎", "마스터+": "🔮"}
LINE_OPTIONS = [discord.SelectOption(label="탑", value="TOP"), discord.SelectOption(label="정글", value="JUG"), discord.SelectOption(label="미드", value="MID"), discord.SelectOption(label="원딜", value="ADC"), discord.SelectOption(label="서포터", value="SUP")]

current_match = {"ids": [], "team1": [], "team2": [], "names1": [], "names2": [], "scores": [0,0]}

# --- [공통 권한 로직] ---
async def is_admin(interaction: discord.Interaction):
    if interaction.user.guild_permissions.administrator: return True
    res = supabase.table("users").select("is_admin").eq("discord_id", interaction.user.id).execute()
    if res.data and res.data[0].get("is_admin"): return True
    await interaction.response.send_message("🚫 운영진 권한이 없습니다.", ephemeral=True); return False

# --- [Views] ---

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

class DraftView(ui.View):
    def __init__(self, pool, l1, l2, ids):
        super().__init__(timeout=None); self.pool, self.leaders, self.ids = pool, {1: l1, 2: l2}, ids
        self.teams, self.team_ids, self.team_scores = {1: [], 2: []}, {1: [l1.id], 2: [l2.id]}, {1: 0, 2: 0}
        self.order, self.idx = [1, 2, 2, 1, 1, 2, 2, 1], 0
        for idx, leader in enumerate([l1, l2], 1):
            d = supabase.table("users").select("*").eq("discord_id", leader.id).execute().data[0]
            self.team_scores[idx] += TIER_SCORE.get(d['tier'], 3)
            self.teams[idx].append(f"👑 **[{d['tier'][0]}] {leader.display_name}** ({d['main_line']}/{d['sub_line']})")
        self.create_buttons()

    def create_buttons(self):
        self.clear_items()
        for d_id, d in self.pool.items():
            btn = ui.Button(label=f"[{d['t_short']}] {d['n']} ({d['lines']})", custom_id=str(d_id))
            btn.callback = self.pick_callback; self.add_item(btn)

    async def pick_callback(self, i):
        if i.user.id != self.leaders[self.order[self.idx]].id: return await i.response.send_message("본인 차례가 아닙니다.", ephemeral=True)
        p_id = int(i.data['custom_id']); p = self.pool.pop(p_id); t_num = self.order[self.idx]
        self.teams[t_num].append(f"· **[{p['t_short']}] {p['n']}** ({p['lines']})")
        self.team_ids[t_num].append(p_id); self.team_scores[t_num] += p['score']; self.idx += 1
        if not self.pool or self.idx >= len(self.order): await self.finish(i)
        else: self.create_buttons(); await i.response.edit_message(content=f"🔵 다음 선택: {self.leaders[self.order[self.idx]].mention}님", view=self)

    async def finish(self, i):
        global current_match
        for pid in self.ids: supabase.rpc('increment_participation', {'user_id': pid}).execute()
        s1, s2 = self.team_scores[1], self.team_scores[2]
        current_match = {"ids": self.ids, "team1": self.team_ids[1], "team2": self.team_ids[2], "names1": [t.split("**")[1].split("] ")[1] for t in self.teams[1]], "names2": [t.split("**")[1].split("] ")[1] for t in self.teams[2]], "scores": [s1, s2]}
        
        prob = round((s1 / (s1 + s2)) * 100) if (s1+s2)>0 else 50
        predict = f"💡 **볼티 예측:** {'🟦 1팀' if s1 >= s2 else '🟥 2팀'} 우세 ({prob if s1 >= s2 else 100-prob}%)"
        embed = discord.Embed(title="⚔️ VOLT 내전 라인업", description=predict, color=0x5865F2)
        embed.add_field(name=f"🟦 1팀 ({s1}점)", value="\n".join(self.teams[1]), inline=False)
        embed.add_field(name=f"🟥 2팀 ({s2}점)", value="\n".join(self.teams[2]), inline=False)
        await bot.get_channel(PUBLIC_CHANNEL_ID).send(embed=embed)
        await i.response.edit_message(content="✅ 공지방 전송 완료!", view=None)

class NextStepView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    @ui.button(label="♻️ 재드래프트", style=discord.ButtonStyle.primary)
    async def r(self, i, b):
        await i.response.defer(ephemeral=True); res = supabase.table("users").select("*").in_("discord_id", current_match["ids"]).execute()
        db = {r['discord_id']: r for r in res.data}; v = ui.View(); sel = ui.Select(cls=ui.UserSelect, placeholder="주장 2명 선택", min_values=2, max_values=2)
        async def cb(i2):
            l1, l2 = sel.values; p_ids = current_match["ids"]
            pool = {m_id: {"n": db[m_id]['discord_name'], "t_short": db[m_id]['tier'][0], "lines": f"{db[m_id]['main_line']}/{db[m_id]['sub_line']}", "score": TIER_SCORE.get(db[m_id]['tier'], 3)} for m_id in p_ids if m_id not in [l1.id, l2.id]}
            await i2.channel.send("⚔️ **재드래프트 시작!**", view=DraftView(pool, l1, l2, p_ids)); await i2.response.edit_message(content="시작됨", view=None)
        sel.callback = cb; v.add_item(sel); await i.followup.send("새 주장을 골라주세요:", view=v, ephemeral=True)
    @ui.button(label="🔥 이대로 리매치", style=discord.ButtonStyle.success)
    async def rm(self, i, b):
        for pid in current_match["ids"]: supabase.rpc('increment_participation', {'user_id': pid}).execute()
        await bot.get_channel(PUBLIC_CHANNEL_ID).send("🔥 **팀 변경 없이 리매치 진행!**"); await i.response.edit_message(content="기록 완료", view=None)
    @ui.button(label="🏁 종료", style=discord.ButtonStyle.secondary)
    async def end(self, i, b): await i.response.edit_message(content="✅ 수고하셨습니다!", view=None)

class AdminManageView(ui.View):
    def __init__(self, all_users):
        super().__init__(timeout=None)
        opts = [discord.SelectOption(label=f"{u['discord_name']} ({'운영진' if u['is_admin'] else '일반'})", value=str(u['discord_id']), default=u['is_admin']) for u in all_users[:25]]
        self.sel = ui.Select(placeholder="운영진 체크 (체크=부여, 해제=박탈)", min_values=0, max_values=len(opts), options=opts)
        async def cb(i):
            await i.response.defer(ephemeral=True); selected = [int(v) for v in self.sel.values]
            supabase.table("users").update({"is_admin": False}).neq("discord_id", 0).execute()
            if selected: supabase.table("users").update({"is_admin": True}).in_("discord_id", selected).execute()
            await i.followup.send(f"✅ 운영진 명단 동기화 완료! ({len(selected)}명)", ephemeral=True)
        self.sel.callback = cb; self.add_item(self.sel)

class MasterDashboardView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    @ui.button(label="📝 등록 센터 배포", style=discord.ButtonStyle.primary, row=0)
    async def b_reg(self, i, b):
        if not await is_admin(i): return
        v = ui.View(timeout=None); btn = ui.Button(label="클랜원 정보 등록/수정", style=discord.ButtonStyle.primary)
        async def cb(i2):
            m = ui.Modal(title="VOLT 등록"); rid = ui.TextInput(label="라이엇 ID", placeholder="닉네임#TAG"); m.add_item(rid)
            async def os(i3): await i3.response.send_message("정보를 선택하세요.", view=RegisterFlow(rid.value), ephemeral=True)
            m.on_submit = os; await i2.response.send_modal(m)
        btn.callback = cb; v.add_item(btn); await bot.get_channel(PUBLIC_CHANNEL_ID).send("⚡ **VOLT 플레이어 등록 센터**", view=v)
        await i.response.send_message("✅ 배포 완료", ephemeral=True)

    @ui.button(label="⚔️ 드래프트 시작", style=discord.ButtonStyle.danger, row=0)
    async def b_df(self, i, b):
        if not await is_admin(i): return
        if not i.user.voice: return await i.response.send_message("음성 채널 필수", ephemeral=True)
        mbs = [m for m in i.user.voice.channel.members if not m.bot]
        if len(mbs) < 10: return await i.response.send_message(f"10명 필요 (현재 {len(mbs)}명)", ephemeral=True)
        res = supabase.table("users").select("*").in_("discord_id", [m.id for m in mbs]).execute(); db = {r['discord_id']: r for r in res.data}
        unreg = [m.display_name for m in mbs if m.id not in db]
        if unreg: return await i.response.send_message(f"미등록 멤버: {', '.join(unreg)}", ephemeral=True)
        v = ui.View(); sel = ui.Select(cls=ui.UserSelect, placeholder="주장 2명 선택", min_values=2, max_values=2)
        async def cb(i2):
            l1, l2 = sel.values; p = {m.id: {"n": m.display_name, "t_short": db[m.id]['tier'][0], "lines": f"{db[m.id]['main_line']}/{db[m.id]['sub_line']}", "score": TIER_SCORE.get(db[m.id]['tier'], 3)} for m in mbs if m.id not in [l1.id, l2.id]}
            await i2.channel.send("⚔️ **드래프트 시작!**", view=DraftView(p, l1, l2, [m.id for m in mbs])); await i2.response.edit_message(content="시작됨", view=None)
        sel.callback = cb; v.add_item(sel); await i.response.send_message("주장을 선택하세요:", view=v, ephemeral=True)

    @ui.button(label="🏅 승리 팀 기록", style=discord.ButtonStyle.success, row=1)
    async def b_win(self, i, b):
        if not await is_admin(i): return
        if not current_match["team1"]: return await i.response.send_message("경기 데이터 없음", ephemeral=True)
        v = ui.View(); sel = ui.Select(placeholder="승리 팀 선택", options=[discord.SelectOption(label="1팀 (🟦)", value="1"), discord.SelectOption(label="2팀 (🟥)", value="2")])
        async def cb(i2):
            idx = int(sel.values[0]); win_ids = current_match[f"team{idx}"]; lose_ids = current_match["team2" if idx == 1 else "team1"]
            for pid in win_ids: 
                supabase.rpc('increment_win', {'user_id': pid}).execute()
                try: supabase.rpc('increment_streak', {'user_id': pid}).execute()
                except: pass
                u = supabase.table("users").select("current_streak, discord_name").eq("discord_id", pid).execute().data[0]
                if u['current_streak'] >= 3: await bot.get_channel(PUBLIC_CHANNEL_ID).send(f"🔥 **{u['discord_name']}**님 {u['current_streak']}연승 중!")
            for pid in lose_ids: supabase.table("users").update({"current_streak": 0}).eq("discord_id", pid).execute()
            await bot.get_channel(PUBLIC_CHANNEL_ID).send(f"🎊 **{idx}팀 승리!** 기록 완료."); await i2.response.edit_message(content="✅ 기록 완료! 다음 단계:", view=NextStepView())
        sel.callback = cb; v.add_item(sel); await i.response.send_message("어느 팀이 이겼나요?", view=v, ephemeral=True)

    @ui.button(label="⚙️ 운영진 관리 (일괄)", style=discord.ButtonStyle.secondary, row=1)
    async def b_adm(self, i, b):
        if not i.user.guild_permissions.administrator: return
        await i.response.defer(ephemeral=True); res = supabase.table("users").select("discord_id, discord_name, is_admin").order("discord_name").execute()
        if not res.data: return await i.followup.send("DB에 유저가 없습니다.", ephemeral=True)
        await i.followup.send("운영진 관리:", view=AdminManageView(res.data), ephemeral=True)

@bot.command(name="내정보")
async def info(ctx):
    res = supabase.table("users").select("*").eq("discord_id", ctx.author.id).execute()
    if not res.data: return await ctx.send("❗ 등록되지 않은 유저입니다.")
    u = res.data[0]; wr = (u['win_count']/u['participation_count']*100) if u['participation_count']>0 else 0
    e = discord.Embed(title=f"👤 {u['discord_name']} 리포트", color=0x5865F2)
    e.add_field(name="티어", value=f"{TIER_EMOJI.get(u['tier'], '⚪')} {u['tier']}")
    e.add_field(name="전적", value=f"🏆 {u['win_count']}승 / ⚔️ {u['participation_count']}회", inline=False)
    e.add_field(name="승률", value=f"{wr:.1f}%")
    e.add_field(name="연승", value=f"🔥 {u.get('current_streak',0)} (최대 {u.get('max_streak',0)})")
    await ctx.send(embed=e)

@bot.command(name="1")
async def master(ctx): await ctx.send("⚡ VOLT 통제실", view=MasterDashboardView())

keep_alive(); bot.run(BOT_TOKEN)