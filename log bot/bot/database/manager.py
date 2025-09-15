import asyncpg
from typing import Optional, Dict, Any
import asyncio
from ..config.settings import DATABASE_URL
import logging

logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
    
    async def initialize(self):
        """Inicializace s retry logikou z main.py"""
        max_retries = 5
        base_delay = 1
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Pokus o připojení k databázi ({attempt + 1}/{max_retries})")
                self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
                
                async with self.pool.acquire() as conn:
                    await self._create_base_tables(conn)
                    await self._create_ticket_tables(conn)
                
                logger.info("Database úspěšně inicializována!")
                return
                
            except Exception as e:
                logger.error(f"Pokus {attempt + 1} selhal: {e}")
                
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.info(f"Čekám {delay}s před dalším pokusem...")
                    await asyncio.sleep(delay)
                else:
                    logger.error("Všechny pokusy o připojení k databázi selhaly")
                    self.pool = None
    
    async def _create_base_tables(self, conn):
        """Vytvoří základní tabulky z main.py"""
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id BIGINT PRIMARY KEY,
                log_channel BIGINT,
                welcome_channel BIGINT,
                goodbye_channel BIGINT,
                welcome_msg TEXT,
                goodbye_msg TEXT,
                invite_tracking BOOLEAN DEFAULT TRUE,
                log_reactions BOOLEAN DEFAULT FALSE,
                log_voice BOOLEAN DEFAULT TRUE,
                log_threads BOOLEAN DEFAULT TRUE,
                log_roles BOOLEAN DEFAULT TRUE,
                log_channels BOOLEAN DEFAULT TRUE,
                log_emojis BOOLEAN DEFAULT TRUE,
                log_user_updates BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS invites (
                guild_id BIGINT,
                invite_code TEXT,
                inviter_id BIGINT,
                uses INTEGER DEFAULT 0,
                max_uses INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, invite_code)
            )
        ''')
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS invite_uses (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT,
                invite_code TEXT,
                user_id BIGINT,
                inviter_id BIGINT,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # RP volby tabulky...
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS rp_candidates (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                election_type TEXT DEFAULT 'presidential',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by BIGINT NOT NULL
            )
        ''')
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS rp_votes (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                candidate_id INTEGER NOT NULL REFERENCES rp_candidates(id),
                voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, user_id)
            )
        ''')
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS rp_election_settings (
                guild_id BIGINT PRIMARY KEY,
                current_type TEXT DEFAULT 'presidential',
                voting_ui TEXT DEFAULT 'buttons',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
    async def _create_ticket_tables(self, conn):
        """Vytvoří tabulky pro ticket systém"""
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS ticket_settings (
                guild_id BIGINT PRIMARY KEY,
                mod_role_id BIGINT,
                admin_role_ids JSONB DEFAULT '[]',
                transcript_channel_id BIGINT,
                custom_buttons JSONB DEFAULT '[]',
                panel_message TEXT DEFAULT 'Kliknutím na tlačítko níže vytvoříš ticket:',
                embed_color INTEGER DEFAULT 5793266,
                use_menu BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS ticket_logs (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                ticket_type TEXT NOT NULL,
                action TEXT NOT NULL,
                channel_id BIGINT,
                moderator_id BIGINT,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS active_tickets (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                ticket_type TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                UNIQUE(guild_id, user_id, ticket_type)
            )
        ''')
    
    async def safe_operation(self, operation_name: str, operation_func, default_return=None):
        """Safely execute database operation with error handling"""
        if not self.pool:
            logger.warning(f"{operation_name}: Databáze není k dispozici")
            return default_return
        
        try:
            return await operation_func()
        except Exception as e:
            logger.error(f"{operation_name}: {e}")
            return default_return