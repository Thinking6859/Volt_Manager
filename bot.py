import os
import discord
from discord.ext import commands
from discord import ui
from supabase import create_client, Client
from keep_alive import keep_alive 

# --- [1. 설정 및 환경변수] ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BOT_TOKEN = os.getenv("DISCORD_TOKEN")

# 📢 각 채널 ID를 정확히 넣어주세요!
PUBLIC_CHANNEL_ID = 1493116057488199741  # 내전 공지방
RANKING_CHANNEL_ID = 1493138106868568075 # 랭킹 게시판

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)

# 데이터 매핑
TIER_SCORE = {"아이언": 1, "브론즈": 2, "실버": 3, "골드": 4, "플래티넘": 5, "에메랄드": 6, "다이아몬드": 7, "마스터+": 9}
TIER_EMOJI = {"아이언": "⚪", "브론즈": "🟤", "실버": "⚪", "골드": "🟡", "플래티넘": "🟢", "에메랄드": "✳️", "다이아몬드": "💎", "마스터+": "🔮"}
LINE_OPTIONS = [
    discord.SelectOption(label="탑", value="TOP"), discord.SelectOption(label="정글", value="JUG"),
    discord.SelectOption(label="미드", value="MID"), discord.SelectOption(label="원딜", value="ADC"),
    discord.SelectOption(label="서포터", value="SUP")
]

last_match_teams = {1: [], 2: []}

# --- [권한 검증] ---
async def is_admin(interaction: discord.Interaction):
    if interaction.user.guild_permissions.administrator: return True
    try:
        res = supabase.table("users").select("is_admin").eq("discord_id", interaction.user.id).execute()
        if res.data and res.data[0].get("is_admin") is True: return True
    except: pass
    await interaction.response.send_message("🚫 운영진 권한이 없습니다.", ephemeral=True)
    return False

# --- [2. 등록 시스템] ---
class RegisterFlow(ui.View):
    def __init__(self, riot_id):
        super().__init__(timeout=180)
        self.riot_id = riot_id
        self.tier, self.main_line = None, None

    @ui.select(placeholder="1. 티어 선택", options=[discord.SelectOption(label=k) for k in TIER_SCORE.keys()])
    async def select_tier(self, interaction, select):
        self.tier = select.values[0]
        await interaction.response.edit_message(content=f"✅ 티어: {self.tier}\n다음으로 **주라인**을 선택하세요.", view=self)

    @ui.select(placeholder="2. 주라인 선택", options=LINE_OPTIONS)
    async def select_main(self, interaction, select):
        self.main_line = select.values[0]
        sub_opts = [opt for opt in LINE_OPTIONS if opt.value != self.main_line]
        sub_select = ui.Select(placeholder="3. 부라인 선택", options=sub_opts)
        
        async def sub_callback(i):
            data = {
                "discord_id": i.user.id, "discord_name": i.user.display_name,
                "riot_id": self.riot_id, "tier": self.tier, "main_line": self.main_line, "sub_line": i.data['values'][0]
            }
            supabase.table("users").upsert(data).execute()
            await i.response.edit_message(content=f"🎊 **{self.riot_id}**님 등록 완료!", view=None)
        
        sub_select.callback = sub_callback
        self.add_item(sub_select)
        await interaction.response.edit_message(content=f"✅ 주라인: {self.main_line}\n마지막으로 **부라인**을 선택하세요.", view=self)

# --- [3. 모집 시스템] ---
class RecruitManageView(ui.View):
    def __init__(self, join_view):
        super().__init__(timeout=None)
        self.join_view = join_view 

    @ui.select(cls=ui.UserSelect, placeholder="➕ 멤버 수동 추가", row=0)
    async def add_member(self, interaction, select):
        user = select.values[0]
        self.join_view.participants.append(user)
        await self.join_view.update_message()
        await interaction.response.send_message(f"✅ {user.display_name} 추가.", ephemeral=True)

    @ui.select(cls=ui.UserSelect, placeholder="➖ 멤버 강제 퇴장", row=1)
    async def remove_member(self, interaction, select):
        user = select.values[0]
        if user in self.join_view.participants: self.join_view.participants.remove(user)
        await self.join_view.update_message()
        await interaction.response.send_message(f"🛑 {user.display_name} 제거.", ephemeral=True)

    @ui.button(label="💣 모집판 폭파", style=discord.ButtonStyle.danger, row=2)
    async def destroy(self, interaction, button):
        self.join_view.stop()
        if self.join_view.message: await self.join_view.message.edit(content="💣 모집 취소됨.", view=None)
        await interaction.response.edit_message(content="✅ 폭파 완료.", view=None)

class JoinView(ui.View):
    def __init__(self, target):
        super().__init__(timeout=None)
        self.target, self.participants, self.message = target, [], None

    async def update_message(self):
        names = "\n".join([f"- {m.display_name}" for m in self.participants])
        content = f"🎮 **VOLT {self.target}인 내전 모집**\n\n**신청자 ({len(self.participants)}/{self.target}):**\n{names if names else '없음'}"
        for x in self.children:
            if isinstance(x, ui.Button) and x.custom_id == "btn_join":
                x.label = f"내전 참여 ({len(self.participants)}/{self.target})"
        if self.message: await self.message.edit(content=content, view=self)

    @ui.button(label="내전 참여 신청", style=discord.ButtonStyle.success, custom_id="btn_join")
    async def join_btn(self, interaction, button):
        if not self.message: self.message = interaction.message
        if interaction.user in self.participants: self.participants.remove(interaction.user)
        else: self.participants.append(interaction.user)
        await self.update_message()
        await interaction.response.send_message("신청 상태가 변경되었습니다.", ephemeral=True)

# --- [4. 드래프트 시스템] ---
class DraftView(ui.View):
    def __init__(self, pool_data, l1, l2, admin_id, all_player_ids):
        super().__init__(timeout=None)
        self.pool, self.leaders, self.admin_id, self.all_player_ids = pool_data, {1: l1, 2: l2}, admin_id, all_player_ids
        self.teams, self.team_ids, self.team_scores = {1: [], 2: []}, {1: [l1.id], 2: [l2.id]}, {1: 0, 2: 0}
        self.order, self.idx = [1, 2, 2, 1, 1, 2, 2, 1], 0

        for idx, leader in enumerate([l1, l2], 1):
            l_data = supabase.table("users").select("*").eq("discord_id", leader.id).execute().data[0]
            self.team_scores[idx] += TIER_SCORE.get(l_data['tier'], 3)
            self.teams[idx].append(f"👑 **{leader.display_name}** ({TIER_EMOJI.get(l_data['tier'])})")
        self.create_buttons()

    def create_buttons(self):
        self.clear_items()
        for d_id, d in self.pool.items():
            btn = ui.Button(label=f"[{d['t_short']}] {d['n']}", custom_id=str(d_id))
            btn.callback = self.pick_callback
            self.add_item(btn)

    async def pick_callback(self, interaction):
        curr_leader = self.leaders[self.order[self.idx]]
        # ⚠️ 테스트를 위해 본인이라면 무조건 통과하게 할 수도 있습니다.
        if interaction.user.id != curr_leader.id:
            return await interaction.response.send_message("차례가 아닙니다.", ephemeral=True)
        
        p_id = int(interaction.data['custom_id'])
        p = self.pool.pop(p_id)
        t_num = self.order[self.idx]
        self.teams[t_num].append(f"· {p['n']} ({p['t_emoji']})")
        self.team_ids[t_num].append(p_id)
        self.team_scores[t_num] += p['score']
        self.idx += 1

        if not self.pool or self.idx >= len(self.order):
            await self.finish(interaction)
        else:
            self.create_buttons()
            await interaction.response.edit_message(content=f"🔵 **다음 선택:** {self.leaders[self.order[self.idx]].mention}", view=self)

    async def finish(self, interaction):
        global last_match_teams
        for p_id in self.all_player_ids: supabase.rpc('increment_participation', {'user_id': p_id}).execute()
        last_match_teams[1], last_match_teams[2] = self.team_ids[1], self.team_ids[2]
        
        embed = discord.Embed(title="⚔️ 드래프트 완료", color=0x5865F2)
        embed.add_field(name=f"🟦 1팀 ({self.team_scores[1]}점)", value="\n".join(self.teams[1]))
        embed.add_field(name=f"🟥 2팀 ({self.team_scores[2]}점)", value="\n".join(self.teams[2]))
        
        await bot.get_channel(PUBLIC_CHANNEL_ID).send(embed=embed)
        await interaction.response.edit_message(content="✅ 전송 완료.", view=None)

# --- [5. 마스터 대시보드] ---
class MasterDashboardView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="📝 등록 센터 배포", style=discord.ButtonStyle.primary, row=0)
    async def btn_reg(self, interaction, button):
        if not await is_admin(interaction): return
        reg_view = ui.View(timeout=None)
        reg_btn = ui.Button(label="클랜원 등록/수정", style=discord.ButtonStyle.primary)
        async def reg_callback(i):
            modal = ui.Modal(title="멤버 등록")
            rid = ui.TextInput(label="라이엇 ID", placeholder="닉네임#태그")
            modal.add_item(rid)
            async def on_submit(i2): await i2.response.send_message("정보 선택", view=RegisterFlow(rid.value), ephemeral=True)
            modal.on_submit = on_submit
            await i.response.send_modal(modal)
        reg_btn.callback = reg_callback
        reg_view.add_item(reg_btn)
        await bot.get_channel(PUBLIC_CHANNEL_ID).send("⚡ **VOLT 등록 센터**", view=reg_view)
        await interaction.response.send_message("✅ 배포 완료", ephemeral=True)

    @ui.button(label="📢 내전 모집 시작", style=discord.ButtonStyle.success, row=0)
    async def btn_recruit(self, interaction, button):
        if not await is_admin(interaction): return
        view = ui.View()
        sel = ui.Select(placeholder="인원", options=[discord.SelectOption(label=f"{x}인", value=str(x)) for x in [10, 20, 30]])
        async def cb(i):
            jv = JoinView(int(sel.values[0]))
            msg = await bot.get_channel(PUBLIC_CHANNEL_ID).send(f"🎮 **내전 모집!**", view=jv)
            jv.message = msg
            await i.response.edit_message(content="✅ 리모컨 활성화.", view=RecruitManageView(jv))
        sel.callback = cb
        view.add_item(sel)
        await interaction.response.send_message("인원 선택:", view=view, ephemeral=True)

    @ui.button(label="⚔️ 드래프트 시작", style=discord.ButtonStyle.danger, row=0)
    async def btn_draft(self, interaction, button):
        if not await is_admin(interaction): return
        if not interaction.user.voice: return await interaction.response.send_message("음성채널 필수", ephemeral=True)
        
        # ⚠️ 테스트 시 아래 10명 제한 주석 처리 가능
        v_members = [m for m in interaction.user.voice.channel.members if not m.bot]
        
        view = ui.View()
        sel = ui.Select(cls=ui.UserSelect, placeholder="주장 2명 선택", min_values=2, max_values=2)
        async def sel_cb(i):
            l1, l2 = sel.values
            res = supabase.table("users").select("*").in_("discord_id", [m.id for m in v_members]).execute()
            db_data = {r['discord_id']: r for r in res.data}
            pool = {m.id: {"n": m.display_name, "t_short": db_data[m.id]['tier'][0], "t_emoji": TIER_EMOJI.get(db_data[m.id]['tier'], "⚪"),
                          "lines": "??", "score": TIER_SCORE.get(db_data[m.id]['tier'], 3)} for m in v_members if m.id not in [l1.id, l2.id]}
            await i.channel.send(f"⚔️ **드래프트!**", view=DraftView(pool, l1, l2, i.user.id, [m.id for m in v_members]))
            await i.response.edit_message(content="✅ 시작됨", view=None)
        sel.callback = sel_cb
        view.add_item(sel)
        await interaction.response.send_message("주장 선택:", view=view, ephemeral=True)

    @ui.button(label="🏅 결과 기록", style=discord.ButtonStyle.success, row=1)
    async def btn_win(self, interaction, button):
        if not await is_admin(interaction): return
        view = ui.View()
        sel = ui.Select(placeholder="승리팀", options=[discord.SelectOption(label="1팀", value="1"), discord.SelectOption(label="2팀", value="2")])
        async def cb(i):
            for pid in last_match_teams[int(sel.values[0])]: supabase.rpc('increment_win', {'user_id': pid}).execute()
            await i.response.edit_message(content="✅ 기록 완료!", view=None)
        sel.callback = cb
        view.add_item(sel)
        await interaction.response.send_message("승리팀은?", view=view, ephemeral=True)

    @ui.button(label="📢 랭킹 보드 배포", style=discord.ButtonStyle.secondary, row=1)
    async def btn_ranking_deploy(self, interaction, button):
        if not await is_admin(interaction): return
        view = ui.View(timeout=None)
        btn = ui.Button(label="🏆 실시간 랭킹 확인", style=discord.ButtonStyle.success)
        async def r_cb(i):
            w_res = supabase.table("users").select("discord_name, win_count").order("win_count", desc=True).limit(10).execute()
            p_res = supabase.table("users").select("discord_name, participation_count").order("participation_count", desc=True).limit(10).execute()
            embed = discord.Embed(title="🏆 랭킹", color=0xFFD700)
            embed.add_field(name="승리", value="\n".join([f"{u['discord_name']}: {u['win_count']}승" for u in w_res.data]) or "없음")
            embed.add_field(name="참여", value="\n".join([f"{u['discord_name']}: {u['participation_count']}회" for u in p_res.data]) or "없음")
            await i.response.send_message(embed=embed, ephemeral=True)
        btn.callback = r_cb
        view.add_item(btn)
        await bot.get_channel(RANKING_CHANNEL_ID).send("📊 **실시간 랭킹 게시판**", view=view)
        await interaction.response.send_message("✅ 랭킹 채널에 배포 완료", ephemeral=True)

    @ui.button(label="⚙️ 운영진 관리", style=discord.ButtonStyle.secondary, row=1)
    async def btn_adm(self, interaction, button):
        if not interaction.user.guild_permissions.administrator: return
        await interaction.response.defer(ephemeral=True)
        view = ui.View()
        s1 = ui.Select(cls=ui.UserSelect, placeholder="임명", row=0)
        async def c1(i):
            supabase.table("users").upsert({"discord_id": s1.values[0].id, "discord_name": s1.values[0].display_name, "is_admin": True}).execute()
            await i.response.send_message("완료", ephemeral=True)
        s1.callback = c1; view.add_item(s1)
        await interaction.followup.send("운영진 관리:", view=view, ephemeral=True)

@bot.command(name="1")
async def master(ctx):
    await ctx.send("⚡ **통제실**", view=MasterDashboardView())

keep_alive()
bot.run(BOT_TOKEN)