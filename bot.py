import os
import sqlite3
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
REVIEW_CHANNEL_ID = int(os.getenv("REVIEW_CHANNEL_ID", "0"))
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))
MAJOR_ROLE_IDS = {int(x.strip()) for x in os.getenv("MAJOR_ROLE_IDS", "").split(",") if x.strip().isdigit()}
APPROVER_ROLE_IDS = {int(x.strip()) for x in os.getenv("APPROVER_ROLE_IDS", "").split(",") if x.strip().isdigit()}
ADMIN_ROLE_IDS = {int(x.strip()) for x in os.getenv("ADMIN_ROLE_IDS", "").split(",") if x.strip()}
DB_PATH = os.getenv("DB_PATH", "qth_warnings.sqlite3")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def setup_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            warrior_key TEXT NOT NULL,
            warrior_name TEXT NOT NULL,
            reason TEXT NOT NULL,
            description TEXT NOT NULL,
            requester_id INTEGER NOT NULL,
            requester_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            review_thread_id INTEGER,
            review_message_id INTEGER,
            decision_by INTEGER,
            decision_note TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_warnings_warrior ON warnings(guild_id, warrior_key, status)")

def normalize_name(name):
    return " ".join(name.strip().casefold().split())

def has_any_role(member, role_ids):
    return isinstance(member, discord.Member) and any(role.id in role_ids for role in member.roles)

def is_approver(member):
    return has_any_role(member, APPROVER_ROLE_IDS | ADMIN_ROLE_IDS)

def is_admin(member):
    return has_any_role(member, ADMIN_ROLE_IDS)

def approved_count(guild_id, warrior_key):
    with db() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM warnings WHERE guild_id=? AND warrior_key=? AND status='approved'",
            (guild_id, warrior_key)
        ).fetchone()
        return int(row["n"])

def make_embed(warning_id, warrior, reason, description, requester, count, status="Pendente"):
    color = discord.Color.gold() if status == "Pendente" else (discord.Color.green() if status == "Aprovada" else discord.Color.red())
    embed = discord.Embed(title=f"Advertência #{warning_id} • {warrior}", color=color)
    embed.add_field(name="Motivo", value=reason[:1024], inline=True)
    embed.add_field(name="Contador atual", value=f"{count}/3", inline=True)
    embed.add_field(name="Solicitante", value=f"{requester} (`{requester.id}`)", inline=False)
    embed.add_field(name="Descrição", value=description[:4000], inline=False)
    embed.add_field(name="Estado", value=status, inline=True)
    embed.set_footer(text="Evidências: envie imagens/arquivos nesta conversa de análise.")
    return embed

class WarningModal(discord.ui.Modal, title="Solicitar advertência"):
    warrior = discord.ui.TextInput(label="Nome do guerreiro", placeholder="Nome exato no QTH/jogo", max_length=100)
    reason = discord.ui.TextInput(label="Motivo", placeholder="Ex.: desrespeito, abandono de posto...", max_length=150, required=True)
    description = discord.ui.TextInput(label="Descrição da ocorrência", style=discord.TextStyle.paragraph, placeholder="Descreva o que aconteceu, com contexto e data se possível.", max_length=1800, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not has_any_role(interaction.user, MAJOR_ROLE_IDS | APPROVER_ROLE_IDS | ADMIN_ROLE_IDS):
            return await interaction.response.send_message("Você não tem cargo autorizado para solicitar advertências.", ephemeral=True)
        channel = interaction.guild.get_channel(REVIEW_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Canal de análise não configurado. Avise um administrador.", ephemeral=True)
        warrior = str(self.warrior).strip()
        key = normalize_name(warrior)
        created = datetime.now(timezone.utc).isoformat()
        with db() as con:
            cur = con.execute("""INSERT INTO warnings
                (guild_id, warrior_key, warrior_name, reason, description, requester_id, requester_name, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (interaction.guild.id, key, warrior, str(self.reason).strip(), str(self.description).strip(),
                 interaction.user.id, interaction.user.display_name, created))
            warning_id = cur.lastrowid
        count = approved_count(interaction.guild.id, key)
        embed = make_embed(warning_id, warrior, str(self.reason).strip(), str(self.description).strip(), interaction.user, count)
        view = DecisionView(warning_id)
        try:
            thread = await channel.create_thread(
                name=f"Advertência #{warning_id} - {warrior}"[:100],
                type=discord.ChannelType.public_thread,
                reason="Nova solicitação de advertência QTH Arsenal"
            )
            msg = await thread.send(embed=embed, view=view)
            with db() as con:
                con.execute("UPDATE warnings SET review_thread_id=?, review_message_id=? WHERE id=?",
                            (thread.id, msg.id, warning_id))
            await thread.send(
                f"{interaction.user.mention} **Anexe aqui as evidências** (prints/imagens/arquivos). "
                "O alto comando autorizado analisará este pedido. Não envie senhas ou dados pessoais."
            )
            await interaction.response.send_message(
                f"Solicitação **#{warning_id}** criada: {thread.mention}. Envie as evidências na conversa.",
                ephemeral=True
            )
        except discord.Forbidden:
            with db() as con:
                con.execute("UPDATE warnings SET status='rejected', decision_note='Falha de permissões do bot ao criar conversa.' WHERE id=?", (warning_id,))
            await interaction.response.send_message(
                "O bot não tem permissão para criar conversas no canal de análise. Peça ao administrador para revisar as permissões.",
                ephemeral=True
            )

class DecisionView(discord.ui.View):
    def __init__(self, warning_id):
        super().__init__(timeout=None)
        self.warning_id = warning_id
        self.approve_button.custom_id = f"qth:approve:{warning_id}"
        self.reject_button.custom_id = f"qth:reject:{warning_id}"

    @discord.ui.button(label="Aprovar", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.decide(interaction, "approved")

    @discord.ui.button(label="Reprovar", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.decide(interaction, "rejected")

    async def decide(self, interaction, status):
        if not interaction.guild or not is_approver(interaction.user):
            return await interaction.response.send_message("Apenas Coronéis/alto comando autorizados podem decidir.", ephemeral=True)
        with db() as con:
            row = con.execute("SELECT * FROM warnings WHERE id=? AND guild_id=?", (self.warning_id, interaction.guild.id)).fetchone()
            if not row:
                return await interaction.response.send_message("Solicitação não encontrada.", ephemeral=True)
            if row["status"] != "pending":
                return await interaction.response.send_message(f"Este pedido já foi decidido: {row['status']}.", ephemeral=True)
            con.execute("""UPDATE warnings SET status=?, decision_by=?, decided_at=?
                           WHERE id=? AND status='pending'""",
                        (status, interaction.user.id, datetime.now(timezone.utc).isoformat(), self.warning_id))
        count = approved_count(interaction.guild.id, row["warrior_key"])
        status_pt = "Aprovada" if status == "approved" else "Reprovada"
        old = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else make_embed(
            self.warning_id, row["warrior_name"], row["reason"], row["description"], interaction.user, count)
        old.color = discord.Color.green() if status == "approved" else discord.Color.red()
        old.set_field_at(1, name="Contador atual", value=f"{count}/3", inline=True)
        old.set_field_at(3, name="Estado", value=f"{status_pt} por {interaction.user.display_name}", inline=True)
        old.add_field(name="Decisão", value=f"Registrada por {interaction.user.mention}", inline=False)
        await interaction.response.edit_message(embed=old, view=None)
        if status == "approved" and count >= 3:
            alert_channel = interaction.guild.get_channel(ALERT_CHANNEL_ID)
            if isinstance(alert_channel, discord.TextChannel):
                await alert_channel.send(
                    f"🚨 **ALERTA 3/3 — QTH ARSENAL**\n"
                    f"Guerreiro: **{row['warrior_name']}**\n"
                    f"Advertências aprovadas: **{count}/3**\n"
                    f"Última advertência: #{self.warning_id}\n"
                    f"Responsável pela aprovação: {interaction.user.mention}\n"
                    "Analisar e registrar a punição conforme as regras do QTH."
                )
        await interaction.followup.send(f"Pedido #{self.warning_id} **{status_pt.lower()}**. Contador: {count}/3.", ephemeral=True)

@bot.tree.command(name="advertir", description="Abrir formulário para solicitar uma advertência.")
async def advertir(interaction: discord.Interaction):
    if not interaction.guild or not has_any_role(interaction.user, MAJOR_ROLE_IDS | APPROVER_ROLE_IDS | ADMIN_ROLE_IDS):
        return await interaction.response.send_message("Você não tem cargo autorizado.", ephemeral=True)
    await interaction.response.send_modal(WarningModal())

@bot.tree.command(name="historico", description="Consultar o histórico de advertências de um guerreiro.")
@app_commands.describe(guerreiro="Nome do guerreiro")
async def historico(interaction: discord.Interaction, guerreiro: str):
    if not interaction.guild or not has_any_role(interaction.user, MAJOR_ROLE_IDS | APPROVER_ROLE_IDS | ADMIN_ROLE_IDS):
        return await interaction.response.send_message("Você não tem cargo autorizado.", ephemeral=True)
    key = normalize_name(guerreiro)
    with db() as con:
        rows = con.execute("""SELECT id, warrior_name, reason, status, created_at, decision_note
                              FROM warnings WHERE guild_id=? AND warrior_key=? ORDER BY id DESC LIMIT 15""",
                           (interaction.guild.id, key)).fetchall()
    approved = sum(1 for r in rows if r["status"] == "approved")
    # Recompute total across all approved rows, not just the display limit.
    count = approved_count(interaction.guild.id, key)
    if not rows:
        return await interaction.response.send_message(f"Não encontrei registros para **{guerreiro}**.", ephemeral=True)
    lines = [f"**Histórico de {rows[0]['warrior_name']} — {count}/3 aprovadas**"]
    for r in rows:
        state = {"pending":"⏳ Pendente","approved":"✅ Aprovada","rejected":"❌ Reprovada"}.get(r["status"],r["status"])
        lines.append(f"`#{r['id']}` {state} — {r['reason']} — {r['created_at'][:10]}")
    if count >= 3:
        lines.append("🚨 **Atingiu 3/3 ou mais: analisar punição conforme as regras do QTH.**")
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

@bot.tree.command(name="painel", description="Mostrar os comandos do sistema de advertências.")
async def painel(interaction: discord.Interaction):
    embed = discord.Embed(title="QTH Arsenal • Disciplina", description="Use `/advertir` para abrir uma solicitação e `/historico` para consultar registros.", color=discord.Color.dark_gold())
    embed.add_field(name="Majores", value="`/advertir` — enviar ocorrência e depois anexar evidências na conversa criada.\n`/historico` — consultar contador.", inline=False)
    embed.add_field(name="Coronéis / alto comando", value="Analisar os pedidos no canal de análise e usar os botões Aprovar/Reprovar.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.event
async def on_ready():
    setup_db()
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild_obj)
            await bot.tree.sync(guild=guild_obj)
        else:
            await bot.tree.sync()
    except Exception as e:
        print("Falha ao sincronizar comandos:", repr(e))
    print(f"Bot conectado como {bot.user}.")

if not TOKEN:
    raise RuntimeError("Configure a variável de ambiente DISCORD_TOKEN.")
bot.run(TOKEN)
