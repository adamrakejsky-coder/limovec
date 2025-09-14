import discord
from discord.ext import commands, tasks
import asyncpg
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from keep_alive import keep_alive
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np
from io import BytesIO
from collections import OrderedDict
import time
import logging
import weakref

# Přidání current directory do Python path pro importy (Render compatibility)
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Kontoluj zda existuje bot složka a soubory
bot_folder_exists = os.path.exists(os.path.join(os.path.dirname(__file__), 'bot'))
manager_exists = os.path.exists(os.path.join(os.path.dirname(__file__), 'bot', 'database', 'manager.py'))

if bot_folder_exists and manager_exists:
    # Normální import pro development/production
    from bot.database.manager import DatabaseManager
    from bot.tickets.manager import TicketManager  
    from bot.utils.cache import LRUCache
    print("✅ Modular components loaded")
else:
    print("⚠️ Bot modules not found, using emergency fallback classes")
    # Emergency inline classes
    from collections import OrderedDict
    import time
    
    class LRUCache:
        def __init__(self, max_size=1000):
            self.max_size = max_size
            self.cache = OrderedDict()
            self.expiry = {}
        
        def get(self, key, default=None):
            if key in self.cache:
                if key in self.expiry and time.time() > self.expiry[key]:
                    del self.cache[key]
                    del self.expiry[key]
                    return default
                self.cache.move_to_end(key)
                return self.cache[key]
            return default
        
        def set(self, key, value, expire_in=3600):
            if key in self.cache:
                self.cache.move_to_end(key)
            else:
                if len(self.cache) >= self.max_size:
                    oldest = next(iter(self.cache))
                    del self.cache[oldest]
                    if oldest in self.expiry:
                        del self.expiry[oldest]
            self.cache[key] = value
            self.expiry[key] = time.time() + expire_in
        
        def cleanup_expired(self):
            current_time = time.time()
            expired_keys = [k for k, exp_time in self.expiry.items() if current_time > exp_time]
            for key in expired_keys:
                if key in self.cache:
                    del self.cache[key]
                del self.expiry[key]
            return len(expired_keys)
    
    # Placeholder pro DatabaseManager
    class DatabaseManager:
        def __init__(self):
            self.pool = None
        
        async def initialize(self):
            try:
                self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
                print("✅ Basic database connection established")
            except Exception as e:
                print(f"❌ Database connection failed: {e}")
                self.pool = None
        
        async def safe_operation(self, operation_name: str, operation_func, default_return=None):
            if not self.pool:
                return default_return
            try:
                return await operation_func()
            except Exception as e:
                print(f"❌ {operation_name}: {e}")
                return default_return
    
    # Placeholder pro TicketManager  
    class TicketManager:
        def __init__(self, bot, db_manager):
            self.bot = bot
            self.db_manager = db_manager
        
        async def setup_persistent_views(self):
            print("⚠️ Ticket system not fully available - using placeholder")
            pass
    
    print("⚠️ Using emergency fallback classes - some functionality may be limited")

# Načtení .env souboru
load_dotenv()

# Konfiguraci databáze
DATABASE_URL = os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL')
if not DATABASE_URL:
    print("❌ KRITICKÁ CHYBA: DATABASE_URL není nastavena! Bot bude pokračovat bez databáze.")
    DATABASE_URL = None

# Optimalizované intents - pouze co potřebujeme
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.invites = True
intents.guilds = True

bot = commands.Bot(
    command_prefix="!", 
    intents=intents,
    max_messages=1000,  # Omez cache zpráv
    case_insensitive=True
)

# Globální databázové připojení - nyní použijeme DatabaseManager
db_manager = DatabaseManager()

# Cache pro invite tracking
invite_cache = {}

# Globální cache objekty s LRUCache
audit_cache = LRUCache(1000)
guild_settings_cache = LRUCache(500)
voice_event_cache = LRUCache(200)  # Cache pro voice events
election_cache = LRUCache(500)  # Cache pro election settings
voice_debounce_tasks = {}  # Pro debouncing voice events

# Rate limitery
class RateLimiter:
    def __init__(self, max_calls=5, window=60):  # 5 volání za minutu
        self.max_calls = max_calls
        self.window = window
        self.calls = {}
    
    def can_call(self, guild_id):
        current_time = time.time()
        if guild_id not in self.calls:
            self.calls[guild_id] = []
        
        # Odstraň staré volání
        self.calls[guild_id] = [call_time for call_time in self.calls[guild_id] 
                               if current_time - call_time < self.window]
        
        if len(self.calls[guild_id]) < self.max_calls:
            self.calls[guild_id].append(current_time)
            return True
        return False

audit_rate_limiter = RateLimiter(5, 60)
reaction_rate_limiter = RateLimiter(20, 60)  # Max 20 reakcí za minutu per guild
voice_rate_limiter = RateLimiter(15, 60)     # Max 15 voice eventů za minutu per guild  
thread_rate_limiter = RateLimiter(10, 60)    # Max 10 thread eventů za minutu per guild
channel_rate_limiter = RateLimiter(10, 60)   # Max 10 channel eventů za minutu per guild
role_rate_limiter = RateLimiter(10, 60)      # Max 10 role eventů za minutu per guild

# Databázové funkce s novým DatabaseManager
async def safe_db_operation(operation_name: str, operation_func, default_return=None):
    """Safely execute database operation with error handling"""
    return await db_manager.safe_operation(operation_name, operation_func, default_return)

async def get_guild_settings(guild_id: int) -> Dict[str, Any]:
    # Zkus cache první
    cache_key = f"guild_settings_{guild_id}"
    cached = guild_settings_cache.get(cache_key)
    if cached:
        return cached
    
    async def _get_settings():
        async with db_manager.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT * FROM guild_settings WHERE guild_id = $1', guild_id)
            if row:
                settings = dict(row)
            else:
                settings = {
                    "guild_id": guild_id,
                    "log_channel": None,
                    "welcome_channel": None,
                    "goodbye_channel": None,
                    "welcome_msg": None,
                    "goodbye_msg": None,
                    "invite_tracking": True,
                    "log_reactions": False,        # Defaultně vypnuté kvůli spamu
                    "log_voice": True,             # Voice události
                    "log_threads": True,           # Thread události
                    "log_roles": True,             # Role události  
                    "log_channels": True,          # Channel události
                    "log_emojis": True,            # Emoji události
                    "log_user_updates": False      # User profile změny (může být spam)
                }
                # Vytvoř defaultní nastavení v databázi
                await conn.execute('''
                    INSERT INTO guild_settings (guild_id, invite_tracking, log_reactions, log_voice, 
                                               log_threads, log_roles, log_channels, log_emojis, log_user_updates) 
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) 
                    ON CONFLICT (guild_id) DO NOTHING
                ''', guild_id, True, False, True, True, True, True, True, False)
            
            guild_settings_cache.set(cache_key, settings, 1800)  # 30 min cache
            return settings
    
    default_settings = {
        "guild_id": guild_id,
        "log_channel": None,
        "welcome_channel": None,
        "goodbye_channel": None,
        "welcome_msg": None,
        "goodbye_msg": None,
        "invite_tracking": True,
        "log_reactions": False,
        "log_voice": True,
        "log_threads": True,
        "log_roles": True,
        "log_channels": True,
        "log_emojis": True,
        "log_user_updates": False
    }
    
    return await safe_db_operation(
        f"get_guild_settings({guild_id})",
        _get_settings,
        default_settings
    )

async def update_guild_settings(guild_id: int, key: str, value):
    async def _update_settings():
        async with db_manager.pool.acquire() as conn:
            # Dynamicky vytvoř UPDATE na základě klíče
            await conn.execute(f'''
                INSERT INTO guild_settings (guild_id, {key}, updated_at) 
                VALUES ($1, $2, CURRENT_TIMESTAMP)
                ON CONFLICT (guild_id) 
                DO UPDATE SET {key} = EXCLUDED.{key}, updated_at = CURRENT_TIMESTAMP
            ''', guild_id, value)
            
            # Invalidate cache
            cache_key = f"guild_settings_{guild_id}"
            guild_settings_cache.cache.pop(cache_key, None)
    
    await safe_db_operation(
        f"update_guild_settings({key})",
        _update_settings
    )

# Utility funkce pro formátování časů
def format_timestamp(dt):
    if dt is None:
        return "Neznámo"
    return f"<t:{int(dt.timestamp())}:F>"

# Cache pro invite tracking
async def cache_guild_invites(guild):
    try:
        invites = await guild.invites()
        invite_cache[guild.id] = {invite.code: invite.uses for invite in invites}
    except discord.Forbidden:
        print(f"⚠️ Nemám oprávnění načíst pozvánky pro {guild.name}")
    except Exception as e:
        print(f"⚠️ Chyba při načítání pozvánek pro {guild.name}: {e}")

# Audit log helper s rate limitingem
async def get_audit_executor(guild, action, target_id, target_type='user'):
    cache_key = f"{guild.id}_{action}_{target_id}_{target_type}"
    cached = audit_cache.get(cache_key)
    if cached:
        return cached
    
    if not audit_rate_limiter.can_call(guild.id):
        return None, None
    
    try:
        async for entry in guild.audit_logs(action=action, limit=10):
            if target_type == 'user' and hasattr(entry, 'user') and entry.user and entry.user.id == target_id:
                result = (entry.user, entry.reason)
                audit_cache.set(cache_key, result, 60)  # 1 min cache
                return result
            elif target_type == 'channel' and hasattr(entry, 'target') and entry.target and entry.target.id == target_id:
                result = (entry.user, entry.reason)
                audit_cache.set(cache_key, result, 60)
                return result
            elif hasattr(entry, 'target') and hasattr(entry.target, 'id') and entry.target.id == target_id:
                result = (entry.user, entry.reason)
                audit_cache.set(cache_key, result, 60)
                return result
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"⚠️ Chyba při načítání audit logu: {e}")
    
    result = (None, None)
    audit_cache.set(cache_key, result, 30)  # Cache i negativní výsledky
    return result

# Funkce pro posílání logů
async def send_log(guild, embed):
    try:
        settings = await get_guild_settings(guild.id)
        log_channel_id = settings.get("log_channel")
        if log_channel_id:
            log_channel = guild.get_channel(log_channel_id)
            if log_channel:
                await log_channel.send(embed=embed)
    except discord.Forbidden:
        print(f"Nemám oprávnění posílat do log kanálu v {guild.name}")
    except Exception as e:
        print(f"Chyba při posílání logu: {e}")

# Cache cleanup task
async def preload_all_settings():
    """Preload všech nastavení pro všechny guilds při startu"""
    if not db_manager.pool:
        print("⚠️ Databáze není připojena, přeskakuji preload nastavení")
        return
        
    try:
        loaded_count = 0
        for guild in bot.guilds:
            # Načti základní nastavení
            await get_guild_settings(guild.id)
            
            # Načti ticket nastavení (pokud existuje ticket_manager)
            if hasattr(bot, 'ticket_manager') and bot.ticket_manager:
                try:
                    await bot.ticket_manager.ticket_db.get_settings(guild.id)
                except Exception as e:
                    print(f"⚠️ Chyba při načítání ticket nastavení pro {guild.name}: {e}")
            
            # Načti election nastavení
            try:
                await get_current_election_type(guild.id)
                await get_voting_ui_type(guild.id)
            except Exception as e:
                print(f"⚠️ Chyba při načítání election nastavení pro {guild.name}: {e}")
            
            loaded_count += 1
        
        print(f"📋 Preload dokončen: {loaded_count} serverů načteno do cache")
        
    except Exception as e:
        print(f"❌ Chyba při preload nastavení: {e}")


@tasks.loop(hours=1)
async def cleanup_caches():
    """Čistí expirované záznamy z cache"""
    try:
        expired_audit = audit_cache.cleanup_expired()
        expired_guild = guild_settings_cache.cleanup_expired()
        expired_voice = voice_event_cache.cleanup_expired()
        expired_election = election_cache.cleanup_expired()
        
        print(f"🧹 Cache cleanup: {expired_audit} audit, {expired_guild} guild, {expired_voice} voice, {expired_election} election")
        
        # Cleanup starých voice debounce tasků
        current_time = time.time()
        old_tasks = []
        for key, task in voice_debounce_tasks.items():
            if task.done() or task.cancelled():
                old_tasks.append(key)
        
        for key in old_tasks:
            voice_debounce_tasks.pop(key, None)
        
        if old_tasks:
            print(f"🧹 Vyčištěno {len(old_tasks)} starých voice tasků")
            
    except Exception as e:
        print(f"❌ Chyba při cache cleanup: {e}")

@bot.event
async def on_ready():
    print(f"✅ Přihlášen jako {bot.user}")
    
    # Zaznamenej start time pro uptime tracking
    bot.start_time = datetime.now(timezone.utc)
    
    # Inicializace databáze s novým DatabaseManager
    await db_manager.initialize()
    
    # Inicializace ticket systému
    if db_manager.pool:
        ticket_manager = TicketManager(bot, db_manager)
        await ticket_manager.setup_persistent_views()
        bot.ticket_manager = ticket_manager
        print("✅ Ticket systém inicializován")
        
        # Načtení ticket commands
        try:
            from bot.commands.tickets import TicketCommands
            await bot.add_cog(TicketCommands(bot))
            print("✅ Ticket příkazy načteny")
        except Exception as e:
            print(f"❌ Chyba při načítání ticket příkazů: {e}")
            print("⚠️ Ticket systém nebude plně funkční")
        
        # Setup globálního interaction handleru pro všechny persistent views
        @bot.event
        async def on_interaction(interaction):
            if interaction.type == discord.InteractionType.component:
                custom_id = interaction.data.get('custom_id', '')
                
                # Handle ticket close button patterny
                if custom_id.startswith('close_ticket_'):
                    try:
                        if hasattr(bot, 'ticket_manager'):
                            await bot.ticket_manager.close_ticket(
                                interaction.channel,
                                interaction.user, 
                                "Zavřeno přes tlačítko"
                            )
                            await interaction.response.send_message("🔒 Ticket je zavírán...", ephemeral=True)
                        else:
                            await interaction.response.send_message("❌ Ticket systém není dostupný.", ephemeral=True)
                        return
                    except Exception as e:
                        print(f"Chyba při zavírání ticketu: {e}")
                        try:
                            await interaction.response.send_message("❌ Chyba při zavírání ticketu.", ephemeral=True)
                        except:
                            pass
                        return
                
                # Handle ticket creation button patterny
                elif custom_id.startswith('ticket_'):
                    try:
                        if hasattr(bot, 'ticket_manager'):
                            import hashlib
                            settings = await bot.ticket_manager.ticket_db.get_settings(interaction.guild.id)
                            buttons = settings.get('custom_buttons', [])
                            
                            # Najdi správný button podle custom_id hash
                            button_info = None
                            for label, welcome_msg in buttons:
                                button_hash = hashlib.md5(f"{interaction.guild.id}_{label}".encode()).hexdigest()[:8]
                                if custom_id == f"ticket_{button_hash}":
                                    button_info = {'name': label, 'message': welcome_msg}
                                    break
                            
                            if button_info:
                                from bot.tickets.views import handle_ticket_creation
                                await handle_ticket_creation(interaction, button_info, bot.ticket_manager)
                            else:
                                await interaction.response.send_message("❌ Tento ticket typ už neexistuje.", ephemeral=True)
                        else:
                            await interaction.response.send_message("❌ Ticket systém není dostupný.", ephemeral=True)
                        return
                    except Exception as e:
                        print(f"Chyba při vytváření ticketu: {e}")
                        try:
                            await interaction.response.send_message("❌ Chyba při vytváření ticketu.", ephemeral=True)
                        except:
                            pass
                        return
                
                # Handle voting button patterny
                elif custom_id.startswith('vote_') and len(custom_id.split('_')) >= 3:
                    try:
                        parts = custom_id.split('_')
                        if parts[0] == 'vote':
                            guild_id = int(parts[1]) 
                            candidate_id = int(parts[2])
                            
                            if interaction.guild.id == guild_id:
                                await handle_vote(interaction, candidate_id)
                                return
                            else:
                                await interaction.response.send_message("❌ Toto hlasování není pro tento server.", ephemeral=True)
                                return
                    except (ValueError, IndexError) as e:
                        print(f"Chyba při zpracování voting button: {e}")
                        try:
                            await interaction.response.send_message("❌ Chyba při zpracování hlasu.", ephemeral=True)
                        except:
                            pass
                        return
                
                # Handle voting select patterny  
                elif custom_id.startswith('vote_select_'):
                    try:
                        parts = custom_id.split('_')
                        if len(parts) >= 4:
                            guild_id = int(parts[2])
                            
                            if interaction.guild.id == guild_id:
                                candidate_id = int(interaction.data['values'][0])
                                await handle_vote(interaction, candidate_id)
                                return
                            else:
                                await interaction.response.send_message("❌ Toto hlasování není pro tento server.", ephemeral=True)
                                return
                    except (ValueError, IndexError, KeyError) as e:
                        print(f"Chyba při zpracování voting select: {e}")
                        try:
                            await interaction.response.send_message("❌ Chyba při zpracování hlasu.", ephemeral=True)
                        except:
                            pass
                        return
        
        print("✅ Globální interaction handler inicializován (voting + tickets)")
    
    # Spuštění cache cleanup tasku
    if not cleanup_caches.is_running():
        cleanup_caches.start()
        print("🧹 Cache cleanup task spuštěn")
    
    # Test databázového připojení pouze pokud máme databázi
    if db_manager.pool:
        try:
            test_guild_id = 123456789  # Test ID
            test_settings = await get_guild_settings(test_guild_id)
            print(f"🔍 Test databáze - načtena nastavení: {test_settings}")
        except Exception as e:
            print(f"❌ Test databáze selhal: {e}")
    
    # Preload nastavení pro všechny guilds
    await preload_all_settings()
    
    # Load existing invites do cache
    for guild in bot.guilds:
        await cache_guild_invites(guild)
    
    print(f"🔄 Připraven sledovat {len(bot.guilds)} serverů")

@bot.event
async def on_guild_join(guild):
    await cache_guild_invites(guild)
    
    # Preload nastavení pro nový server
    try:
        await get_guild_settings(guild.id)
        await get_current_election_type(guild.id)  
        await get_voting_ui_type(guild.id)
        print(f"📋 Načtena nastavení pro nový server: {guild.name}")
    except Exception as e:
        print(f"⚠️ Chyba při načítání nastavení pro {guild.name}: {e}")

# Příkazy
@bot.command()
@commands.has_permissions(administrator=True)
async def set_logs(ctx, channel: discord.TextChannel):
    await update_guild_settings(ctx.guild.id, "log_channel", channel.id)
    await ctx.send(f"✅ Logovací kanál nastaven na {channel.mention}")

@bot.command()
@commands.has_permissions(administrator=True)
async def set_welcome(ctx, channel: discord.TextChannel, *, message: str):
    await update_guild_settings(ctx.guild.id, "welcome_channel", channel.id)
    await update_guild_settings(ctx.guild.id, "welcome_msg", message)
    await ctx.send(f"✅ Welcome zpráva nastavena pro {channel.mention}")

@bot.command()
@commands.has_permissions(administrator=True)
async def set_goodbye(ctx, channel: discord.TextChannel, *, message: str):
    await update_guild_settings(ctx.guild.id, "goodbye_channel", channel.id)
    await update_guild_settings(ctx.guild.id, "goodbye_msg", message)
    await ctx.send(f"✅ Goodbye zpráva nastavena pro {channel.mention}")

@bot.command()
@commands.has_permissions(administrator=True)
async def bot_health(ctx):
    """Zobrazí health status bota"""
    try:
        embed = discord.Embed(title="🏥 Bot Health Status", color=discord.Color.blue())
        
        # Database status
        db_status = "🟢 Online" if db_manager.pool else "🔴 Offline"
        embed.add_field(name="📊 Databáze", value=db_status, inline=True)
        
        # Uptime
        if hasattr(bot, 'start_time'):
            uptime = datetime.now(timezone.utc) - bot.start_time
            hours, remainder = divmod(int(uptime.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"{hours}h {minutes}m {seconds}s"
            embed.add_field(name="⏰ Uptime", value=uptime_str, inline=True)
        
        # Guild count
        embed.add_field(name="🏰 Servery", value=str(len(bot.guilds)), inline=True)
        
        # Cache stats
        cache_stats = f"Guild: {len(guild_settings_cache.cache)}, Audit: {len(audit_cache.cache)}"
        embed.add_field(name="💾 Cache", value=cache_stats, inline=False)
        
        # Ticket system status
        if hasattr(bot, 'ticket_manager'):
            embed.add_field(name="🎫 Ticket systém", value="🟢 Aktivní", inline=True)
        else:
            embed.add_field(name="🎫 Ticket systém", value="🔴 Neaktivní", inline=True)
        
        embed.timestamp = datetime.now(timezone.utc)
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Chyba při získávání health status: {e}")

@bot.command()
@commands.has_permissions(administrator=True)
async def toggle_invites(ctx):
    """Zapne/vypne invite tracking"""
    data = await get_guild_settings(ctx.guild.id)
    current = data.get("invite_tracking", True)
    new_value = not current
    
    await update_guild_settings(ctx.guild.id, "invite_tracking", new_value)
    status = "zapnut" if new_value else "vypnut"
    await ctx.send(f"✅ Invite tracking {status}")

@bot.command()
@commands.has_permissions(administrator=True) 
async def toggle_log(ctx, log_type: str):
    """Zapne/vypne určitý typ logování
    Dostupné typy: reactions, voice, threads, roles, channels, emojis, user_updates"""
    
    valid_types = ["reactions", "voice", "threads", "roles", "channels", "emojis", "user_updates"]
    
    if log_type.lower() not in valid_types:
        await ctx.send(f"❌ Neplatný typ! Dostupné: {', '.join(valid_types)}")
        return
    
    settings_key = f"log_{log_type.lower()}"
    data = await get_guild_settings(ctx.guild.id)
    current = data.get(settings_key, True)
    new_value = not current
    
    await update_guild_settings(ctx.guild.id, settings_key, new_value)
    status = "zapnut" if new_value else "vypnut"
    await ctx.send(f"✅ {log_type.capitalize()} logging {status}")

@bot.command()
@commands.has_permissions(administrator=True)
async def log_status(ctx):
    """Zobrazí stav všech logging nastavení"""
    settings = await get_guild_settings(ctx.guild.id)
    
    embed = discord.Embed(title="📊 Stav logování", color=discord.Color.blue())
    
    log_settings = [
        ("Reactions", settings.get("log_reactions", False)),
        ("Voice", settings.get("log_voice", True)), 
        ("Threads", settings.get("log_threads", True)),
        ("Roles", settings.get("log_roles", True)),
        ("Channels", settings.get("log_channels", True)),
        ("Emojis", settings.get("log_emojis", True)),
        ("User Updates", settings.get("log_user_updates", False)),
        ("Invite Tracking", settings.get("invite_tracking", True))
    ]
    
    for name, enabled in log_settings:
        status = "🟢 Zapnuto" if enabled else "🔴 Vypnuto"
        embed.add_field(name=name, value=status, inline=True)
    
    log_channel = ctx.guild.get_channel(settings.get("log_channel")) if settings.get("log_channel") else None
    embed.add_field(
        name="Log kanál", 
        value=log_channel.mention if log_channel else "❌ Nenastaveno", 
        inline=False
    )
    
    embed.set_footer(text="Použij !toggle_log <typ> pro změnu nastavení")
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def log_enable_all(ctx):
    """Zapne všechny typy logování"""
    log_types = ["reactions", "voice", "threads", "roles", "channels", "emojis", "user_updates"]
    
    for log_type in log_types:
        settings_key = f"log_{log_type}"
        await update_guild_settings(ctx.guild.id, settings_key, True)
    
    embed = discord.Embed(
        title="✅ Všechno logování zapnuto", 
        description="Všechny typy logování byly aktivovány.",
        color=discord.Color.green()
    )
    embed.add_field(
        name="Aktivované typy", 
        value=", ".join([t.capitalize() for t in log_types]), 
        inline=False
    )
    embed.set_footer(text="⚠️ Pozor: Reactions a User Updates mohou generovat hodně zpráv!")
    await ctx.send(embed=embed)

@bot.command() 
@commands.has_permissions(administrator=True)
async def log_disable_all(ctx):
    """Vypne všechny typy logování (kromě základních)"""
    log_types = ["reactions", "voice", "threads", "roles", "channels", "emojis", "user_updates"]
    
    for log_type in log_types:
        settings_key = f"log_{log_type}"
        await update_guild_settings(ctx.guild.id, settings_key, False)
    
    embed = discord.Embed(
        title="🔴 Rozšířené logování vypnuto", 
        description="Všechny rozšířené typy logování byly deaktivovány.\nZákladní logy (zprávy, bany, členi) zůstávají aktivní.",
        color=discord.Color.red()
    )
    embed.add_field(
        name="Deaktivované typy", 
        value=", ".join([t.capitalize() for t in log_types]), 
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True) 
async def log_reset(ctx):
    """Resetuje nastavení logování na výchozí hodnoty"""
    # Výchozí nastavení
    default_settings = {
        "log_reactions": False,      # Defaultně vypnuté kvůli spamu
        "log_voice": True,           # Voice události
        "log_threads": True,         # Thread události
        "log_roles": True,           # Role události  
        "log_channels": True,        # Channel události
        "log_emojis": True,          # Emoji události
        "log_user_updates": False    # User profile změny (může být spam)
    }
    
    for setting_key, default_value in default_settings.items():
        await update_guild_settings(ctx.guild.id, setting_key, default_value)
    
    embed = discord.Embed(
        title="🔄 Logování resetováno", 
        description="Nastavení logování bylo obnoveno na výchozí hodnoty.",
        color=discord.Color.blue()
    )
    
    enabled = [k.replace("log_", "").capitalize() for k, v in default_settings.items() if v]
    disabled = [k.replace("log_", "").capitalize() for k, v in default_settings.items() if not v]
    
    if enabled:
        embed.add_field(name="🟢 Zapnuto", value=", ".join(enabled), inline=True)
    if disabled:
        embed.add_field(name="🔴 Vypnuto", value=", ".join(disabled), inline=True)
        
    embed.set_footer(text="Použij !log_status pro zobrazení aktuálního stavu")
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def db_test(ctx):
    """Test databázového připojení"""
    try:
        if not db_manager.pool:
            await ctx.send("❌ Databáze není připojena")
            return
        
        async with db_manager.pool.acquire() as conn:
            result = await conn.fetchval('SELECT 1')
            await ctx.send(f"✅ Databáze OK - Test query result: {result}")
    except Exception as e:
        await ctx.send(f"❌ Databáze ERROR: {e}")

@bot.command()
@commands.has_permissions(administrator=True)
async def cleanup_cache(ctx):
    """Manuálně spustí cache cleanup"""
    try:
        await ctx.send("🧹 Spouštím cache cleanup...")
        
        expired_audit = audit_cache.cleanup_expired()
        expired_guild = guild_settings_cache.cleanup_expired()
        expired_voice = voice_event_cache.cleanup_expired()
        expired_election = election_cache.cleanup_expired()
        
        embed = discord.Embed(title="🧹 Cache Cleanup", color=discord.Color.green())
        embed.add_field(name="Audit cache", value=f"Vyčištěno {expired_audit} záznamů", inline=True)
        embed.add_field(name="Guild cache", value=f"Vyčištěno {expired_guild} záznamů", inline=True)
        embed.add_field(name="Voice cache", value=f"Vyčištěno {expired_voice} záznamů", inline=True)
        embed.add_field(name="Election cache", value=f"Vyčištěno {expired_election} záznamů", inline=True)
        
        # Cleanup voice debounce tasks
        old_tasks = []
        for key, task in voice_debounce_tasks.items():
            if task.done() or task.cancelled():
                old_tasks.append(key)
        
        for key in old_tasks:
            voice_debounce_tasks.pop(key, None)
        
        embed.add_field(name="Voice tasks", value=f"Vyčištěno {len(old_tasks)} tasků", inline=True)
        embed.timestamp = datetime.now(timezone.utc)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Chyba při cache cleanup: {e}")

@bot.command()
@commands.has_permissions(administrator=True)
async def upravit_kandidata(ctx, candidate_id: int, *, new_name: str):
    """Upraví název existujícího kandidáta/strany"""
    new_name = new_name.strip()
    
    if len(new_name) > 100:
        await ctx.send("❌ Název je příliš dlouhý (max 100 znaků)")
        return
    
    async def _edit_candidate():
        async with db_manager.pool.acquire() as conn:
            # Zkontroluj zda kandidát existuje
            candidate = await conn.fetchrow('''
                SELECT name FROM rp_candidates 
                WHERE id = $1 AND guild_id = $2
            ''', candidate_id, ctx.guild.id)
            
            if not candidate:
                return None
            
            old_name = candidate['name']
            
            # Uprav název
            await conn.execute('''
                UPDATE rp_candidates 
                SET name = $1 
                WHERE id = $2 AND guild_id = $3
            ''', new_name, candidate_id, ctx.guild.id)
            
            return old_name
    
    result = await safe_db_operation("edit_candidate", _edit_candidate)
    
    if result:
        await ctx.send(f"✅ Kandidát změněn z **{result}** na **{new_name}**")
    else:
        await ctx.send("❌ Kandidát s tímto ID nebyl nalezen!")

# RP VOLBY PŘÍKAZY
async def get_current_election_type(guild_id: int) -> str:
    """Získá typ aktuálních voleb"""
    cache_key = f"election_type_{guild_id}"
    cached = election_cache.get(cache_key)
    if cached:
        return cached
    
    async def _get_type():
        async with db_manager.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT current_type FROM rp_election_settings WHERE guild_id = $1', guild_id)
            result = row['current_type'] if row else 'presidential'
            election_cache.set(cache_key, result, 1800)  # 30 min cache
            return result
    
    return await safe_db_operation("get_election_type", _get_type, 'presidential')

async def get_voting_ui_type(guild_id: int) -> str:
    """Získá typ UI pro hlasování"""
    cache_key = f"voting_ui_{guild_id}"
    cached = election_cache.get(cache_key)
    if cached:
        return cached
    
    async def _get_ui():
        async with db_manager.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT voting_ui FROM rp_election_settings WHERE guild_id = $1', guild_id)
            result = row['voting_ui'] if row else 'buttons'
            election_cache.set(cache_key, result, 1800)  # 30 min cache
            return result
    
    return await safe_db_operation("get_voting_ui", _get_ui, 'buttons')

@bot.command()
@commands.has_permissions(administrator=True)
async def nastavit_volby(ctx, election_type: str):
    """Nastaví typ voleb: presidential nebo parliamentary"""
    if election_type.lower() not in ['presidential', 'parliamentary']:
        await ctx.send("❌ Neplatný typ voleb! Použij: `presidential` nebo `parliamentary`")
        return
    
    async def _set_election():
        async with db_manager.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO rp_election_settings (guild_id, current_type, updated_at)
                VALUES ($1, $2, CURRENT_TIMESTAMP)
                ON CONFLICT (guild_id) DO UPDATE SET
                current_type = EXCLUDED.current_type, updated_at = CURRENT_TIMESTAMP
            ''', ctx.guild.id, election_type.lower())
            
            # Invalidate cache
            cache_key = f"election_type_{ctx.guild.id}"
            election_cache.cache.pop(cache_key, None)
    
    await safe_db_operation("set_election_type", _set_election)
    await ctx.send(f"✅ Typ voleb nastaven na: **{election_type.capitalize()}**")

@bot.command()
@commands.has_permissions(administrator=True)
async def pridat_kandidata(ctx, *, name: str):
    """Přidá kandidáta/stranu do RP voleb"""
    name = name.strip()
    
    if len(name) > 100:
        await ctx.send("❌ Název je příliš dlouhý (max 100 znaků)")
        return
    
    election_type = await get_current_election_type(ctx.guild.id)
    
    async def _add_candidate():
        async with db_manager.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO rp_candidates (guild_id, name, election_type, created_by)
                VALUES ($1, $2, $3, $4)
            ''', ctx.guild.id, name, election_type, ctx.author.id)
    
    await safe_db_operation("add_candidate", _add_candidate)
    await ctx.send(f"✅ {'Kandidát' if election_type == 'presidential' else 'Strana'} **{name}** {'přidán' if election_type == 'presidential' else 'přidána'}!")

@bot.command()  
async def volit(ctx):
    """Zobrazí interaktivní hlasovací menu"""
    try:
        current_type = await get_current_election_type(ctx.guild.id)
        ui_type = await get_voting_ui_type(ctx.guild.id)
        
        async def _get_candidates():
            async with db_manager.pool.acquire() as conn:
                rows = await conn.fetch('''
                    SELECT id, name FROM rp_candidates 
                    WHERE guild_id = $1 AND election_type = $2
                    ORDER BY created_at
                ''', ctx.guild.id, current_type)
                return [dict(row) for row in rows]
        
        candidates = await safe_db_operation("get_candidates", _get_candidates, [])
        
        if not candidates:
            await ctx.send(f"❌ Nejsou k dispozici žádní {'kandidáti' if current_type == 'presidential' else 'strany'}!")
            return
        
        title = f"🗳️ {'Prezidentské volby' if current_type == 'presidential' else 'Parlamentní volby'}"
        embed = discord.Embed(title=title, color=discord.Color.blue())
        embed.set_footer(text="Každý uživatel může hlasovat pouze jednou")
        
        if ui_type == 'dropdown':
            # Dropdown menu pro hlasování
            class VotingSelect(discord.ui.Select):
                def __init__(self, candidates_list):
                    options = []
                    for candidate in candidates_list[:25]:  # Discord limit
                        options.append(discord.SelectOption(
                            label=candidate['name'][:100],
                            value=str(candidate['id']),
                            description=f"Hlasovat pro {candidate['name']}"[:100]
                        ))
                    
                    super().__init__(
                        placeholder=f"Vyber {'kandidáta' if current_type == 'presidential' else 'stranu'}...",
                        options=options,
                        custom_id=f"vote_select_{ctx.guild.id}_{current_type}"
                    )
                
                async def callback(self, interaction: discord.Interaction):
                    candidate_id = int(self.values[0])
                    await handle_vote(interaction, candidate_id)
            
            class VotingView(discord.ui.View):
                def __init__(self, candidates_list):
                    super().__init__(timeout=None)  # Persistent view
                    self.add_item(VotingSelect(candidates_list))
            
            await ctx.send(embed=embed, view=VotingView(candidates))
        
        else:
            # Tlačítka pro hlasování
            class VotingView(discord.ui.View):
                def __init__(self, candidates_list):
                    super().__init__(timeout=None)  # Persistent view
                    for i, candidate in enumerate(candidates_list[:20]):  # Discord limit
                        button = discord.ui.Button(
                            label=candidate['name'][:80],
                            style=discord.ButtonStyle.primary,
                            custom_id=f"vote_{ctx.guild.id}_{candidate['id']}"
                        )
                        button.callback = self.create_callback(candidate['id'])
                        self.add_item(button)
                
                def create_callback(self, candidate_id):
                    async def callback(interaction: discord.Interaction):
                        await handle_vote(interaction, candidate_id)
                    return callback
            
            await ctx.send(embed=embed, view=VotingView(candidates))
            
    except Exception as e:
        await ctx.send(f"❌ Chyba při načítání hlasování: {e}")

async def handle_vote(interaction: discord.Interaction, candidate_id: int):
    """Zpracuje hlasování uživatele"""
    try:
        # Zkontroluj 14-denní minimum na serveru
        member = interaction.guild.get_member(interaction.user.id)
        if member and member.joined_at:
            days_on_server = (datetime.now(timezone.utc) - member.joined_at).days
            if days_on_server < 14:
                days_remaining = 14 - days_on_server
                eligible_date = member.joined_at + timedelta(days=14)
                await interaction.response.send_message(
                    f"❌ Musíš být na serveru alespoň 14 dní pro hlasování!\n"
                    f"📅 Připojil ses: {format_timestamp(member.joined_at)}\n" 
                    f"⏳ Budeš moci hlasovat: {format_timestamp(eligible_date)}\n"
                    f"🕐 Zbývá: {days_remaining} dní",
                    ephemeral=True
                )
                return
        
        async def _vote():
            async with db_manager.pool.acquire() as conn:
                # Zkontroluj zda už hlasoval
                existing = await conn.fetchrow('''
                    SELECT id FROM rp_votes 
                    WHERE guild_id = $1 AND user_id = $2
                ''', interaction.guild.id, interaction.user.id)
                
                if existing:
                    return "already_voted"
                
                # Přidej hlas
                await conn.execute('''
                    INSERT INTO rp_votes (guild_id, user_id, candidate_id)
                    VALUES ($1, $2, $3)
                ''', interaction.guild.id, interaction.user.id, candidate_id)
                
                # Získej jméno kandidáta
                candidate = await conn.fetchrow('''
                    SELECT name FROM rp_candidates WHERE id = $1
                ''', candidate_id)
                
                return candidate['name'] if candidate else "unknown"
        
        result = await safe_db_operation("handle_vote", _vote)
        
        if result == "already_voted":
            await interaction.response.send_message("❌ Už jsi hlasoval!", ephemeral=True)
        elif result == "unknown":
            await interaction.response.send_message("❌ Chyba při hlasování!", ephemeral=True)
        else:
            await interaction.response.send_message(f"✅ Tvůj hlas pro **{result}** byl zaznamenán!", ephemeral=True)
            
    except Exception as e:
        await interaction.response.send_message(f"❌ Chyba při hlasování: {e}", ephemeral=True)

@bot.command()
@commands.has_permissions(administrator=True)
async def vysledky(ctx):
    """Zobrazí výsledky RP voleb"""
    try:
        current_type = await get_current_election_type(ctx.guild.id)
        
        async def _get_results():
            async with db_manager.pool.acquire() as conn:
                rows = await conn.fetch('''
                    SELECT c.name, COUNT(v.id) as votes
                    FROM rp_candidates c
                    LEFT JOIN rp_votes v ON c.id = v.candidate_id
                    WHERE c.guild_id = $1 AND c.election_type = $2
                    GROUP BY c.id, c.name
                    ORDER BY votes DESC, c.name
                ''', ctx.guild.id, current_type)
                return [dict(row) for row in rows]
        
        results = await safe_db_operation("get_results", _get_results, [])
        
        if not results:
            await ctx.send("❌ Nejsou k dispozici žádné výsledky!")
            return
        
        title = f"📊 Výsledky {'prezidentských voleb' if current_type == 'presidential' else 'parlamentních voleb'}"
        embed = discord.Embed(title=title, color=discord.Color.gold())
        
        total_votes = sum(result['votes'] for result in results)
        embed.add_field(name="Celkový počet hlasů", value=str(total_votes), inline=False)
        
        for i, result in enumerate(results, 1):
            percentage = (result['votes'] / total_votes * 100) if total_votes > 0 else 0
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            
            embed.add_field(
                name=f"{medal} {result['name']}",
                value=f"**{result['votes']}** hlasů ({percentage:.1f}%)",
                inline=True
            )
        
        embed.timestamp = datetime.now(timezone.utc)
        
        # Vytvoř koláčový graf
        if total_votes > 0:
            try:
                names = [result['name'] for result in results]
                votes = [result['votes'] for result in results]
                colors = ['#FF6B6B', '#4ECDC4', '#45B7D1', '#96CEB4', '#FFEAA7', '#DDA0DD', '#98D8C8', '#F7DC6F']
                
                plt.figure(figsize=(10, 8))
                plt.pie(votes, labels=names, colors=colors[:len(names)], autopct='%1.1f%%', startangle=90)
                plt.title(f'Výsledky {"prezidentských voleb" if current_type == "presidential" else "parlamentních voleb"}', 
                         fontsize=16, fontweight='bold')
                
                # Uložení do BytesIO
                buffer = BytesIO()
                plt.savefig(buffer, format='png', dpi=150, bbox_inches='tight')
                buffer.seek(0)
                plt.close()
                
                # Odeslání s grafem
                file = discord.File(buffer, filename="vysledky.png")
                embed.set_image(url="attachment://vysledky.png")
                # Přidej button pro detailní přehled
                class DetailedResultsView(discord.ui.View):
                    def __init__(self):
                        super().__init__(timeout=300)
                    
                    @discord.ui.button(label="📋 Detailní přehled hlasů", style=discord.ButtonStyle.primary)
                    async def show_detailed_votes(self, interaction: discord.Interaction, button: discord.ui.Button):
                        if interaction.user.guild_permissions.administrator:
                            await show_detailed_voting_breakdown(interaction, current_type)
                        else:
                            await interaction.response.send_message("❌ Pouze administrátoři mohou zobrazit detailní přehled.", ephemeral=True)
                
                await ctx.send(embed=embed, file=file, view=DetailedResultsView())
                
            except Exception as e:
                print(f"Chyba při vytváření grafu: {e}")
                # Bez grafu, ale s buttonem
                class DetailedResultsView(discord.ui.View):
                    def __init__(self):
                        super().__init__(timeout=300)
                    
                    @discord.ui.button(label="📋 Detailní přehled hlasů", style=discord.ButtonStyle.primary)
                    async def show_detailed_votes(self, interaction: discord.Interaction, button: discord.ui.Button):
                        if interaction.user.guild_permissions.administrator:
                            await show_detailed_voting_breakdown(interaction, current_type)
                        else:
                            await interaction.response.send_message("❌ Pouze administrátoři mohou zobrazit detailní přehled.", ephemeral=True)
                
                await ctx.send(embed=embed, view=DetailedResultsView())
        else:
            # Bez grafu, ale s buttonem
            class DetailedResultsView(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout=300)
                
                @discord.ui.button(label="📋 Detailní přehled hlasů", style=discord.ButtonStyle.primary)
                async def show_detailed_votes(self, interaction: discord.Interaction, button: discord.ui.Button):
                    if interaction.user.guild_permissions.administrator:
                        await show_detailed_voting_breakdown(interaction, current_type)
                    else:
                        await interaction.response.send_message("❌ Pouze administrátoři mohou zobrazit detailní přehled.", ephemeral=True)
            
            await ctx.send(embed=embed, view=DetailedResultsView())
        
    except Exception as e:
        await ctx.send(f"❌ Chyba při získávání výsledků: {e}")

async def show_detailed_voting_breakdown(interaction: discord.Interaction, election_type: str):
    """Zobrazí detailní přehled hlasů podobně jako na obrázku - seřazené strany s hlasy"""
    try:
        await interaction.response.defer(ephemeral=True)
        
        async def _get_detailed_breakdown():
            async with db_manager.pool.acquire() as conn:
                # Jednodušší dotaz - získej kandidáty seřazené podle hlasů
                candidates = await conn.fetch('''
                    SELECT
                        c.id as candidate_id,
                        c.name as candidate_name,
                        COUNT(v.id) as vote_count
                    FROM rp_candidates c
                    LEFT JOIN rp_votes v ON c.id = v.candidate_id
                    WHERE c.guild_id = $1 AND c.election_type = $2
                    GROUP BY c.id, c.name
                    ORDER BY COUNT(v.id) DESC, c.name
                ''', interaction.guild.id, election_type)

                # Pro každého kandidáta získej jeho konkrétní hlasy
                result = []
                for candidate in candidates:
                    votes = await conn.fetch('''
                        SELECT v.id as vote_id, v.user_id, v.voted_at
                        FROM rp_votes v
                        WHERE v.candidate_id = $1
                        ORDER BY v.voted_at DESC
                    ''', candidate['candidate_id'])

                    result.append({
                        'candidate_id': candidate['candidate_id'],
                        'candidate_name': candidate['candidate_name'],
                        'vote_count': candidate['vote_count'],
                        'votes': [dict(vote) for vote in votes]
                    })

                return result
        
        breakdown = await safe_db_operation("detailed_breakdown", _get_detailed_breakdown, [])
        
        if not breakdown:
            await interaction.followup.send("❌ Žádné kandidáty nalezeny.", ephemeral=True)
            return
        
        # Vytvoř embed podobný obrázku
        embed = discord.Embed(
            title="📋 Detailní přehled hlasů",
            color=discord.Color.blue()
        )
        
        total_votes = sum(row['vote_count'] for row in breakdown)
        embed.add_field(name="Celkem hlasů", value=str(total_votes), inline=False)
        
        # Pro každého kandidáta vytvoř sekci
        for candidate in breakdown:
            name = candidate['candidate_name']
            vote_count = candidate['vote_count']
            votes_data = candidate['votes'] or []

            if vote_count == 0:
                # Kandidát bez hlasů
                embed.add_field(
                    name=f"{name} (0 hlasů)",
                    value="*Žádné hlasy*",
                    inline=False
                )
            else:
                # Kandidát s hlasy - vytvoř seznam voličů
                voters_list = []
                for vote_data in votes_data:
                    # vote_data je už dict, takže přistupuju přímo k klíčům
                    vote_id = vote_data['vote_id']
                    user_id = vote_data['user_id']
                    # Formát jako na obrázku: ID:123 @user
                    voters_list.append(f"ID:{vote_id} <@{user_id}>")

                voters_text = "\n".join(voters_list) if voters_list else "*Žádné hlasy*"
                
                # Omez délku pokud je moc hlasů
                if len(voters_text) > 1000:
                    # Vezmi jen první část + počet
                    visible_votes = voters_list[:10]
                    remaining = len(voters_list) - 10
                    voters_text = "\n".join(visible_votes) + f"\n... a dalších {remaining} hlasů"
                
                embed.add_field(
                    name=f"{name} ({vote_count} {'hlas' if vote_count == 1 else 'hlasy' if vote_count < 5 else 'hlasů'})",
                    value=voters_text,
                    inline=False
                )
        
        embed.set_footer(text="💡 Použij !odstranit_hlas <ID> pro odstranění konkrétního hlasu")
        
        # Pokud je embed příliš dlouhý, rozdělí ho na více zpráv
        if len(embed) > 6000:  # Discord limit je ~6000 characters
            # Pošli základní info
            summary_embed = discord.Embed(
                title="📋 Detailní přehled hlasů - Souhrn",
                color=discord.Color.blue()
            )
            summary_embed.add_field(name="Celkem hlasů", value=str(total_votes), inline=False)
            await interaction.followup.send(embed=summary_embed, ephemeral=True)
            
            # Pošli každého kandidáta zvlášť
            for candidate in breakdown:
                name = candidate['candidate_name']
                vote_count = candidate['vote_count']
                votes_data = candidate['votes'] or []
                
                candidate_embed = discord.Embed(
                    title=f"{name}",
                    color=discord.Color.green() if vote_count > 0 else discord.Color.red()
                )
                
                if vote_count == 0:
                    candidate_embed.add_field(name="Hlasy", value="*Žádné hlasy*", inline=False)
                else:
                    voters_list = []
                    for vote_data in votes_data:
                        # vote_data je už dict z databáze
                        vote_id = vote_data['vote_id']
                        user_id = vote_data['user_id']
                        voters_list.append(f"ID:{vote_id} <@{user_id}>")
                    
                    # Rozdělí na stránky po 15 hlasech
                    per_page = 15
                    total_pages = (len(voters_list) + per_page - 1) // per_page
                    
                    for page in range(total_pages):
                        start_idx = page * per_page
                        end_idx = min(start_idx + per_page, len(voters_list))
                        page_voters = voters_list[start_idx:end_idx]
                        
                        page_embed = discord.Embed(
                            title=f"{name}" + (f" (strana {page + 1}/{total_pages})" if total_pages > 1 else ""),
                            color=discord.Color.green()
                        )
                        page_embed.add_field(
                            name=f"Hlasy ({vote_count} celkem)",
                            value="\n".join(page_voters),
                            inline=False
                        )
                        
                        if page == total_pages - 1:  # Poslední stránka
                            page_embed.set_footer(text="💡 Použij !odstranit_hlas <ID> pro odstranění konkrétního hlasu")
                        
                        await interaction.followup.send(embed=page_embed, ephemeral=True)
        else:
            # Embed se vejde do jedné zprávy
            await interaction.followup.send(embed=embed, ephemeral=True)
            
    except Exception as e:
        print(f"Chyba při zobrazování detailního přehledu: {e}")
        try:
            await interaction.followup.send(f"❌ Chyba při načítání detailního přehledu: {e}", ephemeral=True)
        except:
            pass

@bot.command()
@commands.has_permissions(administrator=True)
async def seznam_kandidatu(ctx):
    """Zobrazí seznam všech kandidátů"""
    try:
        current_type = await get_current_election_type(ctx.guild.id)
        
        async def _get_candidates():
            async with db_manager.pool.acquire() as conn:
                rows = await conn.fetch('''
                    SELECT id, name, created_at FROM rp_candidates 
                    WHERE guild_id = $1 AND election_type = $2
                    ORDER BY created_at
                ''', ctx.guild.id, current_type)
                return [dict(row) for row in rows]
        
        candidates = await safe_db_operation("get_all_candidates", _get_candidates, [])
        
        if not candidates:
            await ctx.send(f"❌ Nejsou zaregistrováni žádní {'kandidáti' if current_type == 'presidential' else 'strany'}!")
            return
        
        title = f"📋 {'Kandidáti' if current_type == 'presidential' else 'Strany'} ({current_type.capitalize()})"
        embed = discord.Embed(title=title, color=discord.Color.blue())
        
        for candidate in candidates:
            created = candidate['created_at'].strftime('%d.%m.%Y')
            embed.add_field(
                name=f"ID: {candidate['id']}",
                value=f"**{candidate['name']}**\nPřidán: {created}",
                inline=True
            )
        
        embed.set_footer(text=f"Celkem: {len(candidates)} {'kandidátů' if current_type == 'presidential' else 'stran'}")
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Chyba při načítání seznamu: {e}")

@bot.command()
@commands.has_permissions(administrator=True)
async def smazat_kandidata(ctx, candidate_id: int):
    """Smaže kandidáta podle ID"""
    try:
        async def _delete_candidate():
            async with db_manager.pool.acquire() as conn:
                # Získej jméno kandidáta
                candidate = await conn.fetchrow('''
                    SELECT name FROM rp_candidates 
                    WHERE id = $1 AND guild_id = $2
                ''', candidate_id, ctx.guild.id)
                
                if not candidate:
                    return None
                
                # Smaž hlasy
                await conn.execute('DELETE FROM rp_votes WHERE candidate_id = $1', candidate_id)
                # Smaž kandidáta
                await conn.execute('DELETE FROM rp_candidates WHERE id = $1', candidate_id)
                
                return candidate['name']
        
        result = await safe_db_operation("delete_candidate", _delete_candidate)
        
        if result:
            await ctx.send(f"✅ Kandidát **{result}** byl smazán spolu se všemi hlasy!")
        else:
            await ctx.send("❌ Kandidát s tímto ID nebyl nalezen!")
            
    except Exception as e:
        await ctx.send(f"❌ Chyba při mazání kandidáta: {e}")

@bot.command()
@commands.has_permissions(administrator=True)
async def nastavit_ui(ctx, ui_type: str):
    """Nastaví typ UI pro hlasování: buttons nebo dropdown"""
    if ui_type.lower() not in ['buttons', 'dropdown']:
        await ctx.send("❌ Neplatný typ UI! Použij: `buttons` nebo `dropdown`")
        return
    
    async def _set_ui():
        async with db_manager.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO rp_election_settings (guild_id, voting_ui, updated_at)
                VALUES ($1, $2, CURRENT_TIMESTAMP)
                ON CONFLICT (guild_id) DO UPDATE SET
                voting_ui = EXCLUDED.voting_ui, updated_at = CURRENT_TIMESTAMP
            ''', ctx.guild.id, ui_type.lower())
            
            # Invalidate cache
            cache_key = f"voting_ui_{ctx.guild.id}"
            election_cache.cache.pop(cache_key, None)
    
    await safe_db_operation("set_voting_ui", _set_ui)
    await ctx.send(f"✅ UI pro hlasování nastaveno na: **{ui_type.capitalize()}**")

@bot.command()
@commands.has_permissions(administrator=True)
async def vynulovat_volby(ctx):
    """Vymaže všechny hlasy - kandidáti zůstávají zachováni"""
    try:
        # Potvrzovací zpráva
        embed = discord.Embed(
            title="⚠️ Potvrzení",
            description="Opravdu chceš vymazat **všechny hlasy**?\nKandidáti zůstanou zachováni.",
            color=discord.Color.orange()
        )
        
        class ConfirmView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=30)
            
            @discord.ui.button(label="✅ Ano", style=discord.ButtonStyle.danger)
            async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user != ctx.author:
                    await interaction.response.send_message("❌ Pouze autor příkazu může potvrdit!", ephemeral=True)
                    return
                
                async def _reset_votes():
                    async with db_manager.pool.acquire() as conn:
                        result = await conn.execute('DELETE FROM rp_votes WHERE guild_id = $1', ctx.guild.id)
                        return result
                
                await safe_db_operation("reset_votes", _reset_votes)
                await interaction.response.edit_message(
                    content="✅ Všechny hlasy byly vymazány!",
                    embed=None,
                    view=None
                )
            
            @discord.ui.button(label="❌ Ne", style=discord.ButtonStyle.secondary)
            async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user != ctx.author:
                    await interaction.response.send_message("❌ Pouze autor příkazu může zrušit!", ephemeral=True)
                    return
                
                await interaction.response.edit_message(
                    content="❌ Akce zrušena.",
                    embed=None,
                    view=None
                )
        
        await ctx.send(embed=embed, view=ConfirmView())
        
    except Exception as e:
        await ctx.send(f"❌ Chyba při nulování voleb: {e}")

@bot.command()
@commands.has_permissions(administrator=True)
async def odstranit_hlas(ctx, vote_id: int):
    """Odstraní individuální hlas podle ID"""
    try:
        async def _remove_vote():
            async with db_manager.pool.acquire() as conn:
                # Najdi hlas s tímto ID na tomto serveru
                vote = await conn.fetchrow('''
                    SELECT v.id, v.user_id, v.voted_at, c.name as candidate_name 
                    FROM rp_votes v
                    JOIN rp_candidates c ON v.candidate_id = c.id
                    WHERE v.id = $1 AND v.guild_id = $2
                ''', vote_id, ctx.guild.id)
                
                if not vote:
                    return None
                
                # Smaž hlas
                await conn.execute('DELETE FROM rp_votes WHERE id = $1', vote_id)
                return vote
        
        result = await safe_db_operation("remove_vote", _remove_vote)
        
        if result:
            user_mention = f"<@{result['user_id']}>"
            embed = discord.Embed(
                title="✅ Hlas odstraněn",
                color=discord.Color.green()
            )
            embed.add_field(name="ID hlasu", value=str(vote_id), inline=True)
            embed.add_field(name="Uživatel", value=user_mention, inline=True)
            embed.add_field(name="Kandidát", value=result['candidate_name'], inline=True)
            embed.add_field(name="Čas hlasování", value=format_timestamp(result['voted_at']), inline=False)
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ Hlas s ID `{vote_id}` nebyl nalezen na tomto serveru.")
            
    except Exception as e:
        await ctx.send(f"❌ Chyba při mazání hlasu: {e}")

@bot.command()
@commands.has_permissions(administrator=True)
async def seznam_hlasu(ctx, candidate_id: int = None):
    """Zobrazí seznam všech hlasů s IDs (nebo pro konkrétního kandidáta)"""
    try:
        current_type = await get_current_election_type(ctx.guild.id)
        
        async def _get_votes():
            async with db_manager.pool.acquire() as conn:
                if candidate_id:
                    # Hlasy pro konkrétního kandidáta
                    votes = await conn.fetch('''
                        SELECT v.id, v.user_id, v.voted_at, c.name as candidate_name
                        FROM rp_votes v
                        JOIN rp_candidates c ON v.candidate_id = c.id
                        WHERE v.guild_id = $1 AND c.id = $2 AND c.election_type = $3
                        ORDER BY v.voted_at DESC
                    ''', ctx.guild.id, candidate_id, current_type)
                else:
                    # Všechny hlasy
                    votes = await conn.fetch('''
                        SELECT v.id, v.user_id, v.voted_at, c.name as candidate_name
                        FROM rp_votes v
                        JOIN rp_candidates c ON v.candidate_id = c.id
                        WHERE v.guild_id = $1 AND c.election_type = $2
                        ORDER BY c.name, v.voted_at DESC
                    ''', ctx.guild.id, current_type)
                
                return [dict(vote) for vote in votes]
        
        votes = await safe_db_operation("get_votes_list", _get_votes, [])
        
        if not votes:
            await ctx.send("❌ Žádné hlasy nebyly nalezeny.")
            return
        
        # Rozdělí hlasy na stránky (Discord limit 25 fieldů per embed)
        per_page = 20
        total_pages = (len(votes) + per_page - 1) // per_page
        
        for page in range(total_pages):
            start_idx = page * per_page
            end_idx = min(start_idx + per_page, len(votes))
            page_votes = votes[start_idx:end_idx]
            
            title = f"📋 Seznam hlasů"
            if candidate_id:
                title += f" pro kandidáta #{candidate_id}"
            if total_pages > 1:
                title += f" (strana {page + 1}/{total_pages})"
            
            embed = discord.Embed(title=title, color=discord.Color.blue())
            
            for vote in page_votes:
                user_mention = f"<@{vote['user_id']}>"
                value = f"👤 {user_mention}\n🗳️ {vote['candidate_name']}\n⏰ {format_timestamp(vote['voted_at'])}"
                embed.add_field(
                    name=f"ID: {vote['id']}", 
                    value=value, 
                    inline=True
                )
            
            embed.set_footer(text=f"Celkem hlasů: {len(votes)} • Použij !odstranit_hlas <ID> pro smazání")
            await ctx.send(embed=embed)
            
    except Exception as e:
        await ctx.send(f"❌ Chyba při načítání hlasů: {e}")

@bot.command()
async def help_panel(ctx):
    embed = discord.Embed(title="📋 Dostupné příkazy", color=discord.Color.blue())
    
    embed.add_field(name="⚙️ Základní nastavení", value="""
`!set_logs #kanál` - Nastaví kanál pro logy
`!set_welcome #kanál zpráva` - Nastaví welcome zprávu  
`!set_goodbye #kanál zpráva` - Nastaví goodbye zprávu
`!toggle_invites` - Zapne/vypne invite tracking
`!toggle_log <typ>` - Zapne/vypne typ logování
`!log_status` - Zobrazí stav všech log nastavení
`!log_enable_all` - Zapne všechny typy logování
`!log_disable_all` - Vypne všechny rozšířené logy
`!log_reset` - Obnoví výchozí nastavení logů
`!bot_health` - Zobrazí health status bota
`!db_test` - Test databázového připojení
`!cleanup_cache` - Vyčistí cache paměť
    """, inline=False)
    
    embed.add_field(name="🎫 Ticket systém", value="""
`!ticket_help` - Kompletní nápověda pro tickety
`!ticket setup` - Interaktivní nastavení ticketů
`!ticket panel [text]` - Vytvoří ticket panel
`!ticket settings` - Zobrazí nastavení ticketů
`!ticket add_button název zpráva` - Přidá tlačítko
`!ticket mod_role @role` - Nastaví moderátorskou roli
`!ticket admin_role @role` - Přidá admin roli
`!ticket remove_admin_role @role` - Odebere admin roli
`!ticket transcript #kanál` - Nastaví kanál pro transcripty
`!ticket ui buttons/menu` - Přepne typ UI
`!ticket close [důvod]` - Zavře ticket (v ticket kanálu)
    """, inline=False)
    
    embed.add_field(name="🗳️ RP Volby", value="""
**Pro všechny:**
`!volit` - Zobrazí hlasovací menu (min. 14 dní na serveru)
**Pro adminy:**
`!nastavit_volby presidential/parliamentary` - Typ voleb
`!pridat_kandidata název` - Přidá kandidáta/stranu
`!upravit_kandidata ID nový_název` - Upraví kandidáta
`!smazat_kandidata ID` - Smaže kandidáta
`!seznam_kandidatu` - Seznam všech kandidátů
`!nastavit_ui buttons/dropdown` - UI pro hlasování
`!vysledky` - Výsledky s grafem 📊
`!odstranit_hlas <ID>` - Odstraní individuální hlas
`!vynulovat_volby` - Vymaže všechny hlasy
    """, inline=False)
    
    embed.add_field(name="ℹ️ Užitečné", value="""
`!help_panel` - Tato nápověda
Všechny logy obsahují detailní audit trail
Bot automaticky sleduje invite tracking a změny
    """, inline=False)
    
    await ctx.send(embed=embed)

# Event handlers - základní pro invite tracking
@bot.event
async def on_member_join(member):
    guild = member.guild
    data = await get_guild_settings(guild.id)
    
    # Welcome zpráva
    welcome_channel_id = data.get("welcome_channel")
    if welcome_channel_id:
        channel = guild.get_channel(welcome_channel_id)
        if channel:
            message = data.get("welcome_msg", "Vítej na serveru, {user}!")
            await channel.send(message.replace("{user}", member.mention))
    
    # Basic invite tracking
    if not data.get("invite_tracking", True):
        return
    
    try:
        current_invites = await guild.invites()
        current_uses = {invite.code: invite.uses for invite in current_invites}
        cached_uses = invite_cache.get(guild.id, {})
        
        for code, uses in current_uses.items():
            if code in cached_uses and uses > cached_uses[code]:
                # Toto je použitá pozvánka
                for invite in current_invites:
                    if invite.code == code:
                        embed = discord.Embed(
                            title="👋 Nový člen se připojil", 
                            color=discord.Color.green()
                        )
                        embed.add_field(name="Člen", value=f"{member} (ID: {member.id})", inline=False)
                        embed.add_field(name="Pozval", value=f"{invite.inviter}", inline=True)
                        embed.add_field(name="Pozvánka", value=f"`{invite.code}`", inline=True)
                        embed.timestamp = datetime.now(timezone.utc)
                        await send_log(guild, embed)
                        break
                break
        
        # Aktualizuj cache
        invite_cache[guild.id] = current_uses
        
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"⚠️ Chyba při invite trackingu: {e}")

@bot.event
async def on_member_remove(member):
    guild = member.guild
    data = await get_guild_settings(guild.id)
    
    # Goodbye zpráva
    goodbye_channel_id = data.get("goodbye_channel")
    if goodbye_channel_id:
        channel = guild.get_channel(goodbye_channel_id)
        if channel:
            message = data.get("goodbye_msg", "{user} opustil server.")
            await channel.send(message.replace("{user}", member.name))
    
    # Log odchodu
    embed = discord.Embed(title="📤 Člen odešel", color=discord.Color.red())
    embed.add_field(name="Uživatel", value=f"{member} (ID: {member.id})", inline=False)
    embed.add_field(name="Připojen", value=format_timestamp(member.joined_at), inline=True)
    embed.timestamp = datetime.now(timezone.utc)
    await send_log(guild, embed)

# Základní message delete/edit tracking
@bot.event
async def on_message_delete(message):
    if message.guild and not message.author.bot:
        embed = discord.Embed(title="🗑️ Zpráva smazána", color=discord.Color.red())
        embed.add_field(name="Autor", value=f"{message.author.mention} ({message.author})", inline=False)
        embed.add_field(name="Kanál", value=message.channel.mention, inline=True)
        
        content = message.content or "Bez textového obsahu"
        if len(content) > 1024:
            content = content[:1021] + "..."
        embed.add_field(name="Obsah", value=content, inline=False)
        
        embed.timestamp = datetime.now(timezone.utc)
        await send_log(message.guild, embed)

@bot.event
async def on_message_edit(before, after):
    if before.guild and before.content != after.content and not before.author.bot:
        embed = discord.Embed(title="✏️ Zpráva upravena", color=discord.Color.orange())
        embed.add_field(name="Autor", value=f"{before.author.mention} ({before.author})", inline=False)
        embed.add_field(name="Kanál", value=before.channel.mention, inline=True)
        
        old_content = before.content or "Prázdné"
        new_content = after.content or "Prázdné"
        
        if len(old_content) > 512:
            old_content = old_content[:509] + "..."
        if len(new_content) > 512:
            new_content = new_content[:509] + "..."
        
        embed.add_field(name="Před", value=old_content, inline=False)
        embed.add_field(name="Po", value=new_content, inline=False)
        embed.add_field(name="Odkaz", value=f"[Přejít na zprávu]({after.jump_url})", inline=True)
        
        embed.timestamp = datetime.now(timezone.utc)
        await send_log(before.guild, embed)

# Základní ban/unban/kick tracking
@bot.event
async def on_member_ban(guild, user):
    executor, reason = await get_audit_executor(guild, discord.AuditLogAction.ban, user.id, 'user')
    embed = discord.Embed(title="🔨 Uživatel zabanován", color=discord.Color.dark_red())
    embed.add_field(name="Uživatel", value=f"{user} (ID: {user.id})", inline=False)
    embed.set_thumbnail(url=user.display_avatar.url)
    if executor:
        embed.set_footer(text=f"Zabanoval: {executor}")
    if reason:
        embed.add_field(name="Důvod", value=reason, inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    await send_log(guild, embed)

@bot.event
async def on_member_unban(guild, user):
    executor, reason = await get_audit_executor(guild, discord.AuditLogAction.unban, user.id, 'user')
    embed = discord.Embed(title="🎯 Ban odebrán", color=discord.Color.green())
    embed.add_field(name="Uživatel", value=f"{user} (ID: {user.id})", inline=False)
    embed.set_thumbnail(url=user.display_avatar.url)
    if executor:
        embed.set_footer(text=f"Unbanoval: {executor}")
    if reason:
        embed.add_field(name="Důvod", value=reason, inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    await send_log(guild, embed)

# Role change tracking
@bot.event
async def on_member_update(before, after):
    if before.roles != after.roles:
        added_roles = set(after.roles) - set(before.roles)
        removed_roles = set(before.roles) - set(after.roles)
        
        if added_roles or removed_roles:
            executor, reason = await get_audit_executor(after.guild, discord.AuditLogAction.member_role_update, after.id, 'user')
            embed = discord.Embed(title="👤 Role změněny", color=discord.Color.orange())
            embed.add_field(name="Uživatel", value=after.mention, inline=False)
            
            if added_roles:
                embed.add_field(name="➕ Přidané role", value=", ".join([role.mention for role in added_roles]), inline=False)
            if removed_roles:
                embed.add_field(name="➖ Odebrané role", value=", ".join([role.mention for role in removed_roles]), inline=False)
            
            if executor:
                embed.set_footer(text=f"Změnil: {executor}")
            if reason:
                embed.add_field(name="Důvod", value=reason, inline=False)
            embed.timestamp = datetime.now(timezone.utc)
            await send_log(after.guild, embed)

# Channel events
@bot.event
async def on_guild_channel_create(channel):
    executor, reason = await get_audit_executor(channel.guild, discord.AuditLogAction.channel_create, channel.id)
    embed = discord.Embed(title="📥 Kanál vytvořen", color=discord.Color.green())
    embed.add_field(name="Kanál", value=f"{channel.mention} ({channel.name})", inline=False)
    embed.add_field(name="Typ", value=str(channel.type), inline=True)
    embed.add_field(name="ID", value=str(channel.id), inline=True)
    
    if hasattr(channel, 'category') and channel.category:
        embed.add_field(name="Kategorie", value=channel.category.name, inline=True)
    
    # Zobraz permission overwrites pokud existují
    if channel.overwrites:
        perm_info = []
        for target, perms in channel.overwrites.items():
            target_name = target.mention if hasattr(target, 'mention') else str(target)
            perm_info.append(f"• {target_name}: Má custom permissions")
        
        if perm_info:
            perm_text = "\n".join(perm_info)
            if len(perm_text) > 1024:
                perm_text = perm_text[:1021] + "..."
            embed.add_field(name="Custom Permissions", value=perm_text, inline=False)
    
    if executor:
        embed.set_footer(text=f"Vytvořil: {executor}")
    if reason:
        embed.add_field(name="Důvod", value=reason, inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    await send_log(channel.guild, embed)

@bot.event
async def on_guild_channel_delete(channel):
    executor, reason = await get_audit_executor(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
    embed = discord.Embed(title="📤 Kanál smazán", color=discord.Color.red())
    embed.add_field(name="Název", value=channel.name, inline=False)
    embed.add_field(name="Typ", value=str(channel.type), inline=True)
    embed.add_field(name="ID", value=str(channel.id), inline=True)
    
    if hasattr(channel, 'category') and channel.category:
        embed.add_field(name="Kategorie", value=channel.category.name, inline=True)
    
    # Zobraz kdo měl custom permissions v smazaném kanálu
    if channel.overwrites:
        perm_info = []
        for target, perms in channel.overwrites.items():
            target_name = target.mention if hasattr(target, 'mention') else str(target)
            perm_info.append(f"• {target_name}: Měl custom permissions")
        
        if perm_info:
            perm_text = "\n".join(perm_info)
            if len(perm_text) > 1024:
                perm_text = perm_text[:1021] + "..."
            embed.add_field(name="Měli Custom Permissions", value=perm_text, inline=False)
    
    if executor:
        embed.set_footer(text=f"Smazal: {executor}")
    if reason:
        embed.add_field(name="Důvod", value=reason, inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    await send_log(channel.guild, embed)

# Channel update events
@bot.event
async def on_guild_channel_update(before, after):
    settings = await get_guild_settings(after.guild.id)
    if not settings.get("log_channels", True):
        return
        
    if not channel_rate_limiter.can_call(after.guild.id):
        return
        
    if before.name != after.name or before.topic != after.topic or before.overwrites != after.overwrites:
        embed = discord.Embed(title="📝 Kanál upraven", color=discord.Color.orange())
        embed.add_field(name="Kanál", value=after.mention, inline=True)
        embed.add_field(name="ID", value=str(after.id), inline=True)
        
        if before.name != after.name:
            embed.add_field(name="Název změněn", value=f"{before.name} → {after.name}", inline=False)
        
        if hasattr(before, 'topic') and hasattr(after, 'topic') and before.topic != after.topic:
            old_topic = before.topic or "Žádný"
            new_topic = after.topic or "Žádný" 
            embed.add_field(name="Topic změněn", value=f"{old_topic} → {new_topic}"[:1024], inline=False)
        
        if before.overwrites != after.overwrites:
            # Detailní analýza permission overwrites
            perm_changes = []
            
            # Najdi všechny targets (role/uživatele) které se změnily
            all_targets = set(before.overwrites.keys()) | set(after.overwrites.keys())
            
            for target in all_targets:
                before_perms = before.overwrites.get(target)
                after_perms = after.overwrites.get(target)
                
                # Nový permission overwrite
                if before_perms is None and after_perms is not None:
                    target_name = target.mention if hasattr(target, 'mention') else str(target)
                    perm_changes.append(f"➕ **{target_name}**: Přidán permission overwrite")
                
                # Odstraněný permission overwrite
                elif before_perms is not None and after_perms is None:
                    target_name = target.mention if hasattr(target, 'mention') else str(target)
                    perm_changes.append(f"➖ **{target_name}**: Odebrán permission overwrite")
                
                # Změněný permission overwrite
                elif before_perms != after_perms:
                    target_name = target.mention if hasattr(target, 'mention') else str(target)
                    
                    # Analyzuj konkrétní změny
                    allowed_changes = []
                    denied_changes = []
                    
                    # Porovnej allow permissions
                    before_allow = before_perms.pair()[0] if before_perms else discord.Permissions.none()
                    after_allow = after_perms.pair()[0] if after_perms else discord.Permissions.none()
                    
                    before_deny = before_perms.pair()[1] if before_perms else discord.Permissions.none()
                    after_deny = after_perms.pair()[1] if after_perms else discord.Permissions.none()
                    
                    # Seznam permissions pro kanály
                    channel_perms = [
                        ('view_channel', 'View Channel'),
                        ('send_messages', 'Send Messages'),
                        ('send_tts_messages', 'Send TTS Messages'),
                        ('manage_messages', 'Manage Messages'),
                        ('embed_links', 'Embed Links'),
                        ('attach_files', 'Attach Files'),
                        ('read_message_history', 'Read Message History'),
                        ('mention_everyone', 'Mention Everyone'),
                        ('external_emojis', 'Use External Emojis'),
                        ('add_reactions', 'Add Reactions'),
                        ('connect', 'Connect'),
                        ('speak', 'Speak'),
                        ('stream', 'Video'),
                        ('use_voice_activation', 'Use Voice Activity'),
                        ('mute_members', 'Mute Members'),
                        ('deafen_members', 'Deafen Members'),
                        ('move_members', 'Move Members'),
                        ('manage_channels', 'Manage Channel'),
                        ('manage_roles', 'Manage Permissions'),
                        ('manage_webhooks', 'Manage Webhooks'),
                        ('use_slash_commands', 'Use Slash Commands'),
                        ('manage_threads', 'Manage Threads'),
                        ('create_public_threads', 'Create Public Threads'),
                        ('create_private_threads', 'Create Private Threads'),
                        ('send_messages_in_threads', 'Send Messages in Threads'),
                        ('use_embedded_activities', 'Use Activities')
                    ]
                    
                    for perm_attr, perm_name in channel_perms:
                        if hasattr(before_allow, perm_attr):
                            before_allow_val = getattr(before_allow, perm_attr)
                            after_allow_val = getattr(after_allow, perm_attr)
                            before_deny_val = getattr(before_deny, perm_attr)
                            after_deny_val = getattr(after_deny, perm_attr)
                            
                            # Allow changes
                            if before_allow_val != after_allow_val:
                                if after_allow_val:
                                    allowed_changes.append(f"✅ {perm_name}")
                                elif before_allow_val:
                                    allowed_changes.append(f"🚫 {perm_name} (odebráno z Allow)")
                            
                            # Deny changes  
                            if before_deny_val != after_deny_val:
                                if after_deny_val:
                                    denied_changes.append(f"❌ {perm_name}")
                                elif before_deny_val:
                                    denied_changes.append(f"🚫 {perm_name} (odebráno z Deny)")
                    
                    change_details = []
                    if allowed_changes:
                        change_details.append(f"Allow: {', '.join(allowed_changes)}")
                    if denied_changes:
                        change_details.append(f"Deny: {', '.join(denied_changes)}")
                    
                    if change_details:
                        perm_changes.append(f"🔄 **{target_name}**: {' | '.join(change_details)}")
            
            if perm_changes:
                # Pokud je změn moc, rozdělíme je na více fieldů
                perm_text = "\n".join(perm_changes)
                if len(perm_text) > 1024:
                    # Rozdělíme permission změny na více fieldů
                    for i, change in enumerate(perm_changes):
                        if len(change) > 1024:
                            # Pokud je i jednotlivá změna moc dlouhá, zkrátíme ji
                            change = change[:1021] + "..."
                        embed.add_field(name=f"Permission změna {i+1}", value=change, inline=False)
                else:
                    embed.add_field(name="Permission změny", value=perm_text, inline=False)
            else:
                embed.add_field(name="Oprávnění změněna", value="Permission overwrites byly upraveny", inline=False)
        
        embed.timestamp = datetime.now(timezone.utc)
        await send_log(after.guild, embed)

# Role events
@bot.event
async def on_guild_role_create(role):
    settings = await get_guild_settings(role.guild.id)
    if not settings.get("log_roles", True):
        return
        
    if not role_rate_limiter.can_call(role.guild.id):
        return
    executor, reason = await get_audit_executor(role.guild, discord.AuditLogAction.role_create, role.id)
    embed = discord.Embed(title="🎭 Role vytvořena", color=discord.Color.green())
    embed.add_field(name="Role", value=role.mention, inline=True)
    embed.add_field(name="Název", value=role.name, inline=True)
    embed.add_field(name="ID", value=str(role.id), inline=True)
    embed.add_field(name="Barva", value=str(role.color), inline=True)
    embed.add_field(name="Pozice", value=str(role.position), inline=True)
    embed.add_field(name="Zmíněno", value="Ano" if role.mentionable else "Ne", inline=True)
    if executor:
        embed.set_footer(text=f"Vytvořil: {executor}")
    if reason:
        embed.add_field(name="Důvod", value=reason, inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    await send_log(role.guild, embed)

@bot.event
async def on_guild_role_delete(role):
    settings = await get_guild_settings(role.guild.id)
    if not settings.get("log_roles", True):
        return
        
    if not role_rate_limiter.can_call(role.guild.id):
        return
    executor, reason = await get_audit_executor(role.guild, discord.AuditLogAction.role_delete, role.id)
    embed = discord.Embed(title="🗑️ Role smazána", color=discord.Color.red())
    embed.add_field(name="Název", value=role.name, inline=True)
    embed.add_field(name="ID", value=str(role.id), inline=True)
    embed.add_field(name="Barva", value=str(role.color), inline=True)
    if executor:
        embed.set_footer(text=f"Smazal: {executor}")
    if reason:
        embed.add_field(name="Důvod", value=reason, inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    await send_log(role.guild, embed)

@bot.event
async def on_guild_role_update(before, after):
    if not role_rate_limiter.can_call(after.guild.id):
        return
    changes = []
    if before.name != after.name:
        changes.append(f"Název: {before.name} → {after.name}")
    if before.color != after.color:
        changes.append(f"Barva: {before.color} → {after.color}")
    if before.mentionable != after.mentionable:
        changes.append(f"Zmíněno: {'Ano' if before.mentionable else 'Ne'} → {'Ano' if after.mentionable else 'Ne'}")
    
    # Detailní tracking permissions
    if before.permissions != after.permissions:
        added_perms = []
        removed_perms = []
        
        # Všechna možná oprávnění
        all_perms = [
            ('create_instant_invite', 'Create Invite'),
            ('kick_members', 'Kick Members'),
            ('ban_members', 'Ban Members'),
            ('administrator', 'Administrator'),
            ('manage_channels', 'Manage Channels'),
            ('manage_guild', 'Manage Server'),
            ('add_reactions', 'Add Reactions'),
            ('view_audit_log', 'View Audit Log'),
            ('priority_speaker', 'Priority Speaker'),
            ('stream', 'Video'),
            ('read_messages', 'View Channels'),
            ('send_messages', 'Send Messages'),
            ('send_tts_messages', 'Send TTS Messages'),
            ('manage_messages', 'Manage Messages'),
            ('embed_links', 'Embed Links'),
            ('attach_files', 'Attach Files'),
            ('read_message_history', 'Read Message History'),
            ('mention_everyone', 'Mention Everyone'),
            ('external_emojis', 'Use External Emojis'),
            ('view_guild_insights', 'View Server Insights'),
            ('connect', 'Connect'),
            ('speak', 'Speak'),
            ('mute_members', 'Mute Members'),
            ('deafen_members', 'Deafen Members'),
            ('move_members', 'Move Members'),
            ('use_voice_activation', 'Use Voice Activity'),
            ('change_nickname', 'Change Nickname'),
            ('manage_nicknames', 'Manage Nicknames'),
            ('manage_roles', 'Manage Roles'),
            ('manage_webhooks', 'Manage Webhooks'),
            ('manage_emojis', 'Manage Emojis'),
            ('use_slash_commands', 'Use Slash Commands'),
            ('request_to_speak', 'Request to Speak'),
            ('manage_events', 'Manage Events'),
            ('manage_threads', 'Manage Threads'),
            ('create_public_threads', 'Create Public Threads'),
            ('create_private_threads', 'Create Private Threads'),
            ('external_stickers', 'Use External Stickers'),
            ('send_messages_in_threads', 'Send Messages in Threads'),
            ('use_embedded_activities', 'Use Activities'),
            ('moderate_members', 'Timeout Members')
        ]
        
        for perm_attr, perm_name in all_perms:
            if hasattr(before.permissions, perm_attr) and hasattr(after.permissions, perm_attr):
                before_val = getattr(before.permissions, perm_attr)
                after_val = getattr(after.permissions, perm_attr)
                
                if before_val != after_val:
                    if after_val:
                        added_perms.append(perm_name)
                    else:
                        removed_perms.append(perm_name)
        
        if added_perms:
            changes.append(f"➕ Přidána oprávnění: {', '.join(added_perms)}")
        if removed_perms:
            changes.append(f"➖ Odebrána oprávnění: {', '.join(removed_perms)}")
    
    if changes:
        embed = discord.Embed(title="🎭 Role upravena", color=discord.Color.orange())
        embed.add_field(name="Role", value=after.mention, inline=True)
        embed.add_field(name="ID", value=str(after.id), inline=True)
        
        # Rozdělíme změny na více fieldů pokud je jich hodně
        changes_text = "\n".join(changes)
        if len(changes_text) > 1024:
            # Rozdělíme na více fieldů
            for i, change in enumerate(changes):
                embed.add_field(name=f"Změna {i+1}", value=change[:1024], inline=False)
        else:
            embed.add_field(name="Změny", value=changes_text, inline=False)
            
        embed.timestamp = datetime.now(timezone.utc)
        await send_log(after.guild, embed)

# Emoji events
@bot.event
async def on_guild_emojis_update(guild, before, after):
    added_emojis = set(after) - set(before)
    removed_emojis = set(before) - set(after)
    
    for emoji in added_emojis:
        embed = discord.Embed(title="😀 Emoji přidáno", color=discord.Color.green())
        embed.add_field(name="Emoji", value=str(emoji), inline=True)
        embed.add_field(name="Název", value=emoji.name, inline=True)
        embed.add_field(name="ID", value=str(emoji.id), inline=True)
        embed.add_field(name="Animované", value="Ano" if emoji.animated else "Ne", inline=True)
        embed.timestamp = datetime.now(timezone.utc)
        await send_log(guild, embed)
    
    for emoji in removed_emojis:
        embed = discord.Embed(title="🗑️ Emoji odstraněno", color=discord.Color.red())
        embed.add_field(name="Název", value=emoji.name, inline=True)
        embed.add_field(name="ID", value=str(emoji.id), inline=True)
        embed.add_field(name="Animované", value="Ano" if emoji.animated else "Ne", inline=True)
        embed.timestamp = datetime.now(timezone.utc)
        await send_log(guild, embed)

# Reaction events
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot or not reaction.message.guild:
        return
        
    settings = await get_guild_settings(reaction.message.guild.id)
    if not settings.get("log_reactions", False):
        return
    
    if not reaction_rate_limiter.can_call(reaction.message.guild.id):
        return
    
    embed = discord.Embed(title="👍 Reakce přidána", color=discord.Color.green())
    embed.add_field(name="Uživatel", value=user.mention, inline=True)
    embed.add_field(name="Reakce", value=str(reaction.emoji), inline=True)
    embed.add_field(name="Kanál", value=reaction.message.channel.mention, inline=True)
    embed.add_field(name="Zpráva", value=f"[Přejít na zprávu]({reaction.message.jump_url})", inline=False)
    
    content = reaction.message.content[:100] + "..." if len(reaction.message.content) > 100 else reaction.message.content
    if content:
        embed.add_field(name="Obsah zprávy", value=content, inline=False)
    
    embed.timestamp = datetime.now(timezone.utc)
    await send_log(reaction.message.guild, embed)

@bot.event
async def on_reaction_remove(reaction, user):
    if user.bot or not reaction.message.guild:
        return
    
    if not reaction_rate_limiter.can_call(reaction.message.guild.id):
        return
    
    embed = discord.Embed(title="👎 Reakce odstraněna", color=discord.Color.red())
    embed.add_field(name="Uživatel", value=user.mention, inline=True)
    embed.add_field(name="Reakce", value=str(reaction.emoji), inline=True)
    embed.add_field(name="Kanál", value=reaction.message.channel.mention, inline=True)
    embed.add_field(name="Zpráva", value=f"[Přejít na zprávu]({reaction.message.jump_url})", inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    await send_log(reaction.message.guild, embed)

# Voice events
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
        
    if not voice_rate_limiter.can_call(member.guild.id):
        return
        
    guild = member.guild
    embed = None
    
    # Připojení
    if before.channel is None and after.channel is not None:
        embed = discord.Embed(title="🔊 Připojen k voice", color=discord.Color.green())
        embed.add_field(name="Uživatel", value=member.mention, inline=True)
        embed.add_field(name="Kanál", value=after.channel.name, inline=True)
    
    # Odpojení 
    elif before.channel is not None and after.channel is None:
        embed = discord.Embed(title="🔇 Odpojen z voice", color=discord.Color.red())
        embed.add_field(name="Uživatel", value=member.mention, inline=True)
        embed.add_field(name="Kanál", value=before.channel.name, inline=True)
    
    # Přepnutí kanálu
    elif before.channel != after.channel:
        embed = discord.Embed(title="🔄 Přepnut voice kanál", color=discord.Color.orange())
        embed.add_field(name="Uživatel", value=member.mention, inline=True)
        embed.add_field(name="Z kanálu", value=before.channel.name, inline=True)
        embed.add_field(name="Do kanálu", value=after.channel.name, inline=True)
    
    # Mute/Unmute/Deafen změny
    elif (before.mute != after.mute or before.deaf != after.deaf or 
          before.self_mute != after.self_mute or before.self_deaf != after.self_deaf):
        changes = []
        if before.mute != after.mute:
            changes.append(f"Server mute: {'Ano' if after.mute else 'Ne'}")
        if before.deaf != after.deaf:
            changes.append(f"Server deaf: {'Ano' if after.deaf else 'Ne'}")
        if before.self_mute != after.self_mute:
            changes.append(f"Self mute: {'Ano' if after.self_mute else 'Ne'}")
        if before.self_deaf != after.self_deaf:
            changes.append(f"Self deaf: {'Ano' if after.self_deaf else 'Ne'}")
        
        if changes:
            embed = discord.Embed(title="🎤 Voice stav změněn", color=discord.Color.orange())
            embed.add_field(name="Uživatel", value=member.mention, inline=True)
            embed.add_field(name="Kanál", value=after.channel.name if after.channel else "Žádný", inline=True)
            embed.add_field(name="Změny", value="\n".join(changes), inline=False)
    
    if embed:
        embed.timestamp = datetime.now(timezone.utc)
        await send_log(guild, embed)

# Thread events
@bot.event
async def on_thread_create(thread):
    if not thread_rate_limiter.can_call(thread.guild.id):
        return
    embed = discord.Embed(title="🧵 Thread vytvořen", color=discord.Color.green())
    embed.add_field(name="Thread", value=thread.mention, inline=True)
    embed.add_field(name="Název", value=thread.name, inline=True)
    embed.add_field(name="ID", value=str(thread.id), inline=True)
    embed.add_field(name="Rodičovský kanál", value=thread.parent.mention if thread.parent else "Neznámý", inline=True)
    embed.add_field(name="Typ", value=str(thread.type), inline=True)
    if hasattr(thread, 'owner') and thread.owner:
        embed.add_field(name="Vytvořil", value=thread.owner.mention, inline=True)
    embed.timestamp = datetime.now(timezone.utc)
    await send_log(thread.guild, embed)

@bot.event
async def on_thread_delete(thread):
    if not thread_rate_limiter.can_call(thread.guild.id):
        return
    embed = discord.Embed(title="🗑️ Thread smazán", color=discord.Color.red())
    embed.add_field(name="Název", value=thread.name, inline=True)
    embed.add_field(name="ID", value=str(thread.id), inline=True)
    embed.add_field(name="Rodičovský kanál", value=thread.parent.mention if thread.parent else "Neznámý", inline=True)
    embed.timestamp = datetime.now(timezone.utc)
    await send_log(thread.guild, embed)

@bot.event
async def on_thread_update(before, after):
    if not thread_rate_limiter.can_call(after.guild.id):
        return
    changes = []
    if before.name != after.name:
        changes.append(f"Název: {before.name} → {after.name}")
    if before.archived != after.archived:
        changes.append(f"Archivován: {'Ano' if after.archived else 'Ne'}")
    if before.locked != after.locked:
        changes.append(f"Zamčen: {'Ano' if after.locked else 'Ne'}")
    
    if changes:
        embed = discord.Embed(title="🧵 Thread upraven", color=discord.Color.orange())
        embed.add_field(name="Thread", value=after.mention, inline=True)
        embed.add_field(name="ID", value=str(after.id), inline=True)
        embed.add_field(name="Změny", value="\n".join(changes), inline=False)
        embed.timestamp = datetime.now(timezone.utc)
        await send_log(after.guild, embed)

# Member nickname changes
@bot.event  
async def on_user_update(before, after):
    # Globální změny uživatele (username, avatar, etc.)
    changes = []
    if before.name != after.name:
        changes.append(f"Username: {before.name} → {after.name}")
    if before.discriminator != after.discriminator:
        changes.append(f"Discriminator: {before.discriminator} → {after.discriminator}")
    if str(before.avatar) != str(after.avatar):
        changes.append("Avatar změněn")
    
    if changes:
        # Pošli log do všech serverů kde je uživatel
        for guild in bot.guilds:
            if guild.get_member(after.id):
                embed = discord.Embed(title="👤 Profil změněn", color=discord.Color.blue())
                embed.add_field(name="Uživatel", value=f"{after.mention}", inline=True)
                embed.add_field(name="ID", value=str(after.id), inline=True)
                embed.add_field(name="Změny", value="\n".join(changes), inline=False)
                embed.timestamp = datetime.now(timezone.utc)
                if after.avatar:
                    embed.set_thumbnail(url=after.avatar.url)
                await send_log(guild, embed)

# Server updates
@bot.event
async def on_guild_update(before, after):
    changes = []
    if before.name != after.name:
        changes.append(f"Název: {before.name} → {after.name}")
    if before.description != after.description:
        old_desc = before.description or "Žádný"
        new_desc = after.description or "Žádný"
        changes.append(f"Popis: {old_desc} → {new_desc}")
    if str(before.icon) != str(after.icon):
        changes.append("Ikona změněna")
    if before.owner != after.owner:
        changes.append(f"Vlastník: {before.owner} → {after.owner}")
    
    if changes:
        embed = discord.Embed(title="🏰 Server upraven", color=discord.Color.blue())
        embed.add_field(name="Server", value=after.name, inline=True)
        embed.add_field(name="ID", value=str(after.id), inline=True)
        embed.add_field(name="Změny", value="\n".join(changes)[:1024], inline=False)
        embed.timestamp = datetime.now(timezone.utc)
        if after.icon:
            embed.set_thumbnail(url=after.icon.url)
        await send_log(after, embed)

keep_alive()
bot.run(os.environ['TOKEN'])