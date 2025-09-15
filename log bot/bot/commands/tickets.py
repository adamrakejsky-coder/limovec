from discord.ext import commands
import discord
from typing import Optional
from ..tickets.manager import TicketManager
from ..tickets.database import TicketDatabase
from ..tickets.views import PersistentTicketView
import json
import asyncio

class TicketCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ticket_manager: TicketManager = None
        self.ticket_db: TicketDatabase = None
    
    async def cog_load(self):
        """Inicializace po načtení cog"""
        if hasattr(self.bot, 'ticket_manager'):
            self.ticket_manager = self.bot.ticket_manager
            self.ticket_db = self.ticket_manager.ticket_db
    
    def has_ticket_admin_role(self, ctx):
        """Vylepšená permission check"""
        if ctx.author.guild_permissions.administrator:
            return True
        
        # Async check se musí dělat v command
        return False
    
    async def async_has_admin_role(self, ctx) -> bool:
        """Async permission check"""
        if ctx.author.guild_permissions.administrator:
            return True
        
        if not self.ticket_db:
            return False
        
        settings = await self.ticket_db.get_settings(ctx.guild.id)
        admin_role_ids = settings.get("admin_role_ids", [])
        
        return any(role.id in admin_role_ids for role in ctx.author.roles)
    
    @commands.group(invoke_without_command=True)
    async def ticket(self, ctx):
        """Hlavní ticket skupina příkazů"""
        await ctx.send_help(ctx.command)
    
    @ticket.command(name="setup")
    async def ticket_setup(self, ctx):
        """Interaktivní setup ticket systému"""
        if not await self.async_has_admin_role(ctx):
            return await ctx.send("❌ Nemáš oprávnění.")
        
        embed = discord.Embed(
            title="🎫 Ticket systém - Setup",
            description="Nastavím ticket systém krok za krokem.",
            color=discord.Color.blue()
        )
        await ctx.send(embed=embed)
        
        # Setup wizard...
        await ctx.send("Prosím, zmíň **moderátorskou roli** pro tickety:")
        
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel
        
        try:
            msg = await self.bot.wait_for('message', check=check, timeout=60.0)
            
            # Parse role mention
            if msg.role_mentions:
                mod_role = msg.role_mentions[0]
                settings = await self.ticket_db.get_settings(ctx.guild.id)
                settings["mod_role_id"] = mod_role.id
                await self.ticket_db.save_settings(ctx.guild.id, settings)
                await ctx.send(f"✅ Moderátorská role nastavena: {mod_role.mention}")
            else:
                await ctx.send("❌ Žádná role nebyla zmíněna.")
                
        except asyncio.TimeoutError:
            await ctx.send("⏰ Setup zrušen (timeout).")
    
    @ticket.command(name="admin_role")
    async def admin_role(self, ctx, role: discord.Role):
        """Přidá admin roli pro ticket management"""
        if not ctx.author.guild_permissions.administrator:
            return await ctx.send("❌ Nemáš oprávnění.")
        
        settings = await self.ticket_db.get_settings(ctx.guild.id)
        admin_roles = settings.get("admin_role_ids", [])
        
        if role.id not in admin_roles:
            admin_roles.append(role.id)
            settings["admin_role_ids"] = admin_roles
            await self.ticket_db.save_settings(ctx.guild.id, settings)
            await ctx.send(f"✅ Role `{role.name}` nyní může spravovat nastavení ticket bota.")
        else:
            await ctx.send(f"⚠️ Role `{role.name}` už má admin oprávnění.")
    
    @ticket.command(name="remove_admin_role")
    async def remove_admin_role(self, ctx, role: discord.Role):
        """Odstraní admin roli"""
        if not ctx.author.guild_permissions.administrator:
            return await ctx.send("❌ Nemáš oprávnění.")
        
        settings = await self.ticket_db.get_settings(ctx.guild.id)
        admin_roles = settings.get("admin_role_ids", [])
        
        if role.id in admin_roles:
            admin_roles.remove(role.id)
            settings["admin_role_ids"] = admin_roles
            await self.ticket_db.save_settings(ctx.guild.id, settings)
            await ctx.send(f"✅ Role `{role.name}` už nemá admin oprávnění.")
        else:
            await ctx.send(f"⚠️ Role `{role.name}` nemá admin oprávnění.")

    @ticket.command(name="mod_role")
    async def mod_role(self, ctx, role: discord.Role):
        """Nastaví moderátorskou roli"""
        if not await self.async_has_admin_role(ctx):
            return await ctx.send("❌ Nemáš oprávnění.")
        
        settings = await self.ticket_db.get_settings(ctx.guild.id)
        settings["mod_role_id"] = role.id
        await self.ticket_db.save_settings(ctx.guild.id, settings)
        await ctx.send(f"✅ Mod role nastavena na: {role.name}")
    
    @ticket.command(name="transcript")
    async def transcript_channel(self, ctx, channel: discord.TextChannel):
        """Nastaví kanál pro transcripty"""
        if not await self.async_has_admin_role(ctx):
            return await ctx.send("❌ Nemáš oprávnění.")
        
        settings = await self.ticket_db.get_settings(ctx.guild.id)
        settings["transcript_channel_id"] = channel.id
        await self.ticket_db.save_settings(ctx.guild.id, settings)
        await ctx.send(f"✅ Transcript kanál nastaven na: {channel.mention}")
    
    @ticket.command(name="add_button")
    async def add_button(self, ctx, label: str, *, welcome_message: str):
        """Přidá custom tlačítko"""
        if not await self.async_has_admin_role(ctx):
            return await ctx.send("❌ Nemáš oprávnění.")
        
        settings = await self.ticket_db.get_settings(ctx.guild.id)
        buttons = settings.get("custom_buttons", [])
        
        if len(buttons) >= 25:
            return await ctx.send("❌ Maximum 25 tlačítek.")
        
        # Zkontroluj duplicitní názvy
        for existing_label, _ in buttons:
            if existing_label.lower() == label.lower():
                return await ctx.send(f"❌ Tlačítko s názvem **{label}** už existuje!")
        
        buttons.append([label[:80], welcome_message])
        settings["custom_buttons"] = buttons
        await self.ticket_db.save_settings(ctx.guild.id, settings)
        
        await ctx.send(f"✅ Přidán custom button: **{label}** s uvítací zprávou.")
    
    @ticket.command(name="remove_button")
    async def remove_button(self, ctx, *, label: str):
        """Odstraní tlačítko podle názvu"""
        if not await self.async_has_admin_role(ctx):
            return await ctx.send("❌ Nemáš oprávnění.")
        
        settings = await self.ticket_db.get_settings(ctx.guild.id)
        buttons = settings.get("custom_buttons", [])
        
        original_count = len(buttons)
        buttons = [btn for btn in buttons if btn[0] != label]
        
        if len(buttons) < original_count:
            settings["custom_buttons"] = buttons
            await self.ticket_db.save_settings(ctx.guild.id, settings)
            await ctx.send(f"✅ Tlačítko **{label}** odstraněno.")
        else:
            await ctx.send(f"❌ Tlačítko **{label}** nenalezeno.")
    
    @ticket.command(name="clear_buttons")
    async def clear_buttons(self, ctx):
        """Smaže všechna tlačítka"""
        if not await self.async_has_admin_role(ctx):
            return await ctx.send("❌ Nemáš oprávnění.")
        
        settings = await self.ticket_db.get_settings(ctx.guild.id)
        settings["custom_buttons"] = []
        await self.ticket_db.save_settings(ctx.guild.id, settings)
        await ctx.send("✅ Všechna tlačítka byla odstraněna.")
    
    @ticket.command(name="panel")
    async def create_panel(self, ctx, *, message: Optional[str] = None):
        """Vytvoří ticket panel"""
        if not await self.async_has_admin_role(ctx):
            return await ctx.send("❌ Nemáš oprávnění.")
        
        settings = await self.ticket_db.get_settings(ctx.guild.id)
        
        if not settings.get("mod_role_id"):
            return await ctx.send("❌ Nastav nejprve mod roli (`!ticket mod_role`).")
        
        if not settings.get("custom_buttons"):
            return await ctx.send("❌ Přidej alespoň jedno tlačítko (`!ticket add_button`).")
        
        if message:
            settings["panel_message"] = message
            await self.ticket_db.save_settings(ctx.guild.id, settings)
        
        embed = discord.Embed(
            title="🎫 Ticket systém",
            description=settings.get("panel_message", "Kliknutím na tlačítko níže vytvoříš ticket:"),
            color=discord.Color(settings.get("embed_color", 5793266))
        )
        
        view = PersistentTicketView(ctx.guild.id, settings)
        await ctx.send(embed=embed, view=view)
    
    @ticket.command(name="settings")
    async def show_settings(self, ctx):
        """Zobrazí aktuální nastavení"""
        if not await self.async_has_admin_role(ctx):
            return await ctx.send("❌ Nemáš oprávnění.")
        
        settings = await self.ticket_db.get_settings(ctx.guild.id)
        
        embed = discord.Embed(
            title="⚙️ Ticket nastavení",
            color=discord.Color.blue()
        )
        
        mod_role = ctx.guild.get_role(settings.get("mod_role_id"))
        embed.add_field(
            name="Moderátorská role",
            value=mod_role.mention if mod_role else "❌ Nenastaveno",
            inline=True
        )
        
        admin_roles = []
        for role_id in settings.get("admin_role_ids", []):
            role = ctx.guild.get_role(role_id)
            if role:
                admin_roles.append(role.mention)
        
        embed.add_field(
            name="Admin role",
            value=", ".join(admin_roles) if admin_roles else "❌ Žádné",
            inline=True
        )
        
        transcript_channel = ctx.guild.get_channel(settings.get("transcript_channel_id"))
        embed.add_field(
            name="Transcript kanál",
            value=transcript_channel.mention if transcript_channel else "❌ Nenastaveno",
            inline=True
        )
        
        buttons_count = len(settings.get("custom_buttons", []))
        embed.add_field(
            name="Počet tlačítek",
            value=str(buttons_count),
            inline=True
        )
        
        ui_type = "Dropdown menu" if settings.get("use_menu") else "Tlačítka"
        embed.add_field(
            name="Typ UI",
            value=ui_type,
            inline=True
        )
        
        await ctx.send(embed=embed)
    
    @ticket.command(name="ui")
    async def panel_ui(self, ctx, mode: str):
        """Přepne mezi tlačítky a dropdown menu"""
        if not await self.async_has_admin_role(ctx):
            return await ctx.send("❌ Nemáš oprávnění.")
        
        mode = mode.lower()
        if mode not in ["menu", "button", "dropdown"]:
            return await ctx.send("❌ Použij: `menu`, `dropdown` nebo `button`")
        
        settings = await self.ticket_db.get_settings(ctx.guild.id)
        settings["use_menu"] = mode in ["menu", "dropdown"]
        await self.ticket_db.save_settings(ctx.guild.id, settings)
        
        ui_text = "dropdown menu" if settings["use_menu"] else "tlačítka"
        await ctx.send(f"✅ Panel bude nyní používat **{ui_text}**.")
    
    @ticket.command(name="close")
    async def close_ticket(self, ctx, *, reason: Optional[str] = None):
        """Zavře aktuální ticket"""
        if not ctx.channel.name.startswith("ticket-"):
            return await ctx.send("❌ Tento příkaz funguje pouze v ticket kanálech.")
        
        # Najdi typ ticketu z názvu kanálu
        ticket_type = "general"
        if "-" in ctx.channel.name:
            parts = ctx.channel.name.split("-")
            if len(parts) >= 3:
                ticket_type = parts[-1]
        
        try:
            await self.ticket_manager.close_ticket(
                ctx.channel, 
                ctx.author, 
                ticket_type,
                reason
            )
        except Exception as e:
            await ctx.send(f"❌ Chyba při zavírání ticketu: {e}")
    
    @commands.command()
    async def ticket_help(self, ctx):
        """Zobrazí nápovědu pro ticket systém"""
        embed = discord.Embed(
            title="🎫 Ticket systém - Nápověda",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="Základní nastavení",
            value="""
`!ticket setup` - Interaktivní nastavení
`!ticket admin_role @role` - Přidá admin roli
`!ticket mod_role @role` - Nastaví mod roli
`!ticket transcript #kanál` - Kanál pro transcripty
            """,
            inline=False
        )
        
        embed.add_field(
            name="Správa tlačítek",
            value="""
`!ticket add_button název zpráva` - Přidá tlačítko
`!ticket remove_button název` - Odstraní tlačítko
`!ticket clear_buttons` - Smaže všechna tlačítka
            """,
            inline=False
        )
        
        embed.add_field(
            name="Panel a nastavení",
            value="""
`!ticket panel [zpráva]` - Vytvoří panel
`!ticket settings` - Zobrazí nastavení
`!ticket ui menu/button` - Typ rozhraní
`!ticket close [důvod]` - Zavře ticket
            """,
            inline=False
        )
        
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(TicketCommands(bot))