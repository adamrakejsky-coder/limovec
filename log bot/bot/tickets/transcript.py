import discord
from discord.ext import commands
from typing import List, Dict
import io
from datetime import datetime
import html
import asyncio

class TranscriptGenerator:
    def __init__(self):
        self.format_options = ['txt', 'html']
    
    async def generate_transcript(self, channel: discord.TextChannel, 
                                format_type: str = 'txt') -> discord.File:
        """Generuje transcript v různých formátech"""
        
        if format_type == 'html':
            return await self.generate_html_transcript(channel)
        else:
            return await self.generate_txt_transcript(channel)
    
    async def generate_txt_transcript(self, channel: discord.TextChannel) -> discord.File:
        """Generuje textový transcript"""
        transcript_lines = []
        transcript_lines.append(f"=== TRANSCRIPT TICKETU: {channel.name} ===\n")
        transcript_lines.append(f"Kanál: #{channel.name}")
        transcript_lines.append(f"Server: {channel.guild.name}")
        transcript_lines.append(f"Vygenerováno: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
        transcript_lines.append("=" * 50 + "\n")
        
        try:
            messages = []
            async for message in channel.history(limit=None):
                messages.append(message)
            
            messages.reverse()  # Chronologické pořadí
            
            for message in messages:
                timestamp = message.created_at.strftime('%d.%m.%Y %H:%M:%S')
                author = f"{message.author.display_name} ({message.author})"
                content = message.content or "[Žádný textový obsah]"
                
                transcript_lines.append(f"[{timestamp}] {author}: {content}")
                
                # Přidej info o přílohách
                if message.attachments:
                    for attachment in message.attachments:
                        transcript_lines.append(f"    📎 Příloha: {attachment.filename}")
                
                # Přidej info o embedech
                if message.embeds:
                    for embed in message.embeds:
                        if embed.title:
                            transcript_lines.append(f"    📋 Embed: {embed.title}")
        
        except Exception as e:
            transcript_lines.append(f"\n❌ Chyba při čtení zpráv: {e}")
        
        content = "\n".join(transcript_lines)
        buffer = io.StringIO(content)
        
        filename = f"transcript-{channel.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        return discord.File(buffer, filename=filename)
    
    async def generate_html_transcript(self, channel: discord.TextChannel) -> discord.File:
        """HTML transcript s Discord-like stylingem"""
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>Transcript - {html.escape(channel.name)}</title>
            <style>
                body {{
                    background-color: #36393f;
                    color: #dcddde;
                    font-family: Whitney, "Helvetica Neue", Helvetica, Arial, sans-serif;
                    margin: 0;
                    padding: 20px;
                }}
                .header {{
                    background-color: #2f3136;
                    padding: 20px;
                    border-radius: 8px;
                    margin-bottom: 20px;
                }}
                .message {{
                    margin-bottom: 16px;
                    padding: 8px;
                    border-radius: 4px;
                }}
                .message:hover {{
                    background-color: #32353b;
                }}
                .author {{
                    font-weight: 600;
                    color: #ffffff;
                }}
                .timestamp {{
                    color: #72767d;
                    font-size: 12px;
                    margin-left: 8px;
                }}
                .content {{
                    margin-top: 4px;
                    word-wrap: break-word;
                }}
                .attachment {{
                    color: #00b0f4;
                    margin-top: 4px;
                }}
                .embed {{
                    border-left: 4px solid #7289da;
                    background-color: #2f3136;
                    padding: 8px 12px;
                    margin-top: 4px;
                    border-radius: 0 4px 4px 0;
                }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>Transcript: #{html.escape(channel.name)}</h1>
                <p>Server: {html.escape(channel.guild.name)}</p>
                <p>Vygenerováno: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</p>
            </div>
        """
        
        try:
            messages = []
            async for message in channel.history(limit=None):
                messages.append(message)
            
            messages.reverse()
            
            for message in messages:
                timestamp = message.created_at.strftime('%d.%m.%Y %H:%M:%S')
                author_name = html.escape(message.author.display_name)
                content = html.escape(message.content) if message.content else "<em>[Žádný textový obsah]</em>"
                
                html_content += f"""
                <div class="message">
                    <span class="author">{author_name}</span>
                    <span class="timestamp">{timestamp}</span>
                    <div class="content">{content}</div>
                """
                
                # Přidej přílohy
                for attachment in message.attachments:
                    attachment_name = html.escape(attachment.filename)
                    html_content += f'<div class="attachment">📎 Příloha: {attachment_name}</div>'
                
                # Přidej embedy
                for embed in message.embeds:
                    if embed.title:
                        embed_title = html.escape(embed.title)
                        html_content += f'<div class="embed">📋 {embed_title}</div>'
                
                html_content += "</div>"
        
        except Exception as e:
            html_content += f'<div class="message"><span class="author">Systém</span><div class="content">❌ Chyba při čtení zpráv: {html.escape(str(e))}</div></div>'
        
        html_content += """
        </body>
        </html>
        """
        
        buffer = io.StringIO(html_content)
        filename = f"transcript-{channel.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
        
        return discord.File(buffer, filename=filename)