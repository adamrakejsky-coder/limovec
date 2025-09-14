import discord
from discord.ext import commands
import asyncio
import hashlib
from typing import Dict, List, Optional, Tuple
import logging
from datetime import datetime, timezone
from .database import TicketDatabase
from .views import PersistentTicketView, TicketControlView
from .transcript import TranscriptGenerator
from ..utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

class TicketManager:
    def __init__(self, bot: commands.Bot, db_manager):
        self.bot = bot
        self.db_manager = db_manager
        self.ticket_db = TicketDatabase(db_manager)
        self.transcript_gen = TranscriptGenerator()
        self.rate_limiter = RateLimiter(1, 300)  # 1 ticket za 5 minut
        
        # Cache aktivních ticketů
        self.active_tickets = {}  # guild_id -> {user_id: [ticket_info]}
    
    async def setup_persistent_views(self):
        """Obnoví všechny persistent views po restartu"""
        try:
            logger.info("Obnovuji persistent views...")
            
            # Import zde aby se předešlo circular imports
            from .views import (
                PersistentTicketView, 
                TicketControlView, 
                TicketButton,
                TicketSelectMenu,
                CloseTicketButton
            )
            
            # Registruj globální persistent views pro všechny možné interakce
            for guild in self.bot.guilds:
                try:
                    settings = await self.ticket_db.get_settings(guild.id)
                    
                    # Registruj ticket panel view pro tento guild
                    if settings.get("custom_buttons"):
                        panel_view = PersistentTicketView(guild.id, settings)
                        self.bot.add_view(panel_view)
                        logger.info(f"Registrován ticket panel pro {guild.name}")
                    
                except Exception as e:
                    logger.warning(f"Chyba při registraci ticket views pro {guild.name}: {e}")
            
            # Musím registrovat pattern-based persistent views
            # Discord.py automaticky routuje interakce podle custom_id
            
            # Registruj univerzální handler pro všechny close button patterny
            class UniversalCloseView(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout=None)
                    
                    # Přidej mock button který nikdy nebude viděn - slouží jen pro registraci handleru
                    self.add_item(discord.ui.Button(
                        label="Mock", 
                        custom_id="close_ticket_mock", 
                        style=discord.ButtonStyle.red
                    ))
            
            # Discord.py potřebuje view registrovat s přesnými custom_id
            # Takže musím předem vytvořit view pro každý možný close_ticket_{creator_id}
            
            # Interaction handling je nyní v main.py global handleru
            logger.info("Ticket interaction handling delegováno na global handler")
            
            logger.info("Persistent views obnoveny")
        except Exception as e:
            logger.error(f"Chyba při obnovování persistent views: {e}")
    
    async def has_mod_permissions(self, user: discord.Member, guild: discord.Guild) -> bool:
        """Zkontroluje zda má uživatel mod oprávnění"""
        if user.guild_permissions.administrator:
            return True
        
        settings = await self.ticket_db.get_settings(guild.id)
        mod_role_id = settings.get("mod_role_id")
        admin_role_ids = settings.get("admin_role_ids", [])
        
        if mod_role_id and any(role.id == mod_role_id for role in user.roles):
            return True
        
        if any(role.id in admin_role_ids for role in user.roles):
            return True
        
        return False
    
    async def can_create_ticket(self, user: discord.Member, guild: discord.Guild, 
                               ticket_type: str) -> Tuple[bool, str]:
        """Zkontroluje zda může uživatel vytvořit ticket"""
        
        # Rate limiting check (per-guild per-user)
        rate_limit_key = f"{guild.id}_{user.id}"
        if not self.rate_limiter.can_call(rate_limit_key):
            cooldown = self.rate_limiter.get_cooldown(rate_limit_key)
            return False, f"Musíš počkat {cooldown} sekund před vytvořením dalšího ticketu."
        
        # Zjednodušená kontrola - jen zkontroluj zda už nemá otevřený kanál s podobným názvem
        for channel in guild.text_channels:
            if channel.name.startswith(f"ticket-{user.name}".lower()):
                # Má už otevřený ticket
                return False, f"Už máš otevřený ticket: {channel.mention}"
        
        return True, ""
    
    async def create_ticket(self, guild: discord.Guild, user: discord.Member, 
                           ticket_type: str, welcome_message: str, 
                           interaction: discord.Interaction = None):
        """Async vytvoření ticketu s full error handlingem"""
        
        # Kontroly
        can_create, reason = await self.can_create_ticket(user, guild, ticket_type)
        if not can_create:
            if interaction:
                await interaction.response.send_message(reason, ephemeral=True)
            return None, reason  # Return reason for persistent view handling
        
        settings = await self.ticket_db.get_settings(guild.id)
        mod_role_id = settings.get("mod_role_id")
        
        if not mod_role_id:
            error_msg = "Ticket systém není správně nakonfigurován. Kontaktuj administrátory."
            if interaction:
                await interaction.response.send_message(error_msg, ephemeral=True)
            return None, error_msg
        
        try:
            # Vytvoření kanálu s oprávněními
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                user: discord.PermissionOverwrite(
                    view_channel=True, 
                    send_messages=True, 
                    read_message_history=True
                ),
                guild.get_role(mod_role_id): discord.PermissionOverwrite(
                    view_channel=True, 
                    send_messages=True, 
                    read_message_history=True,
                    manage_messages=True
                )
            }
            
            # Přidej admin role oprávnění
            for admin_role_id in settings.get("admin_role_ids", []):
                admin_role = guild.get_role(admin_role_id)
                if admin_role:
                    overwrites[admin_role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        manage_messages=True
                    )
            
            # Vytvoř kanál
            channel_name = f"ticket-{user.name}-{ticket_type.lower()}"[:100]
            ticket_channel = await guild.create_text_channel(
                channel_name, 
                overwrites=overwrites,
                topic=f"Ticket od {user} | Typ: {ticket_type}"
            )
            
            # Pošli response
            if interaction:
                await interaction.response.send_message(
                    f"Ticket vytvoření: {ticket_channel.mention}", 
                    ephemeral=True
                )
            
            # Vytvoř welcome embed
            embed = discord.Embed(
                title=f"Ticket - {ticket_type}",
                description=welcome_message.replace("{user}", user.mention),
                color=discord.Color(settings.get("embed_color", 5793266)),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text=f"Ticket ID: {ticket_channel.id}")
            
            # Pošli welcome zprávu s control view
            control_view = TicketControlView(ticket_type, user.id)
            await ticket_channel.send(
                f"Ahoj {user.mention}!",
                embed=embed,
                view=control_view
            )
            
            # Jednoduché logování (volitelné)
            try:
                await self.ticket_db.log_ticket_action(
                    guild.id, user.id, ticket_type, "created", ticket_channel.id
                )
            except Exception as log_e:
                print(f"⚠️ Nepodařilo se zalogovat vytvoření ticketu: {log_e}")
            
            logger.info(f"Ticket vytvořen: {ticket_channel.name} pro {user}")
            return ticket_channel, None  # Success
            
        except discord.Forbidden:
            error_msg = "Nemám oprávnění vytvořit kanál."
            if interaction:
                await interaction.followup.send(error_msg, ephemeral=True)
            return None, error_msg
        except Exception as e:
            logger.error(f"Chyba při vytváření ticketu: {e}")
            error_msg = "Nastala neočekávaná chyba při vytváření ticketu."
            if interaction:
                await interaction.followup.send(error_msg, ephemeral=True)
            return None, error_msg
        
        return None, "Neznámá chyba"
    
    async def close_ticket(self, channel: discord.TextChannel, 
                          closer: discord.Member, ticket_type: str, 
                          reason: str = None):
        """Async zavření ticketu s transcriptem"""
        try:
            settings = await self.ticket_db.get_settings(channel.guild.id)
            
            # Generuj transcript
            transcript_file = None
            if settings.get("transcript_channel_id"):
                transcript_channel = channel.guild.get_channel(settings["transcript_channel_id"])
                if transcript_channel:
                    transcript_file = await self.transcript_gen.generate_transcript(channel)
            
            # Najdi ticket creator z názvu kanálu nebo databáze
            ticket_creator_id = None
            if channel.topic:
                # Parse z topic
                pass
            
            # Jednoduché logování zavření (volitelné)
            try:
                await self.ticket_db.log_ticket_action(
                    channel.guild.id, 
                    ticket_creator_id or 0, 
                    ticket_type, 
                    "closed", 
                    channel.id, 
                    closer.id, 
                    reason
                )
            except Exception as log_e:
                print(f"⚠️ Nepodařilo se zalogovat zavření ticketu: {log_e}")
            
            # Pošli transcript
            if transcript_file and transcript_channel:
                embed = discord.Embed(
                    title="Transcript ticketu",
                    description=f"Ticket zavřel: {closer.mention}",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc)
                )
                if reason:
                    embed.add_field(name="Důvod", value=reason, inline=False)
                
                await transcript_channel.send(embed=embed, file=transcript_file)
            
            # Smaž kanál
            await channel.delete(reason=f"Ticket zavřen uživatelem {closer}")
            
            logger.info(f"Ticket {channel.name} zavřen uživatelem {closer}")
            
        except Exception as e:
            logger.error(f"Chyba při zavírání ticketu: {e}")
            raise