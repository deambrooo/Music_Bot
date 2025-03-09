import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiohttp
import os
import asyncio
import json
import yt_dlp as youtube_dl
import requests
import logging

load_dotenv()

# Define file names
WHITELIST_FILE = 'whitelist.json'
PUBLICP_FILE = 'publicp.json'
SERVERS_FILE = 'servers.json'
STATUS_CHANNEL_FILE = 'status_channel.json'
STATUS_MESSAGES_FILE = 'status_messages.json'
CACHED_SONG_FILE = "cached_song.json"

def save_json(file, data):
    with open(file, 'w') as f:
        json.dump(data, f)

def load_json(file):
    if os.path.exists(file):
        with open(file, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

# Initialize JSON files if they don't exist
def initialize_json_files():
    files_defaults = {
        WHITELIST_FILE: {'whitelisted_users': []},
        PUBLICP_FILE: {},
        SERVERS_FILE: {'servers': {}},
        STATUS_CHANNEL_FILE: {'channel_id': None},
        STATUS_MESSAGES_FILE: {'messages': {}},
        CACHED_SONG_FILE: {}
    }
    for file, default in files_defaults.items():
        if not os.path.exists(file):
            save_json(file, default)

initialize_json_files()

def load_whitelist():
    data = load_json(WHITELIST_FILE)
    return data.get('whitelisted_users', [])

def save_whitelist(whitelisted_users):
    save_json(WHITELIST_FILE, {'whitelisted_users': whitelisted_users})

def load_publicp():
    return load_json(PUBLICP_FILE)

def save_publicp(publicp):
    save_json(PUBLICP_FILE, publicp)

def load_servers():
    data = load_json(SERVERS_FILE)
    return data.get('servers', {})

def save_servers(servers):
    save_json(SERVERS_FILE, {'servers': servers})

def load_status_channel():
    data = load_json(STATUS_CHANNEL_FILE)
    return data.get('channel_id', None)

def save_status_channel(channel_id):
    save_json(STATUS_CHANNEL_FILE, {'channel_id': channel_id})

def load_status_messages():
    data = load_json(STATUS_MESSAGES_FILE)
    return data.get('messages', {})

def save_status_messages(messages):
    save_json(STATUS_MESSAGES_FILE, {'messages': messages})

def load_cached_song():
    return load_json(CACHED_SONG_FILE)

def save_cached_song(url, audio_url):
    save_json(CACHED_SONG_FILE, {"url": url, "audio_url": audio_url})

class PlayerControls(discord.ui.View):
    def __init__(self, voice_client):
        super().__init__(timeout=None)
        self.voice_client = voice_client
        self.looping = False
        self.last_url = None
        self.volume = 1.0
        self.forced_stop = False

        # Save the channel ID for reconnecting if needed
        if self.voice_client and self.voice_client.channel:
            self.voice_channel_id = self.voice_client.channel.id
        else:
            self.voice_channel_id = None

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary)
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.voice_client.is_playing():
            self.voice_client.pause()
            await interaction.response.send_message("Paused the audio.", ephemeral=True)
        else:
            await interaction.response.send_message("No audio is currently playing.", ephemeral=True)

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.primary)
    async def resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.voice_client.is_paused():
            self.voice_client.resume()
            await interaction.response.send_message("Resumed the audio.", ephemeral=True)
        else:
            await interaction.response.send_message("The audio is not paused.", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.voice_client.is_playing() or self.voice_client.is_paused():
            # Mark as forced stop so after_playing() won't loop
            self.forced_stop = True
            self.voice_client.stop()
            await interaction.response.send_message("Stopped the audio.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing to stop.", ephemeral=True)

    @discord.ui.button(label="Disconnect", style=discord.ButtonStyle.danger)
    async def disconnect(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.voice_client:
            # Mark forced_stop so no loop replay
            self.forced_stop = True
            await self.voice_client.disconnect()
            await interaction.response.send_message("Disconnected from the voice channel.", ephemeral=True)
        else:
            await interaction.response.send_message("Bot is not in a voice channel.", ephemeral=True)

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.looping = not self.looping
        status = "enabled" if self.looping else "disabled"
        await interaction.response.send_message(f"Loop {status}.", ephemeral=True)

    @discord.ui.button(label="Replay", style=discord.ButtonStyle.secondary)
    async def replay(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Restarts the current or last-played audio track."""

        await interaction.response.defer()

        # If something is currently playing/paused, forcibly stop it first
        if self.voice_client.is_playing() or self.voice_client.is_paused():
            self.forced_stop = True
            self.voice_client.stop()

        if self.last_url:
            # If the bot is not connected or forcibly disconnected, reconnect
            if not self.voice_client.is_connected():
                if self.voice_channel_id:
                    channel = interaction.guild.get_channel(self.voice_channel_id)
                    if channel:
                        await channel.connect()
                        # Rebind the voice client
                        self.voice_client = interaction.guild.voice_client
                        self.voice_client.player_controls = self
                    else:
                        await interaction.followup.send("No saved voice channel to reconnect to.", ephemeral=True)
                        return
                else:
                    await interaction.followup.send("No audio source available to replay.", ephemeral=True)
                    return

            # Reset forced_stop so the loop can function if needed
            self.forced_stop = False

            # Actually replay the audio
            await play_audio(self.voice_client, self.last_url, self.volume, replay=True)
            await interaction.followup.send("Replaying the last requested audio.", ephemeral=True)
        else:
            await interaction.followup.send("No audio source available to replay.", ephemeral=True)

    @discord.ui.button(label="🔊 Volume Up", style=discord.ButtonStyle.success)
    async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.volume = min(self.volume + 0.1, 2.0)
        if self.voice_client.source:
            self.voice_client.source.volume = self.volume
        percent = int(self.volume * 100)
        await interaction.response.send_message(f"Volume increased to {percent}%.", ephemeral=True)

    @discord.ui.button(label="🔉 Volume Down", style=discord.ButtonStyle.success)
    async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.volume = max(self.volume - 0.1, 0.1)
        if self.voice_client.source:
            self.voice_client.source.volume = self.volume
        percent = int(self.volume * 100)
        await interaction.response.send_message(f"Volume decreased to {percent}%.", ephemeral=True)

async def play_audio(voice_client, url, volume=1.0, replay=False):
    """
    Play or replay an audio URL (YouTube).
    If replay=True, we skip the YT-DL extraction if there's a cached URL matching 'url'.
    """
    cached_song = load_cached_song()
    if replay and cached_song and cached_song.get("url") == url:
        audio_url = cached_song.get("audio_url")
    else:
        ydl_opts = {'format': 'bestaudio/best', 'noplaylist': True}
        try:
            loop = asyncio.get_event_loop()
            with youtube_dl.YoutubeDL(ydl_opts) as ydl:
                info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
                audio_url = info['url']
                save_cached_song(url, audio_url)
        except Exception as e:
            print(f"Error playing audio: {e}")
            return None

    ffmpeg_options = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn'
    }
    source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_options)
    source = discord.PCMVolumeTransformer(source, volume)

    def after_playing(error):
        if error:
            print(f"Error playing audio: {error}")

        controls = getattr(voice_client, "player_controls", None)
        if controls:
            # If loop is enabled and we didn't forcibly stop, replay
            if controls.looping and not controls.forced_stop:
                asyncio.run_coroutine_threadsafe(
                    play_audio(voice_client, url, controls.volume, replay=True),
                    client.loop
                )
                return

        # Removed auto-disconnect here; rely on check_voice_channel
        return

    voice_client.play(source, after=lambda e: after_playing(e))
    return source

@tasks.loop(seconds=60)
async def check_voice_channel():
    """
    Disconnect if alone in the channel for 60 seconds.
    """
    for guild in client.guilds:
        voice_client = guild.voice_client
        if voice_client and voice_client.is_connected():
            channel = voice_client.channel
            if len(channel.members) == 1 and client.user in channel.members:
                await voice_client.disconnect()

@tasks.loop(seconds=10)
async def check_server_status():
    global previous_statuses
    status_channel_id = load_status_channel()
    status_messages = load_status_messages()
    servers = load_servers()
    channel = client.get_channel(int(status_channel_id)) if status_channel_id else None
    if not channel or not servers:
        return
    online_servers = []
    offline_servers = []
    for server_name, server_info in servers.items():
        ip = server_info['ip']
        port = server_info['port']
        endpoint = f"http://{ip}:{port}/status"
        try:
            response = requests.get(endpoint, timeout=5)
            response.raise_for_status()
            online_servers.append(f"{server_name} 🟢")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error querying server {server_name} status: {e}")
            offline_servers.append(f"{server_name} 🔴")
    status_message = (
        "     **DN STATUS**\n\n"
        "**Server Status**\n"
        f"**ONLINE SERVERS 🟢:**\n{', '.join(online_servers) if online_servers else 'None'}\n"
        f"**Offline servers 🔴:**\n{', '.join(offline_servers) if offline_servers else 'None'}\n"
    )
    global previous_statuses
    if status_message != previous_statuses.get('status_message'):
        if 'status_message_id' in status_messages:
            try:
                msg_obj = await channel.fetch_message(status_messages['status_message_id'])
                await msg_obj.edit(content=status_message)
            except discord.NotFound:
                msg_obj = await channel.send(status_message)
                status_messages['status_message_id'] = msg_obj.id
        else:
            msg_obj = await channel.send(status_message)
            status_messages['status_message_id'] = msg_obj.id
        previous_statuses['status_message'] = status_message
        save_status_messages(status_messages)

previous_statuses = {}

async def handle_rate_limits(func, *args, **kwargs):
    retries = 5
    delay = 1
    for i in range(retries):
        try:
            return await func(*args, **kwargs)
        except discord.errors.HTTPException as e:
            if e.status == 429:
                retry_after = int(e.response.headers.get('Retry-After', delay))
                logging.warning(f"Rate limited. Retrying in {retry_after} seconds...")
                await asyncio.sleep(retry_after)
                delay *= 2
            else:
                raise
    raise Exception("Max retries exceeded")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
client = commands.Bot(command_prefix="!", intents=intents)

@client.event
async def on_ready():
    logging.info(f'Logged in as {client.user}')
    await handle_rate_limits(client.tree.sync)
    check_voice_channel.start()
    check_server_status.start()

@client.tree.command(name="playbot", description="Play a YouTube video in a voice channel")
@app_commands.describe(url="The URL of the YouTube video")
async def playbot(interaction: discord.Interaction, url: str):
    await interaction.response.defer()
    publicp = load_publicp()
    text_channel_id = publicp.get('text_channel_id')
    voice_channel_id = publicp.get('voice_channel_id')

    # Check channel requirements
    if text_channel_id and voice_channel_id:
        if interaction.channel_id == int(text_channel_id):
            if interaction.user.voice and interaction.user.voice.channel.id == int(voice_channel_id):
                pass
            else:
                vc_obj = interaction.guild.get_channel(int(voice_channel_id))
                await interaction.followup.send(
                    f"You must be in the voice channel: {vc_obj.name}",
                    ephemeral=True
                )
                return
        else:
            whitelisted_users = load_whitelist()
            if (str(interaction.user.id) not in whitelisted_users and
                    str(client.user.id) not in whitelisted_users):
                text_channel = interaction.guild.get_channel(int(text_channel_id)) if text_channel_id else None
                channel_name = text_channel.name if text_channel else "specified channel"
                await interaction.followup.send(
                    f"Please use the command in the {channel_name}",
                    ephemeral=True
                )
                return
    else:
        whitelisted_users = load_whitelist()
        if (str(interaction.user.id) not in whitelisted_users and
                str(client.user.id) not in whitelisted_users):
            await interaction.followup.send(
                "You are not whitelisted to use this command.",
                ephemeral=True
            )
            return

    # Basic YouTube check
    if "youtube.com" not in url and "youtu.be" not in url:
        await interaction.followup.send("The provided URL is not a valid YouTube video.", ephemeral=True)
        return

    if not interaction.user.voice:
        await interaction.followup.send("You must be in a voice channel to use this command.", ephemeral=True)
        return

    channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client
    if voice_client is None:
        voice_client = await channel.connect()
    elif voice_client.channel != channel:
        await voice_client.move_to(channel)

    # Stop the currently playing/paused audio if any
    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()

    try:
        source = await play_audio(voice_client, url, volume=1.0)
        if source:
            controls = PlayerControls(voice_client)
            controls.last_url = url
            voice_client.player_controls = controls
            await interaction.followup.send(
                "Now playing your requested audio.",
                view=controls,
                ephemeral=True
            )
        else:
            await interaction.followup.send("An error occurred while trying to play the audio.", ephemeral=True)
    except Exception as e:
        logging.error(f"Error: {e}")
        await interaction.followup.send("An error occurred while trying to play the audio.", ephemeral=True)

@client.tree.command(name="whitelist", description="Whitelist a user to use the bot")
@app_commands.describe(user="The user to whitelist")
async def whitelist(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
    whitelisted_users = load_whitelist()
    if str(user.id) not in whitelisted_users:
        whitelisted_users.append(str(user.id))
        save_whitelist(whitelisted_users)
        await interaction.response.send_message(f"User {user.mention} has been whitelisted.", ephemeral=True)
    else:
        await interaction.response.send_message(f"User {user.mention} is already whitelisted.", ephemeral=True)

@client.tree.command(name="setmchanneltext", description="Set the text channel for the bot")
@app_commands.describe(text_channel_id="The ID of the text channel")
async def setmchanneltext(interaction: discord.Interaction, text_channel_id: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
    publicp = load_publicp()
    publicp['text_channel_id'] = text_channel_id
    save_publicp(publicp)
    await interaction.response.send_message(f"Text channel set to {text_channel_id}.", ephemeral=True)

@client.tree.command(name="setmchannelvc", description="Set the voice channel for the bot")
@app_commands.describe(voice_channel_id="The ID of the voice channel")
async def setmchannelvc(interaction: discord.Interaction, voice_channel_id: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
    publicp = load_publicp()
    publicp['voice_channel_id'] = voice_channel_id
    save_publicp(publicp)
    await interaction.response.send_message(f"Voice channel set to {voice_channel_id}.", ephemeral=True)

@client.tree.command(name="remchanneltext", description="Remove the text channel for the bot")
async def remchanneltext(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
    publicp = load_publicp()
    publicp.pop('text_channel_id', None)
    save_publicp(publicp)
    await interaction.response.send_message("Text channel has been removed.", ephemeral=True)

@client.tree.command(name="remchannelvc", description="Remove the voice channel for the bot")
async def remchannelvc(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
    publicp = load_publicp()
    publicp.pop('voice_channel_id', None)
    save_publicp(publicp)
    await interaction.response.send_message("Voice channel has been removed.", ephemeral=True)

@client.tree.command(name="mchannellist", description="Get the list of the current text and voice channels")
async def mchannellist(interaction: discord.Interaction):
    publicp = load_publicp()
    text_channel_id = publicp.get('text_channel_id', 'Not set')
    voice_channel_id = publicp.get('voice_channel_id', 'Not set')
    await interaction.response.send_message(
        f"Text channel: {text_channel_id}\nVoice channel: {voice_channel_id}",
        ephemeral=True
    )

@client.tree.command(name="setstatuschannel", description="Set the channel for server status updates")
@app_commands.describe(channel_id="The ID of the text channel")
async def setstatuschannel(interaction: discord.Interaction, channel_id: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
    save_status_channel(channel_id)
    await interaction.response.send_message(f"Status channel set to {channel_id}.", ephemeral=True)

@client.tree.command(name="addserver", description="Add a server to monitor")
@app_commands.describe(name="The custom name for the server", ip="The IP address of the server", port="The port of the server")
async def addserver(interaction: discord.Interaction, name: str, ip: str, port: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
    servers = load_servers()
    servers[name] = {'ip': ip, 'port': port}
    save_servers(servers)
    await interaction.response.send_message(f"Server '{name}' added with IP {ip} and port {port}.", ephemeral=True)

@client.tree.command(name="removeserver", description="Remove a server from monitoring")
@app_commands.describe(name="The custom name of the server to remove")
async def removeserver(interaction: discord.Interaction, name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
    servers = load_servers()
    if name in servers:
        del servers[name]
        save_servers(servers)
        await interaction.response.send_message(f"Server '{name}' removed.", ephemeral=True)
    else:
        await interaction.response.send_message(f"Server '{name}' not found.", ephemeral=True)

@client.tree.command(name="listservers", description="List all monitored servers")
async def listservers(interaction: discord.Interaction):
    servers = load_servers()
    if servers:
        server_list = "\n".join([f"{name}: {info['ip']}:{info['port']}" for name, info in servers.items()])
        await interaction.response.send_message(f"Monitored servers:\n{server_list}", ephemeral=True)
    else:
        await interaction.response.send_message("No servers are currently being monitored.", ephemeral=True)

logging.basicConfig(level=logging.INFO)
client.run(os.getenv('token'))
