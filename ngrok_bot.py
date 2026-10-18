import discord
import discord.utils
import queue
import datetime
import concurrent.futures, threading
import json
import os
import logging
import tempfile
import copy
import uuid
import re
import time
import io
from collections import OrderedDict
from pathlib import Path
from discord.ext import commands
from mcstatus import JavaServer
from mcstatus.motd import Motd
import asyncio
from discord.ui import Button, View
from dataclasses import dataclass
from lib_mc2 import to_ansi_123
from discord.ext.pages import Paginator, Page
import aiohttp
from functools import wraps

logger = logging.getLogger(__name__)
DATABASE_PATH = Path(__file__).resolve().with_name('server_database.json')
webhook_tasks = set()


def schedule_command_log(ctx, command_name, event, webhook_url):
    logger.info('Command %s: %s (user=%s)', command_name, event, ctx.author.id)
    if not webhook_url:
        return
    task = asyncio.create_task(send_webhook_log(
        webhook_url, ctx.author, ctx.guild, ctx.channel, command_name, event
    ))
    webhook_tasks.add(task)
    task.add_done_callback(webhook_tasks.discard)

token = os.getenv("DISCORD_TOKEN", "").strip()
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))
bot = commands.Bot(command_prefix='!', intents=discord.Intents.all())


WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

def log_to_webhook(webhook_url=None):
    def decorator(func):
        @wraps(func)
        async def wrapper(ctx, *args, **kwargs):
            url = webhook_url or WEBHOOK_URL
            schedule_command_log(ctx, func.__name__, 'started', url)
            try:
                result = await func(ctx, *args, **kwargs)
            except asyncio.CancelledError:
                schedule_command_log(ctx, func.__name__, 'cancelled', url)
                raise
            except Exception:
                logger.exception('Command %s failed', func.__name__)
                schedule_command_log(ctx, func.__name__, 'failed', url)
                raise
            schedule_command_log(ctx, func.__name__, 'completed', url)
            return result
        return wrapper
    return decorator

async def send_webhook_log(webhook_url, author, guild, channel, command_name, event):
    try:
        embed = discord.Embed(title="Command Log:",color=discord.Colour.blue())
        embed.add_field(name="User:",value=f"```{author.name}#{author.discriminator}```",inline=False)
        embed.add_field(name="Server:",value=f"```{guild.name if guild else 'DM'}```",inline=True)
        embed.add_field(name="Channel:",value=f"```{channel.name if hasattr(channel, 'name') else 'DM'}```",inline=True)
        embed.add_field(name="Command:",value=f"```{command_name}```",inline=False)
        embed.add_field(name="Event:", value=event, inline=False)
        embed.set_footer(text="NgrokSeeker • Command Logger",icon_url='https://github.com/twinedd/pngs/raw/main/4E289ECA-8F45-44AB-A0FA-C82E096D2B29.png')

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            webhook = discord.Webhook.from_url(webhook_url, session=session)
            await webhook.send(embed=embed, username="NgrokSeeker Logger")

    except Exception as e:
        logger.warning('Webhook delivery failed (%s)', type(e).__name__)



class NgrokSettings:
    def __init__(self):
        self.servers = False
        self.scanning = False
        self.recreate = False
        self.database_lock = threading.RLock()

@dataclass
class NgrokServer:
    ip: str = ""
    name: str = ""
    motd: str = ""
    icon: str = ""
    version: str = ""
    players_online: int = 0
    players_list: str = ""
    last_updated: datetime.datetime = None

ngrok_settings = NgrokSettings()

def server_identity(record):
    # Formatting is not identity; different text/version starts a new history.
    text = Motd.parse(record.get('motd', '')).to_plain()
    return (' '.join(text.split()), record.get('version', '').strip())


def remember_players(record, names, timestamp):
    history = record.setdefault('player_history', {})
    for name in names:
        # Status samples can contain advertisement text instead of player names.
        if not re.fullmatch(r'[A-Za-z0-9_]{1,16}', name):
            continue
        entry = history.setdefault(name.casefold(), {
            'name': name, 'first_seen': timestamp, 'last_seen': timestamp,
        })
        entry['name'] = name
        entry['last_seen'] = timestamp


def initialize_history(record):
    if 'player_history' not in record:
        remember_players(record, record.get('players_list', '').split(', '),
                         record.get('last_updated'))
    record.setdefault('instance_id', str(uuid.uuid4()))
    record.setdefault('first_seen', record.get('last_updated'))


def merge_server_observation(previous, observation, names):
    previous = copy.deepcopy(previous) if previous else None
    archives = previous.pop('previous_servers', []) if previous else []
    if previous:
        initialize_history(previous)
    if previous and server_identity(previous) != server_identity(observation):
        previous['available'] = False
        previous['archived_at'] = observation['last_updated']
        previous['archive_reason'] = 'description_or_version_changed'
        archives.append(previous)
        previous = None
    result = previous or {'instance_id': str(uuid.uuid4()),
                          'first_seen': observation['last_updated'],
                          'player_history': {}}
    result.update(observation)
    remember_players(result, names, observation['last_updated'])
    result['previous_servers'] = archives
    return result


def record_server_failure(ip):
    with ngrok_settings.database_lock:
        db = load_from_json()
        if ip in db:
            db[ip]['consecutive_failures'] = db[ip].get('consecutive_failures', 0) + 1
            db[ip]['available'] = False
            save_to_json(db)

def save_to_json(server_data):
    with ngrok_settings.database_lock:
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=DATABASE_PATH.parent,
                                             prefix=DATABASE_PATH.name + '.',
                                             suffix='.tmp', delete=False) as f:
                temporary_path = Path(f.name)
                json.dump(server_data, f, indent=4, ensure_ascii=False, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary_path, DATABASE_PATH)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

def load_from_json():
    with ngrok_settings.database_lock:
        try:
            with DATABASE_PATH.open('r', encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ValueError('Server database must contain a JSON object')
        return data

@bot.event
async def on_ready():
    activity = discord.Activity(name=" ngrok servers | idle", type=3)
    await bot.change_presence(status=discord.Status.online, activity=activity)

def format_seen_time(value):
    if not value:
        return 'Unknown'
    try:
        timestamp = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return 'Unknown'
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
    return f'{discord.utils.format_dt(timestamp, style="f")} ({discord.utils.format_dt(timestamp, style="R")})'


async def create_server_embed(list_players, motd, players_online, version, ip, icon, time, first_seen=None):
    embed = discord.Embed(title="Server Information:",color=discord.Colour.blue())
    embed.add_field(name="Server Address:",value=f"```{ip}```",inline=False)
    embed.add_field(name="MOTD:",value=motd,inline=False)
    embed.add_field(name="Server Info:",value=f"• **Version:** {version}\n"f"• **First seen:** {format_seen_time(first_seen)}\n"f"• **Last seen:** {format_seen_time(time)}",inline=True)
    embed.add_field(name=f"Players: ({players_online})",value=f"```{list_players}```",inline=False)
    embed.set_thumbnail(url=icon)
    embed.set_footer(text="NgrokSeeker • Server Scanner",icon_url='https://github.com/twinedd/pngs/raw/main/4E289ECA-8F45-44AB-A0FA-C82E096D2B29.png')
    return embed

async def create_embed_forscan():
    embed = discord.Embed(title = "Scanning has started!",description = "This may take some time....",color = discord.Colour.default())
    embed.set_author(name="NgrokSeeker", icon_url="https://github.com/twinedd/pngs/raw/main/icons8--128.png")
    return embed

async def create_embed_forstop():
    embed = discord.Embed(title = 'Scanning stop requested', description = 'You can start scanning again by slash command', color = discord.Colour.red())
    embed.set_author(name = '❌ NgrokSeeker')
    return embed

async def create_pages_FromDB(db):
    pages = []
    servers = [(ip, server) for ip, server in db.items() if server.get('available') is True]
    for i in range(0, len(servers), 5):
        embed = discord.Embed(title="Server List:", color=discord.Colour.blue())
        chunk = servers[i:i+5]
        view = View()
        count = 1
        for ip, server in chunk:
            len_ = len(str(f'**{count}**. | `{ip}` `{server["players_online"]} | {server["version"].split(" ")[-1].split("-")[-1]}` - {discord.utils.format_dt(datetime.datetime.fromisoformat(server["last_updated"]), style="R")}'))
            embed.add_field(name=f'**{count}**. | `{ip}{(68-len_)*" "}` `{server["players_online"]} | {server["version"].split(" ")[-1].split("-")[-1]}` - {discord.utils.format_dt(datetime.datetime.fromisoformat(server["last_updated"]), style="R")}',value=' ',inline=False)
            button = Button(label=str(count), style=discord.ButtonStyle.primary)

            async def button_callback(interaction, current_ip=ip, current_server=server):
                await interaction.response.defer(ephemeral=True)
                server_embed = await server_card(current_ip, current_server)
                await interaction.respond(embed=server_embed,
                                          view=create_history_view(current_ip, current_server),
                                          ephemeral=True)
            button.callback = button_callback
            view.add_item(button)
            count += 1
        embed.set_footer(text="NgrokSeeker • Server Scanner",icon_url='https://github.com/twinedd/pngs/raw/main/4E289ECA-8F45-44AB-A0FA-C82E096D2B29.png')
        pages.append({"embed": embed, "view": view})

    return pages

def create_player_history_pages(ip, server):
    record = copy.deepcopy(server)
    initialize_history(record)
    entries = sorted(record['player_history'].values(), key=lambda item: item['name'].casefold())
    pages = []
    for offset in range(0, len(entries), 5):
        embed = discord.Embed(title='Observed Players', color=discord.Colour.blue())
        embed.description = f'Address: `{ip}`\nUnique names observed: {len(entries)}'
        for number, entry in enumerate(entries[offset:offset + 5], 1):
            first = format_seen_time(entry.get('first_seen'))
            last = format_seen_time(entry.get('last_seen'))
            embed.add_field(name=f"{number}. {entry['name']}", value=f'First seen: {first}\nLast seen: {last}', inline=False)
        embed.set_footer(text='Only names observed in server responses are included.')
        pages.append(embed)
    return pages


class AsyncTTLCache:
    def __init__(self, limit=256):
        self.values = OrderedDict()
        self.pending = {}
        self.limit = limit

    async def get(self, key, fetch, ttl):
        cached = self.values.get(key)
        if cached and cached[0] > time.monotonic():
            self.values.move_to_end(key)
            return cached[1]
        if key not in self.pending:
            async def run():
                try:
                    value = await fetch()
                    self.values[key] = (time.monotonic() + ttl(value), value)
                    self.values.move_to_end(key)
                    while len(self.values) > self.limit:
                        self.values.popitem(last=False)
                    return value
                finally:
                    self.pending.pop(key, None)
            self.pending[key] = asyncio.create_task(run())
        return await asyncio.shield(self.pending[key])


profile_cache = AsyncTTLCache()
face_cache = AsyncTTLCache(limit=64)
profile_retry_at = 0.0


async def lookup_player_profile(name):
    return await profile_cache.get(name.casefold(), lambda: fetch_player_profile(name),
                                   lambda value: 900 if value['status'] == 'found' else
                                   300 if value['status'] == 'not_found' else 30)


async def fetch_player_profile(name):
    global profile_retry_at
    if time.monotonic() < profile_retry_at:
        return {'status': 'unavailable'}
    if not re.fullmatch(r'[A-Za-z0-9_]{1,16}', name):
        return {'status': 'unavailable'}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(f'https://api.mojang.com/users/profiles/minecraft/{name}') as response:
                if response.status == 429:
                    try:
                        delay = max(60, float(response.headers.get('Retry-After', '60')))
                    except (ValueError, TypeError):
                        delay = 60
                    profile_retry_at = time.monotonic() + delay
                    logger.warning('Mojang profile API rate limited; retry in %.0f seconds', delay)
                    return {'status': 'unavailable'}
                if response.status in (204, 404):
                    return {'status': 'not_found'}
                if response.status != 200:
                    logger.warning('Mojang profile API returned HTTP %s', response.status)
                    return {'status': 'unavailable'}
                data = await response.json()
                profile_id = uuid.UUID(data['id']).hex
                if data['name'].casefold() != name.casefold():
                    return {'status': 'unavailable'}
                return {'status': 'found', 'id': profile_id, 'name': data['name']}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError, AttributeError) as error:
        logger.warning('Mojang profile lookup failed: %s', type(error).__name__)
        return {'status': 'unavailable'}


async def player_card(ip, entry):
    embed = discord.Embed(title='Player Information', color=discord.Colour.blue())
    embed.add_field(name='Observed name', value=entry['name'], inline=False)
    embed.add_field(name='Server', value=ip, inline=False)
    embed.add_field(name='First seen', value=format_seen_time(entry.get('first_seen')))
    embed.add_field(name='Last seen', value=format_seen_time(entry.get('last_seen')))
    profile = await lookup_player_profile(entry['name'])
    status = profile['status']
    identifier = profile['id'] if status == 'found' else entry['name']
    embed.set_thumbnail(url=f'https://mc-heads.net/avatar/{identifier}/128.png')
    if status == 'found':
        embed.add_field(name='Java profile', value='Found for this username', inline=False)
        embed.add_field(name='Profile UUID', value=profile['id'], inline=False)
    elif status == 'not_found':
        embed.add_field(name='Java profile', value='No current profile found for this username', inline=False)
    else:
        embed.add_field(name='Java profile', value='Lookup unavailable. Try again later.', inline=False)
    return embed


async def download_player_face(url):
    return await face_cache.get(url, lambda: fetch_player_face(url),
                                lambda value: 1200 if value is not None else 30)


async def fetch_player_face(url):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    logger.warning('Face API returned HTTP %s', response.status)
                    return None
                data = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        return None
                if not data.startswith(b'\x89PNG\r\n\x1a\n'):
                    logger.warning('Face API returned non-PNG content: %s', response.headers.get('Content-Type'))
                    return None
                return bytes(data)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.warning('Could not download player face')
        return None


async def send_player_card(interaction, embed):
    url = embed.thumbnail.url if embed.thumbnail else None
    face = await download_player_face(url) if url else None
    if face is not None:
        embed.set_thumbnail(url='attachment://face.png')
        attachment = discord.File(io.BytesIO(face), filename='face.png')
        try:
            await interaction.followup.send(embed=embed, file=attachment, ephemeral=True)
        finally:
            attachment.close()
    else:
        if url:
            embed.set_thumbnail(url=None)
            embed.add_field(name='Skin', value=f'Could not load the face. [Open skin image]({url})', inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def acknowledge_player_click(interaction):
    try:
        # Component defer defaults to an invisible update; explicitly create a loading reply.
        await interaction.response.defer(ephemeral=True, invisible=False)
        return True
    except discord.NotFound as error:
        if error.code != 10062:
            raise
        age = (datetime.datetime.now(datetime.timezone.utc) - interaction.created_at).total_seconds()
        logger.warning('Player button acknowledgement expired (10062); interaction age %.2fs, gateway latency %.3fs',
                       age, bot.latency)
        return False
    except discord.InteractionResponded:
        logger.warning('Player button interaction already acknowledged')
        return False


def player_buttons(ip, entries):
    view = View(timeout=500)
    for number, entry in enumerate(entries, 1):
        button = Button(label=str(number), style=discord.ButtonStyle.primary, row=1)

        async def callback(interaction, selected=copy.deepcopy(entry)):
            if not await acknowledge_player_click(interaction):
                return
            embed = await player_card(ip, selected)
            await send_player_card(interaction, embed)

        button.callback = callback
        view.add_item(button)
    return view


async def server_card(ip, server):
    embed = await create_server_embed(
        list_players=server['players_list'],
        motd=f"```ansi\n{to_ansi_123(Motd.parse(server['motd']).to_minecraft())}```",
        players_online=server['players_online'], version=server['version'], ip=ip,
        icon=server['icon'], time=server['last_updated'], first_seen=server.get('first_seen'))
    if server.get('available') is False:
        embed.description = 'Server did not respond. Showing last known information.'
    return embed


def refresh_server_record(ip):
    ping_server(ip)
    return load_from_json().get(ip)


def create_history_view(ip, server):
    view = View(timeout=500)
    current = copy.deepcopy(server)
    busy = False
    last_refresh = -float('inf')
    button = Button(label='Player History', style=discord.ButtonStyle.secondary)
    refresh = Button(label='Refresh', style=discord.ButtonStyle.primary)

    async def callback(interaction):
        pages = create_player_history_pages(ip, current)
        if not pages:
            await interaction.response.send_message('No player names have been observed on this server yet.', ephemeral=True)
            return
        record = copy.deepcopy(current)
        initialize_history(record)
        entries = sorted(record['player_history'].values(), key=lambda item: item['name'].casefold())
        paginator_pages = [Page(embeds=[embed], custom_view=player_buttons(ip, entries[i * 5:i * 5 + 5]))
                           for i, embed in enumerate(pages)]
        paginator = Paginator(pages=paginator_pages, timeout=500, disable_on_timeout=True)
        await paginator.respond(interaction=interaction, ephemeral=True)

    async def refresh_callback(interaction):
        nonlocal current, busy, last_refresh
        if busy or time.monotonic() - last_refresh < 5:
            await interaction.response.send_message('Please wait a few seconds before refreshing again.', ephemeral=True)
            return
        busy = True
        last_refresh = time.monotonic()
        refresh.disabled = True
        refresh.label = 'Refreshing…'
        try:
            await interaction.response.edit_message(view=view)
            updated = await asyncio.to_thread(refresh_server_record, ip)
            if updated is None:
                raise ValueError('Server record missing after refresh')
            embed = await server_card(ip, updated)
            if updated.get('instance_id') != current.get('instance_id'):
                embed.description = 'Server identity changed. Player history now belongs to the new server.'
            current = updated
        except Exception:
            logger.exception('Manual refresh failed for %s', ip)
            await interaction.followup.send('Could not refresh the server. Please try again later.', ephemeral=True)
        else:
            await interaction.edit_original_response(embed=embed)
        finally:
            busy = False
            refresh.disabled = False
            refresh.label = 'Refresh'
            await interaction.edit_original_response(view=view)

    button.callback = callback
    refresh.callback = refresh_callback
    view.add_item(button)
    view.add_item(refresh)
    return view


def ping_server(ip):
    try:
        server = JavaServer.lookup(f"{ip}", .3).status()
    except Exception:
        logger.debug('Server query failed: %s', ip, exc_info=True)
        record_server_failure(ip)
        return
    player_names = [player.name for player in (server.players.sample or [])]
    list_players = ", ".join(player_names) if player_names else (
        'no players online' if server.players.online == 0 else 'player names not provided')
    version = server.version.name
    motd = server.motd.raw
    server_players_online = server.players.online

    n_server = NgrokServer(
        ip=ip,
        motd=motd,
        icon= f'https://api.mcsrvstat.us/icon/{ip}',
        version=version,
        players_online=server_players_online,
        players_list=list_players,
        last_updated=datetime.datetime.now(datetime.timezone.utc))

    with ngrok_settings.database_lock:
        servers_data = load_from_json()
        observation = {
            'motd': n_server.motd,
            'icon': n_server.icon,
            'version': n_server.version,
            'players_online': n_server.players_online,
            'players_list': n_server.players_list,
            'last_updated': n_server.last_updated.isoformat(),
            'available': True,
            'consecutive_failures': 0}
        servers_data[n_server.ip] = merge_server_observation(
            servers_data.get(n_server.ip), observation, player_names)
        save_to_json(servers_data)
        return

def create_queue_ip():
    list_servers = queue.Queue()
    for port in range(10000, 30000):
        for host in range(1, 8):
            list_servers.put(f'{host}.tcp.eu.ngrok.io:{port}')
    return list_servers

def worker(q):
    while True:
        try:
            ip = q.get_nowait()
        except queue.Empty:
            return
        try:
            ping_server(ip)
        except Exception:
            logger.exception('Failed to process server %s', ip)
        finally:
            q.task_done()


def scan_loop():
    # Only the coordinator creates a new pass, after every worker has finished.
    with concurrent.futures.ThreadPoolExecutor(max_workers=13) as executor:
        while True:
            ip_queue = create_queue_ip()
            futures = [executor.submit(worker, ip_queue) for _ in range(13)]
            for future in futures:
                future.result()
            if not ngrok_settings.recreate:
                return

@bot.slash_command(description="Starts scanning")
@log_to_webhook()
async def start_scanning(ctx):
    if ctx.author.id == BOT_OWNER_ID:
        ngrok_settings.recreate = True
        if not ngrok_settings.scanning:
            ngrok_settings.scanning = True
            await ctx.respond(embed = await create_embed_forscan())
            await bot.change_presence(status=discord.Status.online, activity=discord.Activity(name=" ngrok servers | active", type=3))
            await asyncio.to_thread(scan_loop)
        else:
            await ctx.respond('Scanning is already running.')
    else:
        await ctx.respond('You cannot use this command', ephemeral=True)

@bot.slash_command(description="Stop scanning")
@log_to_webhook()
async def stop_scanning(ctx):
    if ctx.author.id == BOT_OWNER_ID:
        ngrok_settings.recreate = False
        ngrok_settings.scanning = False
        await ctx.respond(embed = await create_embed_forstop())
        await bot.change_presence(status=discord.Status.online, activity=discord.Activity(name=" ngrok servers | idle", type=3))
    else:
        await ctx.respond('You cannot use this command', ephemeral=True)

@bot.slash_command(description="Show available servers")
@log_to_webhook()
async def servers(ctx):
    await ctx.defer(ephemeral=True)
    try:
        servers_data = await asyncio.to_thread(load_from_json)
    except (OSError, ValueError):
        logger.exception('Unable to read server database')
        await ctx.respond('Could not read the server database. Check the error log.', ephemeral=True)
        return
    my_pages_data = await create_pages_FromDB(servers_data)
    if not my_pages_data:
        await ctx.respond('No available servers. Records remain in the database; older records will appear after a successful check.', ephemeral=True)
        return
    my_pages = [Page(embeds=[data["embed"]], custom_view=data["view"]) for data in my_pages_data]
    paginator = Paginator(pages=my_pages, timeout=500, disable_on_timeout=True,)
    await paginator.respond(interaction=ctx.interaction,ephemeral=True)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    if not token or not BOT_OWNER_ID:
        raise SystemExit('Set DISCORD_TOKEN and BOT_OWNER_ID in .env (Pipenv) or environment variables.')
    bot.run(token)
