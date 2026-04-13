import os
import discord
from discord.ext import commands
from discord import ui
from supabase import create_client, Client
from keep_alive import keep_alive # 가짜 웹서버 모듈 임포트

# --- [로컬 테스트용 .env 로드 (Render에서는 환경변수로 작동)] ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- [1. 환경변수 및 기본 설정] ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BOT_TOKEN = os.getenv("DISCORD_TOKEN")

if not all([SUPABASE_URL, SUPABASE_KEY, BOT_TOKEN]):
    raise ValueError("필수 환경변수가 누락되었습니다. Render 대시보드를 확인하세요.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# 데이터 매핑 상수
TIER_SCORE = {"아이언": 1, "브론즈": 2, "실버": 3, "골드": 4, "플래티넘": 5, "에메랄드": 6, "다이아몬드": 7, "마스터+": 9}
TIER_EMOJI = {"아이언": "⚪", "브론즈": "🟤", "실버": "⚪", "골드": "🟡", "플래티넘": "🟢", "에메랄드": "✳️", "다이아몬드": "💎", "마스터+": "🔮"}
LINE_OPTIONS = [
    discord.SelectOption(label="탑", value="TOP"), discord.SelectOption(label="정글", value="JUG"),
    discord.SelectOption(label="미드", value="MID"), discord.SelectOption(label="원딜", value="ADC"),
    discord.SelectOption(label="서포터", value="SUP")
]

# --- [권한 검증 로직] ---
async def is_admin(interaction: discord.Interaction):
    if interaction.user.guild_permissions.administrator: return True
    res = supabase.table("users").select("is_admin").eq("discord_id", interaction.user.id).execute()
    if res.data and res.data[0].get("is_admin") is True: return True
    await interaction.response.send_message("🚫 볼티: 클랜 운영진 권한이 없습니다.", ephemeral=True)
    return False

# --- [2. 등록 UI 컴포넌트] ---
class RegisterFlow(ui.View):
    def __init__(self, riot_id):
        super().__init__(timeout=180)
        self.riot_id = riot_id
        self.tier, self.main_line = None, None

    @ui.select(placeholder="1. 현재 티어를 선택하세요", options=[discord.SelectOption(label=k) for k in TIER_SCORE.keys()])
    async def select_tier(self, interaction, select):
        self.tier = select.values[0]
        await interaction.response.edit_message(content=f"✅ 티어: {self.tier}\n다음으로 **주라인**을 선택해주세요.", view=self)

    @ui.select(placeholder="2. 주라인(Main)을 선택하세요", options=LINE_OPTIONS)
    async def select_main(self, interaction, select):
        self.main_line = select.values[0]
        sub_opts = [opt for opt in LINE_OPTIONS if opt.value != self.main_line]
        sub_select = ui.Select(placeholder="3. 부라인(Sub)을 선택하세요", options=sub_opts, custom_id="sub_select")
        sub_select.callback = self.select_sub_callback
        self.add_item(sub_select)
        await interaction.response.edit_message(content=f"✅ 주라인: {self.main_line}\n마지막으로 **부라인**을 선택해주세요.", view=self)

    async def select_sub_callback(self, interaction):
        sub_line = interaction.data['values'][0]
        data = {
            "discord_id": interaction.user.id, "discord_name": interaction.user.display_name,
            "riot_id": self.riot_id, "tier": self.tier, "main_line": self.main_line, "sub_line": sub_line
        }
        supabase.table("users").upsert(data).execute()
        await interaction.response.edit_message(content=f"🎊 **{self.riot_id}**님, VOLT 클랜 등록 완료!", view=None)

class NicknameModal(ui.Modal, title='⚡ VOLT 멤버 정보 입력'):
    riot_id = ui.TextInput(label='라이엇 ID (닉네임#태그)', placeholder='EX) 설 화#0920', required=True)
    async def on_submit(self, interaction):
        await interaction.response.send_message("티어와 포지션을 선택해주세요.", view=RegisterFlow(self.riot_id.value), ephemeral=True)

# --- [3. 모집 및 드래프트 UI 컴포넌트] ---
class JoinView(ui.View):
    def __init__(self, target):
        super().__init__(timeout=None)
        self.target = target
        self.participants = []

    @ui.button(label="내전 참여 신청", style=discord.ButtonStyle.success)
    async def join_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user in self.participants:
            self.participants.remove(interaction.user)
            await interaction.response.send_message("참여를 취소했습니다.", ephemeral=True)
        else:
            if len(self.participants) >= self.target:
                return await interaction.response.send_message("이미 정원이 찼습니다!", ephemeral=True)
            self.participants.append(interaction.user)
            await interaction.response.send_message("신청 완료!", ephemeral=True)

        button.label = f"내전 참여 ({len(self.participants)}/{self.target})"
        names = "\n".join([f"- {m.display_name}" for m in self.participants])
        content = f"🎮 **VOLT {self.target}인 내전 모집**\n\n**현재 신청자:**\n{names if names else '없음'}"
        if len(self.participants) == self.target:
            content += f"\n\n🔥 **{self.target}명 모집 완료!** 드래프트를 준비하세요."
        await interaction.message.edit(content=content, view=self)

class DraftView(ui.View):
    def __init__(self, pool_data, l1, l2, admin_id, all_player_ids):
        super().__init__(timeout=None)
        self.pool = pool_data
        self.leaders = {1: l1, 2: l2}
        self.admin_id = admin_id
        self.all_player_ids = all_player_ids
        self.teams = {1: [], 2: []}
        self.team_scores = {1: 0, 2: 0}
        
        for idx, leader in enumerate([l1, l2], 1):
            l_data = supabase.table("users").select("*").eq("discord_id", leader.id).execute().data[0]
            score = TIER_SCORE.get(l_data['tier'], 3)
            p_count = l_data.get('participation_count', 0)
            self.team_scores[idx] += score
            self.teams[idx].append(f"👑 **{leader.display_name}** ({TIER_EMOJI.get(l_data['tier'])} {l_data['main_line']}) [참여:{p_count}]")

        self.order = [1, 2, 2, 1, 1, 2, 2, 1]
        self.idx = 0
        self.processing = False
        self.create_buttons()

    def create_buttons(self):
        self.clear_items()
        for d_id, d in self.pool.items():
            label = f"[{d['t_short']}] {d['n']} ({d['lines']}) [참여:{d['p_count']}]"
            btn = ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=str(d_id))
            btn.callback = self.pick_callback
            self.add_item(btn)
        
        stop_btn = ui.Button(label="강제 종료", style=discord.ButtonStyle.danger, row=4)
        stop_btn.callback = self.stop_callback
        self.add_item(stop_btn)

    async def pick_callback(self, interaction):
        if self.processing: return
        curr_leader = self.leaders[self.order[self.idx]]
        if interaction.user.id != curr_leader.id:
            return await interaction.response.send_message("본인 차례가 아닙니다!", ephemeral=True)

        self.processing = True
        target_id = int(interaction.data['custom_id'])
        p = self.pool.pop(target_id)
        
        t_num = self.order[self.idx]
        self.teams[t_num].append(f"· {p['n']} ({p['t_emoji']} {p['lines']}) [참여:{p['p_count']}]")
        self.team_scores[t_num] += p['score']
        self.idx += 1

        if not self.pool or self.idx >= len(self.order):
            if self.pool:
                lid, lp = self.pool.popitem()
                self.teams[1].append(f"· {lp['n']} ({lp['t_emoji']} {lp['lines']}) [참여:{lp['p_count']}]")
                self.team_scores[1] += lp['score']
            await self.finish_and_reward(interaction)
        else:
            self.create_buttons()
            self.processing = False
            await interaction.response.edit_message(content=f"🔵 **현재 픽:** {self.leaders[self.order[self.idx]].mention}", view=self)

    async def finish_and_reward(self, interaction):
        # 참여도 점수 적립 (주의: Supabase SQL에 increment_participation 함수가 생성되어 있어야 합니다)
        try:
            for p_id in self.all_player_ids:
                supabase.rpc('increment_participation', {'user_id': p_id}).execute()
        except Exception as e:
            print(f"참여도 적립 실패: {e}")

        diff = abs(self.team_scores[1] - self.team_scores[2])
        balance_msg = "✅ 균형 잡힌 팀" if diff <= 2 else "⚠️ 전력 차이 주의"

        embed = discord.Embed(title="⚔️ VOLT 최종 라인업 & 참여도 적립 완료", description=f"**{balance_msg}** (점수차: {diff}점)", color=0x5865F2)
        embed.add_field(name=f"🟦 1팀 ({self.team_scores[1]}점)", value="\n".join(self.teams[1]), inline=True)
        embed.add_field(name=f"🟥 2팀 ({self.team_scores[2]}점)", value="\n".join(self.teams[2]), inline=True)
        await interaction.response.edit_message(content="**드래프트 완료!**", embed=embed, view=None)

    async def stop_callback(self, interaction):
        if interaction.user.id != self.admin_id: return
        self.stop()
        await interaction.response.edit_message(content="🛑 드래프트가 강제 종료되었습니다.", view=None)

# --- [4. 대시보드 및 운영 UI 컴포넌트] ---
class RecruitSelectView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)
    @ui.select(placeholder="모집 규모 선택", options=[
        discord.SelectOption(label="10인 내전", value="10"),
        discord.SelectOption(label="20인 내전", value="20"),
        discord.SelectOption(label="30인 내전", value="30")
    ])
    async def select_count(self, interaction: discord.Interaction, select: ui.Select):
        target = int(select.values[0])
        await interaction.channel.send(f"🎮 **VOLT {target}인 챌린지 시작!**", view=JoinView(target))
        await interaction.response.edit_message(content=f"✅ {target}인 모집판 생성 완료.", view=None)

class LeaderSelectView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)
    @ui.select(cls=discord.ui.UserSelect, placeholder="주장 2명 선택", min_values=2, max_values=2)
    async def select_leaders(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        l1, l2 = select.values
        v_members = [m for m in interaction.user.voice.channel.members if not m.bot]
        
        res = supabase.table("users").select("*").in_("discord_id", [m.id for m in v_members]).execute()
        db_data = {r['discord_id']: r for r in res.data}
        unreg = [m.mention for m in v_members if m.id not in db_data]
        if unreg:
            return await interaction.response.send_message(f"🚫 미등록자 발견: {', '.join(unreg)}", ephemeral=True)

        pool = {}
        for m in v_members:
            if m.id not in [l1.id, l2.id]:
                d = db_data[m.id]
                pool[m.id] = {
                    "n": m.display_name, "t_short": d['tier'][0], "t_emoji": TIER_EMOJI.get(d['tier'], "⚪"),
                    "lines": f"{d['main_line']}/{d['sub_line']}", "score": TIER_SCORE.get(d['tier'], 3),
                    "p_count": d.get('participation_count', 0)
                }
        
        await interaction.channel.send(f"⚔️ **{l1.display_name} VS {l2.display_name} 드래프트!**", view=DraftView(pool, l1, l2, interaction.user.id, [m.id for m in v_members]))
        await interaction.response.edit_message(content="✅ 드래프트 패널 생성 완료.", view=None)

class AdminManageView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)
    @ui.select(cls=discord.ui.UserSelect, placeholder="운영진으로 임명할 멤버 선택", max_values=1)
    async def grant_admin(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        supabase.table("users").update({"is_admin": True}).eq("discord_id", select.values[0].id).execute()
        await interaction.response.edit_message(content=f"👑 **{select.values[0].display_name}**님에게 운영진 권한 부여 완료.", view=None)

class MasterDashboardView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="📝 플레이어 카드 배포", style=discord.ButtonStyle.primary, row=0)
    async def btn_reg(self, interaction: discord.Interaction, button: ui.Button):
        if not await is_admin(interaction): return
        reg_view = ui.View(timeout=None)
        reg_btn = ui.Button(label="클랜원 정보 등록/수정", style=discord.ButtonStyle.primary)
        
        async def open_modal(i: discord.Interaction):
            res = supabase.table("users").select("discord_id").eq("discord_id", i.user.id).execute()
            if res.data:
                confirm = ui.View(timeout=30)
                b = ui.Button(label="기존 정보 덮어쓰기", style=discord.ButtonStyle.danger)
                b.callback = lambda i2: i2.response.send_modal(NicknameModal())
                confirm.add_item(b)
                return await i.response.send_message("이미 등록된 정보가 있습니다.", view=confirm, ephemeral=True)
            await i.response.send_modal(NicknameModal())
            
        reg_btn.callback = open_modal
        reg_view.add_item(reg_btn)
        await interaction.channel.send("⚡ **VOLT 클랜 플레이어 등록 센터**", view=reg_view)
        await interaction.response.send_message("✅ 배포 완료", ephemeral=True)

    @ui.button(label="📢 내전 모집 열기", style=discord.ButtonStyle.success, row=0)
    async def btn_recruit(self, interaction: discord.Interaction, button: ui.Button):
        if not await is_admin(interaction): return
        await interaction.response.send_message("모집 규모를 선택하세요:", view=RecruitSelectView(), ephemeral=True)

    @ui.button(label="⚔️ 드래프트 시작", style=discord.ButtonStyle.danger, row=0)
    async def btn_draft(self, interaction: discord.Interaction, button: ui.Button):
        if not await is_admin(interaction): return
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message("⚠️ 음성 채널에 먼저 접속해 주세요.", ephemeral=True)
        v_members = [m for m in interaction.user.voice.channel.members if not m.bot]
        if len(v_members) != 10:
            return await interaction.response.send_message(f"⚠️ 정확히 10명이 필요합니다. (현재 {len(v_members)}명)", ephemeral=True)
        await interaction.response.send_message("주장 2명을 선택하세요:", view=LeaderSelectView(), ephemeral=True)

    @ui.button(label="🏆 랭킹 리더보드", style=discord.ButtonStyle.secondary, row=1)
    async def btn_ranking(self, interaction: discord.Interaction, button: ui.Button):
        win_res = supabase.table("users").select("discord_name, win_count").order("win_count", desc=True).limit(5).execute()
        part_res = supabase.table("users").select("discord_name, participation_count").order("participation_count", desc=True).limit(5).execute()
        
        embed = discord.Embed(title="🏆 VOLT 클랜 리더보드", color=0xFFD700)
        wins = "\n".join([f"{i+1}위: {u['discord_name']} ({u.get('win_count', 0)}승)" for i, u in enumerate(win_res.data)])
        parts = "\n".join([f"{i+1}위: {u['discord_name']} ({u.get('participation_count', 0)}회)" for i, u in enumerate(part_res.data)])
        
        embed.add_field(name="🥇 승리 랭킹", value=wins if wins else "데이터 없음", inline=True)
        embed.add_field(name="🔥 참여 랭킹", value=parts if parts else "데이터 없음", inline=True)
        await interaction.response.send_message(embed=embed)

    @ui.button(label="⚙️ 운영진 임명", style=discord.ButtonStyle.secondary, row=1)
    async def btn_admin(self, interaction: discord.Interaction, button: ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("🚫 디스코드 서버 관리자 전용입니다.", ephemeral=True)
        await interaction.response.send_message("운영진 설정 메뉴:", view=AdminManageView(), ephemeral=True)

# --- [5. 메인 명령어 및 실행] ---
@bot.event
async def on_member_update(before, after):
    if before.display_name != after.display_name:
        supabase.table("users").update({"discord_name": after.display_name}).eq("discord_id", after.id).execute()

@bot.command(name="1")
async def master_dashboard(ctx):
    if ctx.author.guild_permissions.administrator:
        is_authorized = True
    else:
        res = supabase.table("users").select("is_admin").eq("discord_id", ctx.author.id).execute()
        is_authorized = (res.data and res.data[0].get("is_admin") is True)

    if not is_authorized: return await ctx.send("🚫 운영진 전용 명령어입니다.")
    try: await ctx.message.delete()
    except: pass
    
    embed = discord.Embed(title="⚡ VOLT 마스터 대시보드", description="제어 버튼을 클릭하여 볼티 시스템을 관리하세요.", color=0x2ecc71)
    await ctx.send(embed=embed, view=MasterDashboardView())

# 가짜 웹서버 실행 후 디스코드 봇 가동
keep_alive()
bot.run(BOT_TOKEN)