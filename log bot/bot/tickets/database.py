import asyncpg
import json
from typing import Dict, Any, List, Tuple, Optional
from ..database.manager import DatabaseManager
from ..utils.cache import LRUCache
import logging

logger = logging.getLogger(__name__)

class TicketDatabase:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.settings_cache = LRUCache(500)  # Cache pro nastavení
    
    async def get_settings(self, guild_id: int) -> Dict[str, Any]:
        """Async verze get_settings s cachingem"""
        cache_key = f"ticket_settings_{guild_id}"
        cached = self.settings_cache.get(cache_key)
        if cached:
            return cached
        
        async def _get_settings():
            async with self.db.pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT * FROM ticket_settings WHERE guild_id = $1', 
                    guild_id
                )
                
                if row:
                    settings = {
                        "mod_role_id": row['mod_role_id'],
                        "admin_role_ids": json.loads(row['admin_role_ids'] or '[]'),
                        "transcript_channel_id": row['transcript_channel_id'],
                        "custom_buttons": json.loads(row['custom_buttons'] or '[]'),
                        "panel_message": row['panel_message'],
                        "embed_color": row['embed_color'],
                        "use_menu": row['use_menu']
                    }
                else:
                    settings = {
                        "mod_role_id": None,
                        "admin_role_ids": [],
                        "transcript_channel_id": None,
                        "custom_buttons": [],
                        "panel_message": "Kliknutím na tlačítko níže vytvoříš ticket:",
                        "embed_color": 5793266,
                        "use_menu": False
                    }
                    await self.save_settings(guild_id, settings)
                
                self.settings_cache.set(cache_key, settings, 300)  # 5 min cache
                return settings
        
        default_settings = {
            "mod_role_id": None,
            "admin_role_ids": [],
            "transcript_channel_id": None,
            "custom_buttons": [],
            "panel_message": "Kliknutím na tlačítko níže vytvoříš ticket:",
            "embed_color": 5793266,
            "use_menu": False
        }
        
        return await self.db.safe_operation(
            f"get_ticket_settings({guild_id})",
            _get_settings,
            default_settings
        )
    
    async def save_settings(self, guild_id: int, settings: Dict[str, Any]):
        """Async verze save_settings"""
        async def _save_settings():
            async with self.db.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO ticket_settings 
                    (guild_id, mod_role_id, admin_role_ids, transcript_channel_id, 
                     custom_buttons, panel_message, embed_color, use_menu, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, CURRENT_TIMESTAMP)
                    ON CONFLICT (guild_id) 
                    DO UPDATE SET 
                        mod_role_id = EXCLUDED.mod_role_id,
                        admin_role_ids = EXCLUDED.admin_role_ids,
                        transcript_channel_id = EXCLUDED.transcript_channel_id,
                        custom_buttons = EXCLUDED.custom_buttons,
                        panel_message = EXCLUDED.panel_message,
                        embed_color = EXCLUDED.embed_color,
                        use_menu = EXCLUDED.use_menu,
                        updated_at = CURRENT_TIMESTAMP
                ''', 
                    guild_id,
                    settings["mod_role_id"],
                    json.dumps(settings["admin_role_ids"]),
                    settings["transcript_channel_id"],
                    json.dumps(settings["custom_buttons"]),
                    settings["panel_message"],
                    settings["embed_color"],
                    settings["use_menu"]
                )
                
                # Invalidate cache
                cache_key = f"ticket_settings_{guild_id}"
                self.settings_cache.cache.pop(cache_key, None)
        
        await self.db.safe_operation(
            f"save_ticket_settings({guild_id})",
            _save_settings
        )
    
    async def log_ticket_action(self, guild_id: int, user_id: int, ticket_type: str, 
                               action: str, channel_id: int = None, moderator_id: int = None, 
                               reason: str = None):
        """Logování ticket akcí"""
        async def _log_action():
            async with self.db.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO ticket_logs 
                    (guild_id, user_id, ticket_type, action, channel_id, moderator_id, reason)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                ''', guild_id, user_id, ticket_type, action, channel_id, moderator_id, reason)
        
        await self.db.safe_operation(
            f"log_ticket_action({action})",
            _log_action
        )
    
    async def create_active_ticket(self, guild_id: int, user_id: int, 
                                  channel_id: int, ticket_type: str):
        """Vytvoří záznam o aktivním ticketu"""
        async def _create_ticket():
            async with self.db.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO active_tickets (guild_id, user_id, channel_id, ticket_type)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (guild_id, user_id, ticket_type) 
                    DO UPDATE SET 
                        channel_id = EXCLUDED.channel_id,
                        created_at = CURRENT_TIMESTAMP,
                        status = 'open'
                ''', guild_id, user_id, channel_id, ticket_type)
        
        await self.db.safe_operation(
            "create_active_ticket",
            _create_ticket
        )
    
    async def close_active_ticket(self, guild_id: int, user_id: int, ticket_type: str):
        """Zavře aktivní ticket"""
        async def _close_ticket():
            async with self.db.pool.acquire() as conn:
                await conn.execute('''
                    UPDATE active_tickets 
                    SET status = 'closed', closed_at = CURRENT_TIMESTAMP
                    WHERE guild_id = $1 AND user_id = $2 AND ticket_type = $3
                ''', guild_id, user_id, ticket_type)
        
        await self.db.safe_operation(
            "close_active_ticket",
            _close_ticket
        )
    
    async def get_user_active_tickets(self, guild_id: int, user_id: int) -> List[Dict]:
        """Vrátí aktivní tickety uživatele"""
        async def _get_tickets():
            async with self.db.pool.acquire() as conn:
                rows = await conn.fetch('''
                    SELECT * FROM active_tickets 
                    WHERE guild_id = $1 AND user_id = $2 AND status = 'open'
                ''', guild_id, user_id)
                return [dict(row) for row in rows]
        
        return await self.db.safe_operation(
            "get_user_active_tickets",
            _get_tickets,
            []
        )