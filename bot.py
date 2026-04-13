import os
import discord
from discord.ext import commands
from discord import ui
from supabase import create_client, Client
from keep_alive import keep_alive 

# --- [로컬 테스트용 .env 로드] ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- [1. 설정 및 환경변수] ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BOT_TOKEN = os.getenv("DISCORD_TOKEN")

# 📢 여기에 '내전 공지방' 채널 ID를 숫자로 입력하세요!
PUBLIC_CHANNEL_ID = 1493116057488199741

if not all([SUPABASE_URL, SUPABASE_KEY, BOT_TOKEN]):
    raise ValueError("필수 환경변수가 누락되었습니다. Render 대시보드를 확인하세요.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)

# 데이터 매핑 상수
TIER_SCORE = {"아이언": 1, "브론즈": 2, "실버": 3, "골드": 4, "플래티넘": 5, "에메랄드": 6, "다이아몬드": 7, "마스터+": 9}
TIER_EMOJI = {"아이언": "⚪", "브론즈": "🟤", "실버": "⚪", "골드": "🟡", "플래티넘": "🟢", "에메랄드": "✳️", "다이아몬드": "💎", "마스터+": "🔮"}
LINE_OPTIONS = [
    discord.SelectOption(label="탑", value="TOP"), discord.SelectOption(label="정글", value="JUG"),
    discord.SelectOption(label="미드", value="MID"), discord.SelectOption(label="원딜", value="ADC"),
    discord.SelectOption(label="서포터", value="SUP")
]

# 세션 메모리
last_match_teams = {1: [], 2: []}

# --- [권한 검증] ---
async def is_admin(interaction: discord.Interaction):
    if interaction.user.guild_permissions.administrator: return True
    res = supabase.table("users").select("is_admin").eq("discord_id", interaction.user.id).execute()
    if res.data and res.data[0].get("is_admin") is True: return True
    await interaction.response.send_message("🚫 볼티: 운영진 권한이 없습니다.", ephemeral=True)
    return False

# --- [2. 등록 & 관리자 전용 UI] ---
class AdminManageView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)
    @ui.select(cls=discord.ui.UserSelect, placeholder="👑 운영진 임명", max_values=1, row=0)
    async def grant_admin(self, interaction, select):
        supabase.table("users").update({"is_admin": True}).eq("discord_id", select.values[0].id).execute()
        await interaction.response.edit_message(content=f"✅ {select.values[0].display_name}님 운영진 임명.", view=None)
    @ui.select(cls=discord.ui.UserSelect, placeholder="🚫 운영진 박탈", max_values=1, row=1)
    async def revoke_admin(self, interaction, select):
        supabase.table("users").update({"is_admin": False}).eq("discord_id", select.values[0].id).execute()
        await interaction.response.edit_message(content=f"🛑 {select.values[0].display_name}님 권한 박탈.", view=None)

# --- [3. 모집 시스템 (원격 통제)] ---
class RecruitManageView(ui.View):
    def __init__(self, join_view):
        super().__init__(timeout=None)
        self.join_view = join_view 

    @ui.select(cls=discord.ui.UserSelect, placeholder="➕ 멤버 수동 추가", max_values=1, row=0)
    async def add_member(self, interaction, select):
        user = select.values[0]
        if user in self.join_view.participants: return await interaction.response.send_message("이미 있음", ephemeral=True)
        self.join_view.participants.append(user)
        await self.join_view.update_message()
        await interaction.response.send_message(f"✅ {user.display_name} 추가.", ephemeral=True)

    @ui.select(cls=discord.ui.UserSelect, placeholder="➖ 멤버 강제 퇴장", max_values=1, row=1)
    async def remove_member(self, interaction, select):
        user = select.values[0]
        if user not in self.join_view.participants: return await interaction.response.send_message("없음", ephemeral=True)
        self.join_view.participants.remove(user)
        await self.join_view.update_message()
        await interaction.response.send_message(f"🛑 {user.display_name} 제거.", ephemeral=True)

    @ui.button(label="💣 모집판 폭파", style=discord.ButtonStyle.danger, row=2)
    async def destroy(self, interaction, button):
        self.join_view.stop()
        await self.join_view.message.edit(content="💣 운영진에 의해 모집이 취소되었습니다.", view=None)
        await interaction.response.edit_message(content="✅ 폭파 완료.", view=None)

class JoinView(ui.View):
    def __init__(self, target):
        super().__init__(timeout=None)
        self.target = target
        self.participants = []
        self.message = None

    async def update_message(self):
        names = "\n".join([f"- {m.display_name}" for m in self.participants])
        content = f"🎮 **VOLT {self.target}인 내전 모집**\n\n**현재 신청자 ({len(self.participants)}/{self.target}):**\n{names if names else '없음'}"
        join_btn = [x for x in self.children if x.custom_id == "btn_join"][0]
        join_btn.label = f"내전 참여 ({len(self.participants)}/{self.target})"
        if self.message: await self.message.edit(content=content, view=self)

    @ui.button(label="내전 참여 신청", style=discord.ButtonStyle.success, custom_id="btn_join")
    async def join_btn(self, interaction, button):
        if not self.message: self.message = interaction.message
        if interaction.user in self.participants:
            self.participants.remove(interaction.user)
            await interaction.response.send_message("참여 취소", ephemeral=True)
        else:
            if len(self.participants) >= self.target: return await interaction.response.send_message("정원 초과", ephemeral=True)
            self.participants.append(interaction.user)
            await interaction.response.send_message("참여 완료", ephemeral=True)
        await self.update_message()

# --- [4. 드래프트 & 경기 관리] ---
class DraftView(ui.View):
    def __init__(self, pool_data, l1, l2, admin_id, all_player_ids):
        super().__init__(timeout=None)
        self.pool, self.leaders, self.admin_id, self.all_player_ids = pool_data, {1: l1, 2: l2}, admin_id, all_player_ids
        self.teams, self.team_ids, self.team_scores = {1: [], 2: []}, {1: [l1.id], 2: [l2.id]}, {1: 0, 2: 0}
        self.order, self.idx, self.processing = [1, 2, 2, 1, 1, 2, 2, 1], 0, False

        for idx, leader in enumerate([l1, l2], 1):
            l_data = supabase.table("users").select("*").eq("discord_id", leader.id).execute().data[0]
            self.team_scores[idx] += TIER_SCORE.get(l_data['tier'], 3)
            self.teams[idx].append(f"👑 **{leader.display_name}** ({TIER_EMOJI.get(l_data['tier'])} {l_data['main_line']})")
        self.create_buttons()

    def create_buttons(self):
        self.clear_items()
        for d_id, d in self.pool.items():
            btn = ui.Button(label=f"[{d['t_short']}] {d['n']} ({d['lines']})", custom_id=str(d_id))
            btn.callback = self.pick_callback
            self.add_item(btn)

    async def pick_callback(self, interaction):
        if self.processing: return
        if interaction.user.id != self.leaders[self.order[self.idx]].id:
            return await interaction.response.send_message("당신 차례가 아님", ephemeral=True)
        
        self.processing = True
        p_id = int(interaction.data['custom_id'])
        p = self.pool.pop(p_id)
        t_num = self.order[self.idx]
        self.teams[t_num].append(f"· {p['n']} ({p['t_emoji']} {p['lines']})")
        self.team_ids[t_num].append(p_id)
        self.team_scores[t_num] += p['score']
        self.idx += 1

        if not self.pool or self.idx >= len(self.order):
            if self.pool: # 막픽 자동배정
                lid, lp = self.pool.popitem()
                self.teams[1].append(f"· {lp['n']} ({lp['t_emoji']} {lp['lines']})")
                self.team_ids[1].append(lid); self.team_scores[1] += lp['score']
            await self.finish(interaction)
        else:
            self.create_buttons(); self.processing = False
            await interaction.response.edit_message(content=f"🔵 **현재 픽:** {self.leaders[self.order[self.idx]].mention}", view=self)

    async def finish(self, interaction):
        global last_match_teams
        for p_id in self.all_player_ids: supabase.rpc('increment_participation', {'user_id': p_id}).execute()
        last_match_teams[1], last_match_teams[2] = self.team_ids[1], self.team_ids[2]
        
        diff = abs(self.team_scores[1] - self.team_scores[2])
        embed = discord.Embed(title="⚔️ VOLT 최종 라인업", description=f"점수차: {diff}점", color=0x5865F2)
        embed.add_field(name=f"🟦 1팀 ({self.team_scores[1]}점)", value="\n".join(self.teams[1]))
        embed.add_field(name=f"🟥 2팀 ({self.team_scores[2]}점)", value="\n".join(self.teams[2]))
        
        # 공지 채널로 원격 전송
        public_channel = bot.get_channel(PUBLIC_CHANNEL_ID)
        await public_channel.send(embed=embed)
        await interaction.response.edit_message(content="✅ 드래프트 완료! 결과가 공지방에 전송되었습니다.", view=None)

# --- [5. 마스터 대시보드] ---
class MasterDashboardView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="📝 플레이어 카드 배포", style=discord.ButtonStyle.primary, row=0)
    async def btn_reg(self, interaction, button):
        if not await is_admin(interaction): return
        reg_view = ui.View(timeout=None)
        reg_btn = ui.Button(label="클랜원 등록/수정", style=discord.ButtonStyle.primary)
        async def reg_callback(i):
            modal = ui.Modal(title="VOLT 멤버 등록")
            rid = ui.TextInput(label="라이엇 ID", placeholder="닉네임#태그")
            modal.add_item(rid)
            async def on_submit(i2):
                await i2.response.send_message("티어를 선택하세요.", ephemeral=True) # 생략된 등록 흐름 연결
            modal.on_submit = on_submit
            await i.response.send_modal(modal)
        reg_btn.callback = reg_callback
        reg_view.add_item(reg_btn)
        public_channel = bot.get_channel(PUBLIC_CHANNEL_ID)
        await public_channel.send("⚡ **VOLT 클랜 플레이어 등록 센터**", view=reg_view)
        await interaction.response.send_message("✅ 배포 완료", ephemeral=True)

    @ui.button(label="📢 내전 모집 열기", style=discord.ButtonStyle.success, row=0)
    async def btn_recruit(self, interaction, button):
        if not await is_admin(interaction): return
        view = ui.View()
        select = ui.Select(placeholder="인원 선택", options=[discord.SelectOption(label=f"{x}인", value=str(x)) for x in [10, 20, 30]])
        async def sel_callback(i):
            target = int(select.values[0])
            join_view = JoinView(target)
            public_channel = bot.get_channel(PUBLIC_CHANNEL_ID)
            msg = await public_channel.send(f"🎮 **VOLT {target}인 내전 모집!**", view=join_view)
            join_view.message = msg
            await i.response.edit_message(content=f"✅ {target}인 모집 리모컨입니다.", view=RecruitManageView(join_view))
        select.callback = sel_callback
        view.add_item(select)
        await interaction.response.send_message("규모를 선택하세요:", view=view, ephemeral=True)

    @ui.button(label="⚔️ 드래프트 시작", style=discord.ButtonStyle.danger, row=0)
    async def btn_draft(self, interaction, button):
        if not await is_admin(interaction): return
        if not interaction.user.voice: return await interaction.response.send_message("음성채널 필수", ephemeral=True)
        v_members = [m for m in interaction.user.voice.channel.members if not m.bot and not (m.voice.self_mute or m.voice.mute)]
        if len(v_members) != 10: return await interaction.response.send_message(f"마이크 켠 10명 필요(현재 {len(v_members)}명)", ephemeral=True)
        
        view = ui.View()
        sel = ui.Select(cls=ui.UserSelect, placeholder="주장 2명 선택", min_values=2, max_values=2)
        async def sel_cb(i):
            l1, l2 = sel.values
            res = supabase.table("users").select("*").in_("discord_id", [m.id for m in v_members]).execute()
            db_data = {r['discord_id']: r for r in res.data}
            pool = {m.id: {"n": m.display_name, "t_short": db_data[m.id]['tier'][0], "t_emoji": TIER_EMOJI.get(db_data[m.id]['tier'], "⚪"),
                          "lines": f"{db_data[m.id]['main_line']}/{db_data[m.id]['sub_line']}", "score": TIER_SCORE.get(db_data[m.id]['tier'], 3),
                          "p_count": db_data[m.id].get('participation_count', 0)} for m in v_members if m.id not in [l1.id, l2.id]}
            await i.channel.send(f"⚔️ **드래프트 시작!**", view=DraftView(pool, l1, l2, i.user.id, [m.id for m in v_members]))
            await i.response.edit_message(content="✅ 생성됨", view=None)
        sel.callback = sel_cb
        view.add_item(sel)
        await interaction.response.send_message("주장을 선택하세요:", view=view, ephemeral=True)

    @ui.button(label="🏅 결과 기록 (승점)", style=discord.ButtonStyle.success, row=1)
    async def btn_win(self, interaction, button):
        if not await is_admin(interaction): return
        if not last_match_teams[1]: return await interaction.response.send_message("기록 없음", ephemeral=True)
        view = ui.View()
        sel = ui.Select(placeholder="승리팀 선택", options=[discord.SelectOption(label="1팀", value="1"), discord.SelectOption(label="2팀", value="2")])
        async def sel_cb(i):
            for pid in last_match_teams[int(sel.values[0])]: supabase.rpc('increment_win', {'user_id': pid}).execute()
            await i.response.edit_message(content=f"✅ {sel.values[0]}팀 승리 기록완료!", view=None)
        sel.callback = sel_cb
        view.add_item(sel)
        await interaction.response.send_message("승리팀은?", view=view, ephemeral=True)

@bot.command(name="1")
async def master(ctx):
    if not ctx.author.guild_permissions.administrator:
        res = supabase.table("users").select("is_admin").eq("discord_id", ctx.author.id).execute()
        if not (res.data and res.data[0]['is_admin']): return
    await ctx.send("⚡ **VOLT 마스터 통제실**", view=MasterDashboardView())

keep_alive()
bot.run(BOT_TOKEN)