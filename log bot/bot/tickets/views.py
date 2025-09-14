import discord
from discord.ext import commands
from typing import Dict, List, Tuple
import asyncio
import hashlib
import logging

logger = logging.getLogger(__name__)

class PersistentTicketView(discord.ui.View):
    def __init__(self, guild_id: int, settings: Dict):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.settings = settings
        self.setup_components()
    
    def setup_components(self):
        """Dynamicky vytvoří komponenty na základě nastavení"""
        self.clear_items()
        
        if self.settings.get("use_menu", False):
            self.add_item(TicketSelectMenu(self.guild_id, self.settings))
        else:
            for i, (label, welcome_msg) in enumerate(self.settings.get("custom_buttons", [])):
                if i >= 25:  # Discord limit
                    break
                    
                # Vytvoř konzistentní custom_id
                button_hash = hashlib.md5(f"{self.guild_id}_{label}".encode()).hexdigest()[:8]
                custom_id = f"ticket_{button_hash}"
                
                button = TicketButton(
                    label=label[:80],  # Discord limit
                    custom_id=custom_id,
                    welcome_message=welcome_msg,
                    ticket_type=label
                )
                self.add_item(button)

class TicketSelectMenu(discord.ui.Select):
    def __init__(self, guild_id: int, settings: Dict):
        self.guild_id = guild_id
        self.settings = settings
        
        options = []
        for label, welcome_msg in settings.get("custom_buttons", [])[:25]:  # Discord limit
            options.append(discord.SelectOption(
                label=label[:100],
                description="Klikni pro vytvoření ticketu"[:100],
                value=label
            ))
        
        super().__init__(
            placeholder="Vyber kategorii ticketu...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"ticket_select_{guild_id}"
        )
    
    async def callback(self, interaction: discord.Interaction):
        selected_type = self.values[0]
        welcome_message = None
        
        for label, welcome_msg in self.settings.get("custom_buttons", []):
            if label == selected_type:
                welcome_message = welcome_msg
                break
        
        if welcome_message:
            await self.create_ticket_safe(interaction, selected_type, welcome_message)
        else:
            await interaction.response.send_message(
                "Chyba: Kategorie ticketu nenalezena.", 
                ephemeral=True
            )
    
    async def create_ticket_safe(self, interaction: discord.Interaction, 
                                ticket_type: str, welcome_message: str):
        """Bezpečné vytvoření ticketu"""
        from .manager import TicketManager
        
        try:
            ticket_manager = interaction.client.ticket_manager
            await ticket_manager.create_ticket(
                interaction.guild, 
                interaction.user, 
                ticket_type, 
                welcome_message,
                interaction
            )
        except Exception as e:
            logger.error(f"Chyba při vytváření ticketu: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Nastala chyba při vytváření ticketu. Kontaktuj administrátory.",
                    ephemeral=True
                )

class TicketButton(discord.ui.Button):
    def __init__(self, label: str, custom_id: str, welcome_message: str, ticket_type: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.green,
            custom_id=custom_id
        )
        self.welcome_message = welcome_message
        self.ticket_type = ticket_type
    
    async def callback(self, interaction: discord.Interaction):
        await self.create_ticket_safe(interaction)
    
    async def create_ticket_safe(self, interaction: discord.Interaction):
        """Bezpečné vytvoření ticketu s error handlingem"""
        from .manager import TicketManager
        
        try:
            ticket_manager = interaction.client.ticket_manager
            await ticket_manager.create_ticket(
                interaction.guild, 
                interaction.user, 
                self.ticket_type, 
                self.welcome_message,
                interaction
            )
        except Exception as e:
            logger.error(f"Chyba při vytváření ticketu: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Nastala chyba při vytváření ticketu. Kontaktuj administrátory.",
                    ephemeral=True
                )

class TicketControlView(discord.ui.View):
    def __init__(self, ticket_type: str, creator_id: int):
        super().__init__(timeout=None)
        self.ticket_type = ticket_type
        self.creator_id = creator_id
        
        # Přidej close button s persistent custom_id
        close_button = CloseTicketButton(ticket_type, creator_id)
        self.add_item(close_button)

class CloseTicketButton(discord.ui.Button):
    def __init__(self, ticket_type: str, creator_id: int):
        super().__init__(
            label="🔒 Zavřít ticket",
            style=discord.ButtonStyle.red,
            custom_id=f"close_ticket_{creator_id}"
        )
        self.ticket_type = ticket_type
        self.creator_id = creator_id
    
    async def callback(self, interaction: discord.Interaction):
        from .manager import TicketManager
        
        try:
            ticket_manager = interaction.client.ticket_manager
            
            # Kontrola oprávnění
            can_close = (
                interaction.user.id == self.creator_id or
                await ticket_manager.has_mod_permissions(interaction.user, interaction.guild)
            )
            
            if not can_close:
                await interaction.response.send_message(
                    "Nemáš oprávnění zavřít tento ticket.", 
                    ephemeral=True
                )
                return
            
            await ticket_manager.close_ticket(
                interaction.channel, 
                interaction.user,
                self.ticket_type
            )
            
        except Exception as e:
            logger.error(f"Chyba při zavírání ticketu: {e}")
            await interaction.response.send_message(
                "Nastala chyba při zavírání ticketu.",
                ephemeral=True
            )

# Global handler pro ticket creation z persistent views
async def handle_ticket_creation(interaction: discord.Interaction, button_info: dict, ticket_manager):
    """Handles ticket creation from persistent views"""
    try:
        # Použij ticket manager k vytvoření ticketu (předej interaction pro správné error handling)
        result = await ticket_manager.create_ticket(
            interaction.guild,
            interaction.user,
            button_info['name'],
            button_info['message'],
            interaction  # Pass interaction pro správné error handling
        )
        
        # Handle tuple return (ticket_channel, error_msg)
        if isinstance(result, tuple):
            ticket_channel, error_msg = result
        else:
            # Fallback for old return format
            ticket_channel = result
            error_msg = None
        
        # Pokud create_ticket vrátilo error_msg, znamená to že interaction už byla zpracována
        if ticket_channel:
            # Success - create_ticket už poslal response, takže neděláme nic
            pass
        elif error_msg and not interaction.response.is_done():
            # Error a interaction ještě nebyla zpracována
            await interaction.response.send_message(
                f"❌ {error_msg}",
                ephemeral=True
            )
        elif not ticket_channel and not error_msg and not interaction.response.is_done():
            # Fallback pro neočekávané stavy
            await interaction.response.send_message(
                "❌ Nepodařilo se vytvořit ticket.",
                ephemeral=True
            )
            
    except Exception as e:
        logger.error(f"Chyba při vytváření ticketu: {e}")
        await interaction.response.send_message(
            f"❌ Chyba při vytváření ticketu: {e}",
            ephemeral=True
        )