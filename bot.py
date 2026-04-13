import os
import discord
from discord.ext import commands
from discord import ui
from supabase import create_client, Client

# --- [1. 환경변수 설정] ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BOT_TOKEN = os.getenv("DISCORD_TOKEN")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

TIER_SCORE = {"아이언": 1, "브론즈": 2, "실버": 3, "골드": 4, "플래티넘": 5, "에메랄드": 6, "다이아몬드": 7, "마스터+": 9}
TIER_EMOJI = {"아이언": "⚪", "브론즈": "🟤", "실버": "⚪", "골드": "🟡", "플래티넘": "🟢", "에메랄드": "✳️", "다이아몬드": "💎", "마스터+": "🔮"}

# --- [권한 체크 함수] ---
async def is_admin(interaction: discord.Interaction):
    # 디스코드 자체 관리자 권한이 있으면 무조건 통과 (최고 관리자)
    if interaction.user.guild_permissions.administrator: return True
    # DB에 is_admin이 true로 설정되어 있는지 확인
    res = supabase.table("users").select("is_admin").eq("discord_id", interaction.user.id).execute()
    if res.data and res.data[0].get("is_admin") is True: return True
    
    await interaction.response.send_message("🚫 볼티: 해당 기능을 사용할 수 있는 운영진 권한이 없습니다.", ephemeral=True)
    return False

# --- [2. 서브 UI 메뉴들 (모집, 주장 선택, 운영진 설정)] ---

# 인원 선택 드롭다운 (10/20/30)
class RecruitSelectView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)
    
    @ui.select(placeholder="몇 명 규모의 내전을 모집할까요?", options=[
        discord.SelectOption(label="10인 내전 (1팀 vs 2팀)", value="10", emoji="🎮"),
        discord.SelectOption(label="20인 내전 (대규모)", value="20", emoji="🔥"),
        discord.SelectOption(label="30인 내전 (이벤트)", value="30", emoji="🏆")
    ])
    async def select_count(self, interaction: discord.Interaction, select: ui.Select):
        target = int(select.values[0])
        # JoinView는 이전 코드와 동일하게 10/20/30 모집판을 띄우는 역할
        await interaction.channel.send(f"🎮 **VOLT {target}인 내전 신청 시작!**", view=JoinView(target))
        await interaction.response.edit_message(content=f"✅ {target}인 모집판을 채널에 생성했습니다.", view=None)

# 드래프트 주장 선택 (디스코드 유저 선택 메뉴 활용)
class LeaderSelectView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @ui.select(cls=discord.ui.UserSelect, placeholder="드래프트를 진행할 주장 2명을 선택하세요", min_values=2, max_values=2)
    async def select_leaders(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        l1, l2 = select.values
        v_members = [m for m in interaction.user.voice.channel.members if not m.bot]
        
        # 이전 코드의 드래프트 시작 로직 통합
        res = supabase.table("users").select("*").in_("discord_id", [m.id for m in v_members]).execute()
        db_data = {r['discord_id']: r for r in res.data}
        unreg = [m.mention for m in v_members if m.id not in db_data]
        
        if unreg:
            return await interaction.response.send_message(f"🚫 미등록자가 있습니다: {', '.join(unreg)}", ephemeral=True)

        pool = {}
        all_ids = [m.id for m in v_members]
        for m in v_members:
            if m.id not in [l1.id, l2.id]:
                d = db_data[m.id]
                pool[m.id] = {
                    "n": m.display_name, "t_short": d['tier'][0], "t_emoji": TIER_EMOJI.get(d['tier'], "⚪"),
                    "lines": f"{d['main_line']}/{d['sub_line']}", "score": TIER_SCORE.get(d['tier'], 3),
                    "p_count": d.get('participation_count', 0)
                }
        
        await interaction.channel.send(f"⚔️ **{l1.display_name} VS {l2.display_name} 드래프트 시작!**", view=DraftView(pool, l1, l2, interaction.user.id, all_ids))
        await interaction.response.edit_message(content="✅ 드래프트 패널을 생성했습니다.", view=None)

# 운영진 권한 부여/박탈 메뉴
class AdminManageView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @ui.select(cls=discord.ui.UserSelect, placeholder="운영진으로 임명할 멤버를 선택하세요", max_values=1)
    async def grant_admin(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        target_user = select.values[0]
        supabase.table("users").update({"is_admin": True}).eq("discord_id", target_user.id).execute()
        await interaction.response.edit_message(content=f"👑 **{target_user.display_name}**님에게 운영진 권한을 부여했습니다.", view=None)

    @ui.button(label="운영진 권한 회수 (박탈)", style=discord.ButtonStyle.danger, row=1)
    async def revoke_admin_btn(self, interaction: discord.Interaction, button: ui.Button):
        # 권한 회수용 유저 선택 메뉴 띄우기
        revoke_view = ui.View(timeout=60)
        select = discord.ui.UserSelect(placeholder="권한을 박탈할 운영진을 선택하세요", max_values=1)
        async def revoke_callback(i: discord.Interaction):
            t_user = select.values[0]
            supabase.table("users").update({"is_admin": False}).eq("discord_id", t_user.id).execute()
            await i.response.edit_message(content=f"🚫 **{t_user.display_name}**님의 운영진 권한을 회수했습니다.", view=None)
        select.callback = revoke_callback
        revoke_view.add_item(select)
        await interaction.response.send_message("회수 대상을 선택하세요:", view=revoke_view, ephemeral=True)

# --- [3. 메인 대시보드 뷰 (The Master Panel)] ---
class MasterDashboardView(ui.View):
    def __init__(self):
        super().__init__(timeout=None) # 대시보드는 시간제한 없음

    @ui.button(label="📝 플레이어 카드 배포", style=discord.ButtonStyle.primary, custom_id="dash_reg", row=0)
    async def btn_reg(self, interaction: discord.Interaction, button: ui.Button):
        if not await is_admin(interaction): return
        
        # 등록 버튼을 생성하여 채널에 전송 (이전 post_reg 로직)
        reg_view = ui.View(timeout=None)
        reg_btn = ui.Button(label="클랜원 정보 등록/수정", style=discord.ButtonStyle.primary, custom_id="btn_register_modal")
        reg_btn.callback = self.open_reg_modal # (아래 모달 오픈 함수 연결)
        reg_view.add_item(reg_btn)
        
        await interaction.channel.send("⚡ **VOLT 클랜 플레이어 카드 등록 센터**", view=reg_view)
        await interaction.response.send_message("✅ 등록 패널을 채널에 게시했습니다.", ephemeral=True)

    @ui.button(label="📢 내전 모집 열기", style=discord.ButtonStyle.success, custom_id="dash_recruit", row=0)
    async def btn_recruit(self, interaction: discord.Interaction, button: ui.Button):
        if not await is_admin(interaction): return
        await interaction.response.send_message("모집 규모를 선택하세요:", view=RecruitSelectView(), ephemeral=True)

    @ui.button(label="⚔️ 드래프트 시작", style=discord.ButtonStyle.danger, custom_id="dash_draft", row=0)
    async def btn_draft(self, interaction: discord.Interaction, button: ui.Button):
        if not await is_admin(interaction): return
        
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message("⚠️ 음성 채널에 먼저 접속해 주세요.", ephemeral=True)
            
        v_members = [m for m in interaction.user.voice.channel.members if not m.bot]
        if len(v_members) != 10:
            return await interaction.response.send_message(f"⚠️ 음성 채널에 정확히 10명이 있어야 합니다. (현재 {len(v_members)}명)", ephemeral=True)

        await interaction.response.send_message("드래프트를 진행할 주장 2명을 선택하세요:", view=LeaderSelectView(), ephemeral=True)

    @ui.button(label="🏆 랭킹 및 명예의 전당", style=discord.ButtonStyle.secondary, custom_id="dash_ranking", row=1)
    async def btn_ranking(self, interaction: discord.Interaction, button: ui.Button):
        # 랭킹은 누구나 볼 수 있도록 설정 (is_admin 체크 안 함)
        # 이전 랭킹 출력 로직 실행
        win_res = supabase.table("users").select("discord_name, win_count").order("win_count", desc=True).limit(5).execute()
        embed = discord.Embed(title="🏆 VOLT 클랜 리더보드", color=0xFFD700)
        wins = "\n".join([f"{i+1}위: {u['discord_name']} ({u.get('win_count', 0)}승)" for i, u in enumerate(win_res.data)])
        embed.add_field(name="🥇 최다 승리", value=wins if wins else "데이터 없음", inline=True)
        await interaction.response.send_message(embed=embed)

    @ui.button(label="⚙️ 운영진 관리", style=discord.ButtonStyle.secondary, custom_id="dash_admin", row=1)
    async def btn_admin(self, interaction: discord.Interaction, button: ui.Button):
        # 운영진 관리는 오직 '디스코드 서버 최고 관리자'만 가능하도록 이중 보안
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("🚫 디스코드 서버 관리자 권한이 있는 사람만 운영진을 임명할 수 있습니다.", ephemeral=True)
        
        await interaction.response.send_message("운영진 설정 메뉴입니다:", view=AdminManageView(), ephemeral=True)

    # (내부 등록 모달 오픈 콜백)
    async def open_reg_modal(self, interaction: discord.Interaction):
        # 기존 등록/수정 모달 오픈 로직
        res = supabase.table("users").select("discord_id").eq("discord_id", interaction.user.id).execute()
        if res.data:
            confirm = ui.View(timeout=30)
            b = ui.Button(label="정보 수정하기", style=discord.ButtonStyle.danger)
            b.callback = lambda i: i.response.send_modal(NicknameModal())
            confirm.add_item(b)
            return await interaction.response.send_message("이미 등록된 정보가 있습니다. 덮어씌우시겠습니까?", view=confirm, ephemeral=True)
        await interaction.response.send_modal(NicknameModal())

# --- [4. 단 하나의 마스터 명령어] ---
@bot.command(name="1")
async def master_dashboard(ctx):
    # !1을 쳤을 때, 서버 관리자거나 DB에 등록된 운영진인지 확인 후 대시보드 출력
    if ctx.author.guild_permissions.administrator:
        is_authorized = True
    else:
        res = supabase.table("users").select("is_admin").eq("discord_id", ctx.author.id).execute()
        is_authorized = (res.data and res.data[0].get("is_admin") is True)

    if not is_authorized:
        return await ctx.send("🚫 볼티: 이 명령어는 클랜 운영진만 사용할 수 있습니다.")

    embed = discord.Embed(
        title="⚡ VOLT 마스터 대시보드",
        description="원하는 제어 버튼을 클릭하여 볼티 시스템을 관리하세요.\n모든 기능은 현재 채널 혹은 접속 중인 음성 채널을 기준으로 작동합니다.",
        color=0x2ecc71
    )
    embed.set_footer(text="버튼 클릭 시 해당 기능의 세부 설정 창이 열립니다.")
    
    # 보안상 이전 메시지를 지우고 대시보드만 남기는 것이 깔끔함
    try: await ctx.message.delete() 
    except: pass
    
    await ctx.send(embed=embed, view=MasterDashboardView())

# (DraftView, JoinView, RegisterFlow, NicknameModal 코드는 이전과 동일하게 유지되어 합쳐집니다.)

bot.run(BOT_TOKEN)